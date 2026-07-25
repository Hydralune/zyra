import type {
  PermissionApi,
  PermissionSessionBinding,
} from "../../api/permission-api.ts"
import type { TaskApi } from "../../api/task-api.ts"
import {
  freshPermissionId,
  identifier,
} from "./canonical.ts"
import type {
  PermissionConsoleOptions,
  PermissionConsoleSnapshot,
  PermissionDecisionReceipt,
  PermissionInterventionRecord,
  PermissionRespondOptions,
  PermissionResponseAttempt,
  PermissionTaskBindingInput,
} from "./contracts.ts"
import { PermissionExpirySupervisor } from "./expiry-supervisor.ts"
import {
  PermissionEventReconciler,
  type PermissionCanonicalEvent,
} from "./event-reconciler.ts"
import { PermissionInterventionLedger } from "./intervention-ledger.ts"
import { PermissionPermitLifecycle } from "./permit-lifecycle.ts"
import {
  projectPermissionBackend,
  projectPermissionReceipt,
  type PermissionBackendProjection,
} from "./projection.ts"
import {
  createPermissionResponseDraft,
  createPermissionResponseProof,
  verifyLocalPermissionResponseProof,
} from "./response-proof.ts"
import { PermissionResponseRace } from "./response-race.ts"
import { PermissionReconnectSupervisor } from "./reconnect-supervisor.ts"
import { PermissionReceiptLedger } from "./receipt-admission.ts"
import { PermissionSessionRoster } from "./session-roster.ts"
import { PermissionConsoleStore } from "./store.ts"

export class PermissionConsoleRuntime {
  readonly #api: PermissionApi
  readonly #store: PermissionConsoleStore
  readonly #race: PermissionResponseRace
  readonly #interventions: PermissionInterventionLedger
  readonly #events: PermissionEventReconciler
  readonly #sessions: PermissionSessionRoster
  readonly #reconnect: PermissionReconnectSupervisor
  readonly #receiptLedger: PermissionReceiptLedger
  readonly #expiry: PermissionExpirySupervisor
  readonly #permits: PermissionPermitLifecycle
  readonly #now: () => Date
  readonly #pollIntervalMs: number
  readonly #requestTimeoutMs: number
  readonly #maximumRequests: number
  readonly #maximumReceipts: number
  readonly #disabled: boolean
  #bindingInput?: PermissionTaskBindingInput
  #generation = 0
  #refreshCount = 0
  #reconnectCount = 0
  #refresh?: Promise<PermissionConsoleSnapshot>
  #abort?: AbortController
  #timer?: ReturnType<typeof setTimeout>
  #closed = false
  #removeNetworkListeners?: () => void

  constructor(input: {
    api: PermissionApi
    taskApi: Pick<TaskApi, "controlCommand">
    options?: PermissionConsoleOptions
  }) {
    const options = input.options ?? {}
    this.#api = input.api
    this.#now = options.now ?? (() => new Date())
    this.#pollIntervalMs = boundedInterval(
      options.pollIntervalMs,
      2_000,
      300_000,
      5_000,
    )
    this.#requestTimeoutMs = boundedInterval(
      options.requestTimeoutMs,
      1_000,
      120_000,
      15_000,
    )
    this.#maximumRequests = boundedCount(
      options.maximumRequests,
      16,
      1_000,
      400,
    )
    this.#maximumReceipts = boundedCount(
      options.maximumReceipts,
      16,
      10_000,
      512,
    )
    this.#disabled = options.disabled === true
    this.#store = new PermissionConsoleStore({
      now: this.#now,
      maximumTimeline: options.maximumTimeline,
      disabled: this.#disabled,
    })
    this.#race = new PermissionResponseRace({
      now: this.#now,
      maximum: options.maximumAttempts,
    })
    this.#interventions = new PermissionInterventionLedger(input.taskApi, {
      now: this.#now,
      maximum: options.maximumInterventions,
    })
    this.#events = new PermissionEventReconciler({
      maximumEntries: options.maximumTimeline,
    })
    this.#sessions = new PermissionSessionRoster(this.#api, {
      now: this.#now,
    })
    this.#reconnect = new PermissionReconnectSupervisor({
      now: this.#now,
      pollIntervalMs: this.#pollIntervalMs,
    })
    this.#receiptLedger = new PermissionReceiptLedger({
      now: this.#now,
      maximum: this.#maximumReceipts,
    })
    this.#expiry = new PermissionExpirySupervisor({
      now: this.#now,
      maximum: this.#maximumRequests,
    })
    this.#permits = new PermissionPermitLifecycle({
      now: this.#now,
      maximum: this.#maximumReceipts,
    })
    this.#installNetworkListeners()
  }

  getSnapshot = (): PermissionConsoleSnapshot => this.#store.getSnapshot()

  subscribe = (listener: () => void): (() => void) =>
    this.#store.subscribe(listener)

  async bindTask(
    input: PermissionTaskBindingInput,
  ): Promise<PermissionConsoleSnapshot> {
    this.#assertAvailable()
    const bindingInput = normalizeBindingInput(input)
    const generation = ++this.#generation
    this.#bindingInput = bindingInput
    this.#reconnect.bind(generation)
    this.#stopTimer()
    this.#abort?.abort("Permission task binding changed.")
    this.#abort = new AbortController()
    this.#store.patch({
      phase: "binding",
      productMode: bindingInput.productMode ?? "interactive",
      binding: primaryBinding(bindingInput),
      bindings: sessionBindings(bindingInput),
      requests: [],
      rules: [],
      decisions: [],
      attempts: [],
      receipts: [],
      interventions: [],
      selectedRequestId: null,
      responseError: null,
      diagnostics: {
        generation,
        lastErrorCode: undefined,
        lastErrorMessage: undefined,
      },
    })
    this.#race.clear()
    this.#interventions.clear()
    this.#events.clear()
    this.#receiptLedger.clear()
    this.#expiry.clear()
    this.#permits.clear()
    try {
      const roster = await this.#sessions.bind(bindingInput, {
        signal: this.#abort.signal,
        timeoutMs: this.#requestTimeoutMs,
      })
      if (generation !== this.#generation) return this.getSnapshot()
      this.#store.patch({
        bindings: roster.readyBindings,
        diagnostics: {
          custodySessionCount: roster.readyBindings.length,
          custodyFailureCount: roster.failureCount,
        },
      })
      const snapshot = await this.refresh("bind")
      this.#scheduleRefresh()
      return snapshot
    } catch (error) {
      if (generation !== this.#generation) return this.getSnapshot()
      const reconnect = this.#reconnect.failure(error, {
        online: transportOnline(),
      })
      this.#publishError(error, "permission_session_bind_failed")
      this.#store.patch({
        diagnostics: {
          reconnectPhase: reconnect.phase,
          nextReconnectAt: reconnect.nextProbeAt,
        },
      })
      this.#scheduleRefresh()
      return this.getSnapshot()
    }
  }

  async refresh(
    reason: "bind" | "poll" | "reconnect" | "response" | "manual" = "manual",
  ): Promise<PermissionConsoleSnapshot> {
    this.#assertAvailable()
    if (!this.#bindingInput) return this.getSnapshot()
    if (this.#refresh) return this.#refresh
    const generation = this.#generation
    const startedAt = this.#now().toISOString()
    this.#store.patch({
      phase:
        reason === "reconnect"
          ? "reconnecting"
          : this.getSnapshot().phase === "ready"
            ? "ready"
            : "loading",
      diagnostics: {
        lastRefreshStartedAt: startedAt,
        generation,
      },
    })
    const pending = this.#refreshBindings(generation, reason)
    this.#refresh = pending
    try {
      return await pending
    } finally {
      if (this.#refresh === pending) this.#refresh = undefined
    }
  }

  select(requestId?: string): PermissionConsoleSnapshot {
    return this.#store.select(requestId)
  }

  ingestCanonicalEvents(
    events: readonly PermissionCanonicalEvent[],
  ): PermissionConsoleSnapshot {
    this.#assertAvailable()
    const binding = this.#bindingInput
    if (!binding) return this.getSnapshot()
    const reconciled = this.#events.ingest(events, {
      taskId: binding.taskId,
      runId: binding.runId,
    })
    const permits = this.#permits.ingest(events)
    return this.#store.patch({
      canonicalEvents: [
        ...reconciled.entries,
        ...this.#permits.timelineEntries(),
      ],
      diagnostics: {
        eventProjectionRevision: reconciled.revision,
        admittedPermissionEvents: reconciled.admitted,
        quarantinedPermissionEvents: reconciled.quarantines.length,
        issuedPermitCount: permits.issuedCount,
        consumedPermitCount: permits.consumedCount,
        permitConflictCount: permits.conflictCount,
      },
    })
  }

  async respond(
    options: PermissionRespondOptions,
  ): Promise<PermissionDecisionReceipt | PermissionInterventionRecord> {
    this.#assertAvailable()
    const snapshot = this.getSnapshot()
    const requestId = identifier(
      options.requestId,
      "permission request id",
    )
    const request = snapshot.requests.find(
      (candidate) => candidate.requestId === requestId,
    )
    if (!request) {
      throw permissionRuntimeError(
        "permission_request_not_found",
        `Permission request ${requestId} is not in the canonical projection.`,
      )
    }
    const bindingInput = this.#bindingInput
    if (!bindingInput) {
      throw permissionRuntimeError(
        "permission_session_unbound",
        "Permission console is not bound to a task session.",
      )
    }
    if (snapshot.productMode === "sealed") {
      const record = await this.#interventions.record({
        ...primaryBinding(bindingInput),
        requestId,
        action: options.effect === "allow" ? "approve" : "deny",
        actorId: options.displayResponder,
        productMode: "sealed",
        reason:
          "Sealed autonomous policy rejects human permission responses and enters deterministic recovery without a wait.",
        signal: options.signal,
      })
      this.#store.patch({
        interventions: this.#interventions.list(),
        phase: "ready",
        responseError: {
          code: record.reasonCode,
          message: record.reason,
          requestId,
          retryable: false,
        },
      })
      return record
    }

    const draft = createPermissionResponseDraft(request, {
      effect: options.effect,
      displayResponder: options.displayResponder,
      feedback: options.feedback,
      now: this.#now(),
      responseId: freshPermissionId(
        "request_permission_response",
        this.#now(),
      ),
    })
    const created = this.#race.create({
      requestId,
      responseId: draft.responseId,
      effect: draft.effect,
      source: options.source,
      displayResponder: draft.displayResponder,
    })
    const claim = this.#race.claim(created.attemptId)
    this.#publishAttempts("responding")
    if (!claim.won) {
      throw permissionRuntimeError(
        "permission_response_race_lost",
        claim.attempt.errorMessage
          ?? "Another response already owns this permission request.",
      )
    }
    try {
      const proof = await createPermissionResponseProof(draft, this.#now())
      if (!(await verifyLocalPermissionResponseProof(request, proof))) {
        throw permissionRuntimeError(
          "permission_response_local_proof_failed",
          "Local permission response proof verification failed.",
        )
      }
      this.#race.proofReady(created.attemptId)
      this.#race.submitted(created.attemptId)
      this.#publishAttempts("responding")
      const body = await this.#api.resolve(request, {
        requestId,
        responseId: draft.responseId,
        effect: draft.effect,
        consoleResponse: { ...proof },
        displayResponder: draft.displayResponder,
        feedback: draft.feedback,
        signal: options.signal,
        timeoutMs: this.#requestTimeoutMs,
      })
      const projectedReceipt = projectPermissionReceipt(body, {
        requestId,
        responseId: draft.responseId,
        effect: draft.effect,
        receivedAt: this.#now(),
      })
      const admission = this.#receiptLedger.admit({
        receipt: projectedReceipt,
        request,
        proof,
      })
      this.#permits.issue(admission)
      const receipt = admission.receipt
      const accepted = this.#race.accept(created.attemptId, receipt)
      const receipts = boundedAppend(
        this.getSnapshot().receipts,
        receipt,
        this.#maximumReceipts,
      )
      this.#store.patch({
        attempts: this.#race.list(),
        receipts,
        responseError: accepted.phase === "accepted"
          ? null
          : {
              code:
                accepted.errorCode
                ?? "permission_response_not_accepted",
              message:
                accepted.errorMessage
                ?? "Canonical permission owner rejected the response.",
              requestId,
              retryable: false,
            },
      })
      await this.refresh("response")
      return receipt
    } catch (error) {
      const attempt = this.#race.get(created.attemptId)
      if (attempt && !terminalAttempt(attempt)) {
        this.#race.reject(
          created.attemptId,
          errorCode(error, "permission_response_failed"),
          errorMessage(error),
        )
      }
      this.#publishAttempts("error", error, requestId)
      throw error
    }
  }

  async recordSealedAction(input: {
    action: "steer" | "retry" | "mode_change"
    actorId: string
    requestId?: string
    reason?: string
    signal?: AbortSignal
  }): Promise<PermissionInterventionRecord> {
    this.#assertAvailable()
    const binding = this.#bindingInput
    if (!binding) {
      throw permissionRuntimeError(
        "permission_session_unbound",
        "Permission console is not bound to a task session.",
      )
    }
    if (this.getSnapshot().productMode !== "sealed") {
      throw permissionRuntimeError(
        "permission_intervention_not_sealed",
        "Only sealed permission interventions use the rejection ledger.",
      )
    }
    const record = await this.#interventions.record({
      ...primaryBinding(binding),
      requestId: input.requestId,
      action: input.action,
      actorId: input.actorId,
      productMode: "sealed",
      reason: input.reason,
      signal: input.signal,
    })
    this.#store.patch({
      interventions: this.#interventions.list(),
      responseError: {
        code: record.reasonCode,
        message: record.reason,
        requestId: input.requestId,
        retryable: false,
      },
    })
    return record
  }

  async expireNow(): Promise<PermissionConsoleSnapshot> {
    this.#assertAvailable()
    const input = this.#bindingInput
    if (!input) return this.getSnapshot()
    for (const binding of this.#sessions.readyBindings()) {
      await this.#api.expire(binding, {
        timeoutMs: this.#requestTimeoutMs,
      })
    }
    return this.refresh("manual")
  }

  close(reason = "Permission console closed."): void {
    if (this.#closed) return
    this.#closed = true
    this.#generation += 1
    this.#stopTimer()
    this.#abort?.abort(reason)
    this.#removeNetworkListeners?.()
    this.#removeNetworkListeners = undefined
    this.#race.clear()
    this.#interventions.clear()
    this.#events.clear()
    this.#receiptLedger.clear()
    this.#expiry.close()
    this.#permits.clear()
    this.#sessions.close()
    this.#reconnect.close()
    this.#store.close()
  }

  async #refreshBindings(
    generation: number,
    reason: string,
  ): Promise<PermissionConsoleSnapshot> {
    const input = this.#bindingInput
    if (!input) return this.getSnapshot()
    const abort = this.#abort ?? new AbortController()
    this.#abort = abort
    try {
      if (reason === "reconnect") {
        const roster = await this.#sessions.resumeAll({
          signal: abort.signal,
          timeoutMs: this.#requestTimeoutMs,
        })
        this.#store.patch({
          bindings: roster.readyBindings,
          diagnostics: {
            custodySessionCount: roster.readyBindings.length,
            custodyFailureCount: roster.failureCount,
          },
        })
      }
      const readyBindings = this.#sessions.readyBindings()
      if (!readyBindings.length) {
        throw permissionRuntimeError(
          "permission_custody_unavailable",
          "No permission session custody is ready for canonical projection.",
        )
      }
      const projections = await Promise.all(
        readyBindings.map(async (binding) => {
          const body = await this.#api.summary(binding, {
            limit: this.#maximumRequests,
            signal: abort.signal,
            timeoutMs: this.#requestTimeoutMs,
          })
          return projectPermissionBackend(body, {
            now: this.#now(),
            productMode: input.productMode ?? "interactive",
            policyFrozen: input.productMode === "sealed",
            defaultPolicyHash: input.policyHash,
            defaultPolicyRevision: input.policyRevision,
            humanInterventionCount: input.humanInterventionCount,
          })
        }),
      )
      if (generation !== this.#generation) return this.getSnapshot()
      const merged = mergeProjections(projections, this.#maximumRequests)
      this.#refreshCount += 1
      const terminalIds = new Set(
        merged.requests
          .filter((request) => request.terminal)
          .map((request) => request.requestId),
      )
      for (const requestId of terminalIds) {
        this.#race.releaseRequest(
          requestId,
          "Canonical permission request settled during refresh.",
        )
      }
      const expiry = this.#expiry.reconcile(merged.requests)
      const reconnect = this.#reconnect.success()
      return this.#store.patch({
        phase: "ready",
        productMode: input.productMode ?? merged.mode.productMode,
        requests: merged.requests,
        mode: merged.mode,
        rules: merged.rules,
        decisions: merged.decisions,
        attempts: this.#race.list(),
        interventions: this.#interventions.list(),
        responseError: reason === "response" ? null : undefined,
        diagnostics: {
          refreshCount: this.#refreshCount,
          reconnectCount: this.#reconnectCount,
          lastRefreshCompletedAt: this.#now().toISOString(),
          lastErrorCode: undefined,
          lastErrorMessage: undefined,
          sourceOwner: merged.sourceOwner,
          sourceSchema: merged.sourceSchema,
          backendAuthoritative: true,
          localPendingStore: false,
          custodyInMemoryOnly: true,
          responseProofRequired: true,
          reconnectPhase: reconnect.phase,
          nextReconnectAt: reconnect.nextProbeAt,
          nextExpiryAt: expiry.nextExpiryAt,
          expiryClaimCount: expiry.claimCount,
        },
      })
    } catch (error) {
      if (generation !== this.#generation) return this.getSnapshot()
      const reconnect = this.#reconnect.failure(error, {
        online: transportOnline(),
      })
      this.#publishError(error, "permission_refresh_failed")
      this.#store.patch({
        diagnostics: {
          reconnectPhase: reconnect.phase,
          nextReconnectAt: reconnect.nextProbeAt,
        },
      })
      return this.getSnapshot()
    }
  }

  #publishAttempts(
    phase: "responding" | "error",
    error?: unknown,
    requestId?: string,
  ): void {
    this.#store.patch({
      phase,
      attempts: this.#race.list(),
      responseError:
        phase === "error"
          ? {
              code: errorCode(error, "permission_response_failed"),
              message: errorMessage(error),
              requestId,
              retryable: retryableError(error),
            }
          : null,
    })
  }

  #publishError(error: unknown, fallbackCode: string): void {
    const code = errorCode(error, fallbackCode)
    const message = errorMessage(error)
    this.#store.patch({
      phase: "error",
      responseError: {
        code,
        message,
        retryable: retryableError(error),
      },
      diagnostics: {
        lastErrorCode: code,
        lastErrorMessage: message,
      },
    })
  }

  #scheduleRefresh(): void {
    this.#stopTimer()
    if (this.#closed || this.#disabled || !this.#bindingInput) return
    const reconnectDelay = this.#reconnect.nextDelayMs()
    const expiryDelay = this.#expiry.nextDelayMs()
    const delays = [reconnectDelay, expiryDelay].filter(
      (value): value is number => value !== undefined,
    )
    if (!delays.length) return
    const delay = Math.min(...delays)
    this.#timer = setTimeout(() => {
      this.#timer = undefined
      const expiryClaim = this.#expiry.claimDue()
      if (expiryClaim) {
        void this.expireNow()
          .then(() => {
            this.#expiry.settle(expiryClaim.claimId, { accepted: true })
          })
          .catch(() => {
            this.#expiry.settle(expiryClaim.claimId, { accepted: false })
          })
          .finally(() => this.#scheduleRefresh())
        return
      }
      const prior = this.#reconnect.snapshot().phase
      const probeId = this.#reconnect.claimProbe()
      if (!probeId) {
        this.#scheduleRefresh()
        return
      }
      const reason =
        prior === "healthy" || prior === "idle"
          ? "poll"
          : "reconnect"
      if (reason === "reconnect") this.#reconnectCount += 1
      void this.refresh(reason).finally(() => this.#scheduleRefresh())
    }, delay)
  }

  #stopTimer(): void {
    if (this.#timer !== undefined) clearTimeout(this.#timer)
    this.#timer = undefined
  }

  #installNetworkListeners(): void {
    if (typeof window === "undefined") return
    const online = () => {
      if (!this.#bindingInput || this.#closed) return
      this.#reconnect.markOnline()
      this.#reconnectCount += 1
      void this.refresh("reconnect")
    }
    const offline = () => {
      if (!this.#bindingInput || this.#closed) return
      const reconnect = this.#reconnect.markOffline()
      this.#stopTimer()
      this.#store.patch({
        phase: "reconnecting",
        diagnostics: {
          lastErrorCode: "permission_transport_offline",
          lastErrorMessage:
            "Permission transport is offline; canonical state remains on the backend.",
          reconnectPhase: reconnect.phase,
          nextReconnectAt: reconnect.nextProbeAt,
        },
      })
    }
    window.addEventListener("online", online)
    window.addEventListener("offline", offline)
    this.#removeNetworkListeners = () => {
      window.removeEventListener("online", online)
      window.removeEventListener("offline", offline)
    }
  }

  #assertAvailable(): void {
    if (this.#closed) {
      throw permissionRuntimeError(
        "permission_console_closed",
        "Permission console is closed.",
      )
    }
    if (this.#disabled) {
      throw permissionRuntimeError(
        "permission_console_disabled",
        "Permission console is disabled.",
      )
    }
  }
}

function mergeProjections(
  values: readonly PermissionBackendProjection[],
  maximumRequests: number,
): PermissionBackendProjection {
  const first = values[0]
  if (!first) {
    throw permissionRuntimeError(
      "permission_projection_empty",
      "Permission backend returned no session projections.",
    )
  }
  const requests = new Map(
    values
      .flatMap((value) => value.requests)
      .map((request) => [request.requestId, request] as const),
  )
  const rules = new Map(
    values
      .flatMap((value) => value.rules)
      .map((rule) => [rule.ruleId, rule] as const),
  )
  const decisions = new Map(
    values
      .flatMap((value) => value.decisions)
      .map((decision) => [decision.decisionId, decision] as const),
  )
  return Object.freeze({
    requests: Object.freeze(
      [...requests.values()]
        .sort(
          (left, right) =>
            left.createdAt.localeCompare(right.createdAt)
            || left.requestId.localeCompare(right.requestId),
        )
        .slice(-maximumRequests),
    ),
    rules: Object.freeze([...rules.values()]),
    decisions: Object.freeze([...decisions.values()]),
    mode: first.mode,
    sourceOwner: first.sourceOwner,
    sourceSchema: first.sourceSchema,
  })
}

function normalizeBindingInput(
  input: PermissionTaskBindingInput,
): PermissionTaskBindingInput {
  const taskId = identifier(input.taskId, "permission task id")
  const runId = identifier(input.runId, "permission run id")
  const sessionId = identifier(input.sessionId, "permission session id")
  const additional = [
    ...new Set(
      (input.additionalSessionIds ?? [])
        .map((value) => identifier(value, "permission session id"))
        .filter((value) => value !== sessionId),
    ),
  ]
  return Object.freeze({
    ...input,
    taskId,
    runId,
    sessionId,
    additionalSessionIds: Object.freeze(additional),
    custodyTokens: input.custodyTokens
      ? Object.freeze({ ...input.custodyTokens })
      : undefined,
    productMode: input.productMode ?? "interactive",
  })
}

function primaryBinding(
  input: PermissionTaskBindingInput,
): PermissionSessionBinding {
  return Object.freeze({
    taskId: input.taskId,
    runId: input.runId,
    sessionId: input.sessionId,
  })
}

function sessionBindings(
  input: PermissionTaskBindingInput,
): PermissionSessionBinding[] {
  return [
    input.sessionId,
    ...(input.additionalSessionIds ?? []),
  ].map((sessionId) =>
    Object.freeze({
      taskId: input.taskId,
      runId: input.runId,
      sessionId,
    }),
  )
}

function terminalAttempt(attempt: PermissionResponseAttempt): boolean {
  return [
    "accepted",
    "rejected",
    "lost_race",
    "cancelled",
  ].includes(attempt.phase)
}

function boundedAppend<T>(
  values: readonly T[],
  value: T,
  maximum: number,
): readonly T[] {
  return Object.freeze([...values, value].slice(-maximum))
}

function boundedInterval(
  value: number | undefined,
  minimum: number,
  maximum: number,
  fallback: number,
): number {
  if (value === undefined) return fallback
  if (!Number.isFinite(value)) return fallback
  return Math.min(maximum, Math.max(minimum, Math.floor(value)))
}

function boundedCount(
  value: number | undefined,
  minimum: number,
  maximum: number,
  fallback: number,
): number {
  return boundedInterval(value, minimum, maximum, fallback)
}

function errorCode(error: unknown, fallback: string): string {
  if (!error || typeof error !== "object") return fallback
  const code = (error as Record<string, unknown>).code
  return typeof code === "string" && code.trim()
    ? code.trim().slice(0, 256)
    : fallback
}

function errorMessage(error: unknown): string {
  const message =
    error instanceof Error
      ? error.message
      : String(error || "Permission operation failed.")
  return message.slice(0, 2_000)
}

function retryableError(error: unknown): boolean {
  if (!error || typeof error !== "object") return true
  const status = Number((error as Record<string, unknown>).status ?? 0)
  const code = errorCode(error, "")
  if ([400, 401, 403, 404, 409, 410, 422].includes(status)) return false
  return ![
    "permission_request_expired",
    "permission_request_stale",
    "permission_response_proof_invalid",
    "permission_response_identity_mismatch",
  ].includes(code)
}

function permissionRuntimeError(code: string, message: string): Error {
  return Object.assign(new Error(message), {
    name: "PermissionConsoleRuntimeError",
    code,
  })
}

function transportOnline(): boolean {
  return typeof navigator === "undefined" || navigator.onLine !== false
}
