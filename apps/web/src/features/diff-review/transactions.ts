import type { TaskApi } from "../../api/task-api.ts"
import {
  freezeApplyRequest,
  parsePatchTransactionReceipt,
  parseReviewReceipt,
  transactionCanRetry,
  type DiffManifestContract,
  type DiffReviewLineSelection,
  type DiffReviewReceipt,
  type PatchApplyRequest,
  type PatchFilePrecondition,
  type PatchRollbackRequest,
  type PatchTransactionReceipt,
} from "./contracts.ts"
import {
  assertPreflightSet,
  type HashlinePreflightReceipt,
} from "./hashline.ts"

export type PatchTransactionRuntimePhase =
  | "idle"
  | "submitting-review"
  | "preparing"
  | "applying"
  | "permission-pending"
  | "committed"
  | "rolling-back"
  | "rolled-back"
  | "rejected"
  | "failed"
  | "disconnected"
  | "closed"

export interface PatchTransactionRuntimeState {
  phase: PatchTransactionRuntimePhase
  taskId: string
  runId: string
  diffId: string
  activeRequest?: PatchApplyRequest | PatchRollbackRequest
  latestReceipt?: PatchTransactionReceipt
  receipts: readonly PatchTransactionReceipt[]
  reviewReceipts: readonly DiffReviewReceipt[]
  pendingPermissionRequestId?: string
  pendingPermissionPermitId?: string
  error?: PatchTransactionFailure
  generation: number
  updatedAt: number
}

export interface PatchTransactionFailure {
  code: string
  message: string
  retryable: boolean
  disconnected: boolean
  transactionId?: string
  occurredAt: number
}

export interface PatchApplyIdentity {
  actorId: string
  sessionId: string
  sessionRevision: number
  workerRequestId: string
  sealed: boolean
}

export interface PatchApplyInput {
  manifest: DiffManifestContract
  selectedFileIds: readonly string[]
  preflightReceipts: readonly HashlinePreflightReceipt[]
  reviewRevision: number
  identity: PatchApplyIdentity
  idempotencyKey?: string
  causationId?: string
  permissionPermitId?: string
  signal?: AbortSignal
}

export interface PatchRollbackInput {
  receipt: PatchTransactionReceipt
  identity: PatchApplyIdentity
  idempotencyKey?: string
  causationId?: string
  permissionPermitId?: string
  signal?: AbortSignal
}

export interface ReviewCommentInput {
  manifest: DiffManifestContract
  action: "select" | "comment" | "comment_update" | "comment_resolve"
  selection: DiffReviewLineSelection
  body?: string
  commentId?: string
  expectedRevision: number
  actorId: string
  causationId?: string
  sealed?: boolean
  permissionPermitId?: string
  signal?: AbortSignal
}

export type PatchTransactionListener = (
  state: PatchTransactionRuntimeState,
) => void

export class PatchTransactionRuntimeError extends Error {
  readonly code: string
  readonly retryable: boolean
  readonly details: Readonly<Record<string, unknown>>

  constructor(
    code: string,
    message: string,
    retryable = false,
    details: Readonly<Record<string, unknown>> = {},
  ) {
    super(message)
    this.name = "PatchTransactionRuntimeError"
    this.code = code
    this.retryable = retryable
    this.details = Object.freeze({ ...details })
  }
}

function cleanIdentity(value: string, name: string): string {
  const selected = String(value || "").trim()
  if (!selected || selected.length > 512 || /[\u0000\r\n]/.test(selected)) {
    throw new PatchTransactionRuntimeError(
      "patch_transaction_identity_invalid",
      `${name} is not a valid identity.`,
    )
  }
  return selected
}

function nonNegativeInteger(value: number, name: string): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new PatchTransactionRuntimeError(
      "patch_transaction_integer_invalid",
      `${name} must be a non-negative safe integer.`,
    )
  }
  return value
}

function fnv(value: string): string {
  let high = 0x811c9dc5
  let low = 0x01000193
  for (let index = 0; index < value.length; index += 1) {
    high ^= value.charCodeAt(index)
    high = Math.imul(high, 0x01000193)
    low ^= value.charCodeAt(value.length - index - 1)
    low = Math.imul(low, 0x85ebca6b)
  }
  return `${(high >>> 0).toString(16).padStart(8, "0")}${(low >>> 0)
    .toString(16)
    .padStart(8, "0")}`
}

function stableIdentity(
  kind: string,
  values: Readonly<Record<string, unknown>>,
): string {
  return `${kind}:${fnv(JSON.stringify(values, Object.keys(values).sort()))}`
}

function uniqueFileIds(values: readonly string[]): readonly string[] {
  const result: string[] = []
  const seen = new Set<string>()
  for (const value of values) {
    const fileId = cleanIdentity(value, "fileId")
    if (seen.has(fileId)) {
      throw new PatchTransactionRuntimeError(
        "patch_transaction_duplicate_file",
        `Patch selection repeats file ${fileId}.`,
      )
    }
    seen.add(fileId)
    result.push(fileId)
  }
  if (!result.length) {
    throw new PatchTransactionRuntimeError(
      "patch_transaction_empty_selection",
      "Patch apply requires at least one selected file.",
    )
  }
  return Object.freeze(result)
}

function applyRequest(input: PatchApplyInput): PatchApplyRequest {
  const manifest = input.manifest
  const selectedFileIds = uniqueFileIds(input.selectedFileIds)
  const files = selectedFileIds.map((fileId) => {
    const file = manifest.files.find((candidate) => candidate.fileId === fileId)
    if (!file) {
      throw new PatchTransactionRuntimeError(
        "patch_transaction_unknown_file",
        `Selected file ${fileId} is not in the diff manifest.`,
      )
    }
    if (file.binary) {
      throw new PatchTransactionRuntimeError(
        "patch_transaction_binary_rejected",
        `Binary file ${file.path} cannot use text patch apply.`,
      )
    }
    return file
  })
  const preconditions = assertPreflightSet(files, input.preflightReceipts)
  const actorId = cleanIdentity(input.identity.actorId, "actorId")
  const sessionId = cleanIdentity(input.identity.sessionId, "sessionId")
  const workerRequestId = cleanIdentity(
    input.identity.workerRequestId,
    "workerRequestId",
  )
  const causationId = input.causationId
    ? cleanIdentity(input.causationId, "causationId")
    : stableIdentity("diff-apply-cause", {
      taskId: manifest.source.taskId,
      diffId: manifest.diffId,
      reviewRevision: input.reviewRevision,
      files: selectedFileIds.join(","),
    })
  const idempotencyKey = input.idempotencyKey
    ? cleanIdentity(input.idempotencyKey, "idempotencyKey")
    : stableIdentity("diff-apply-idempotency", {
      taskId: manifest.source.taskId,
      diffId: manifest.diffId,
      artifactRevision: manifest.source.artifactRevision,
      reviewRevision: input.reviewRevision,
      files: selectedFileIds.join(","),
      preconditions: preconditions
        .map((item) =>
          `${item.fileId}:${item.currentSha256}:${item.proposedSha256}`)
        .join("|"),
    })
  const toolCallId = stableIdentity("diff-apply-tool", {
    idempotencyKey,
    causationId,
  })
  return freezeApplyRequest({
    schema: "zyra.patch-review-apply.v1",
    taskId: manifest.source.taskId,
    runId: manifest.source.runId,
    diffId: manifest.diffId,
    artifactId: manifest.source.artifactId,
    artifactRevision: manifest.source.artifactRevision,
    workspaceId: manifest.source.workspaceId,
    idempotencyKey,
    causationId,
    actorId,
    sessionId,
    sessionRevision: nonNegativeInteger(
      input.identity.sessionRevision,
      "sessionRevision",
    ),
    workerRequestId,
    toolCallId,
    expectedOwnerEpoch: manifest.source.ownerEpoch,
    expectedBindingRevision: manifest.source.bindingRevision,
    expectedLeaseId: manifest.source.leaseId,
    selectedFileIds,
    preconditions,
    sealed: Boolean(input.identity.sealed),
    reviewRevision: nonNegativeInteger(
      input.reviewRevision,
      "reviewRevision",
    ),
    permissionPermitId: input.permissionPermitId,
  })
}

function rollbackRequest(input: PatchRollbackInput): PatchRollbackRequest {
  const receipt = input.receipt
  if (!receipt.transactionId || !receipt.snapshotId) {
    throw new PatchTransactionRuntimeError(
      "patch_rollback_reference_missing",
      "Rollback requires a committed transaction and snapshot reference.",
    )
  }
  if (!receipt.committed) {
    throw new PatchTransactionRuntimeError(
      "patch_rollback_not_committed",
      "Only a committed patch transaction can be rolled back.",
    )
  }
  const causationId = input.causationId
    ? cleanIdentity(input.causationId, "causationId")
    : stableIdentity("diff-rollback-cause", {
      transactionId: receipt.transactionId,
      snapshotId: receipt.snapshotId,
    })
  const idempotencyKey = input.idempotencyKey
    ? cleanIdentity(input.idempotencyKey, "idempotencyKey")
    : stableIdentity("diff-rollback-idempotency", {
      transactionId: receipt.transactionId,
      snapshotId: receipt.snapshotId,
      taskId: receipt.taskId,
    })
  return Object.freeze({
    schema: "zyra.patch-review-rollback.v1",
    taskId: receipt.taskId,
    runId: receipt.runId,
    diffId: receipt.diffId,
    transactionId: receipt.transactionId,
    snapshotId: receipt.snapshotId,
    idempotencyKey,
    causationId,
    actorId: cleanIdentity(input.identity.actorId, "actorId"),
    sessionId: cleanIdentity(input.identity.sessionId, "sessionId"),
    sessionRevision: nonNegativeInteger(
      input.identity.sessionRevision,
      "sessionRevision",
    ),
    workerRequestId: cleanIdentity(
      input.identity.workerRequestId,
      "workerRequestId",
    ),
    toolCallId: stableIdentity("diff-rollback-tool", {
      idempotencyKey,
      causationId,
    }),
    sealed: Boolean(input.identity.sealed),
    permissionPermitId: input.permissionPermitId,
  })
}

function failureFrom(error: unknown): PatchTransactionFailure {
  if (error instanceof PatchTransactionRuntimeError) {
    return Object.freeze({
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      disconnected: false,
      occurredAt: Date.now(),
    })
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return Object.freeze({
      code: "patch_transaction_cancelled",
      message: error.message || "Patch transaction was cancelled.",
      retryable: true,
      disconnected: false,
      occurredAt: Date.now(),
    })
  }
  const candidate = error as {
    code?: unknown
    message?: unknown
    status?: unknown
  }
  const message = error instanceof Error
    ? error.message
    : String(candidate?.message || error || "Unknown patch transaction failure.")
  const status = typeof candidate?.status === "number" ? candidate.status : 0
  const disconnected =
    status === 0
    || status === 503
    || /disabled|unavailable|disconnect|network|fetch/i.test(message)
  return Object.freeze({
    code: String(candidate?.code || (disconnected
      ? "patch_transaction_disconnected"
      : "patch_transaction_failed")),
    message,
    retryable: disconnected || status === 408 || status === 429,
    disconnected,
    occurredAt: Date.now(),
  })
}

function runtimePhase(receipt: PatchTransactionReceipt): PatchTransactionRuntimePhase {
  if (receipt.phase === "permission_pending") return "permission-pending"
  if (receipt.phase === "committed") return "committed"
  if (receipt.phase === "rolled_back") return "rolled-back"
  if (
    receipt.phase === "permission_denied"
    || receipt.phase === "stale"
    || receipt.phase === "conflicted"
    || receipt.phase === "quarantined"
  ) {
    return "rejected"
  }
  if (receipt.phase === "rollback_failed") return "failed"
  return "failed"
}

function freezeState(
  state: PatchTransactionRuntimeState,
): PatchTransactionRuntimeState {
  return Object.freeze({
    ...state,
    receipts: Object.freeze([...state.receipts]),
    reviewReceipts: Object.freeze([...state.reviewReceipts]),
  })
}

export class PatchReceiptJournal {
  readonly #receipts = new Map<string, PatchTransactionReceipt>()
  readonly #byIdempotency = new Map<string, string>()
  readonly #byTransaction = new Map<string, string>()
  #maximum = 2_000

  admit(
    receipt: PatchTransactionReceipt,
    idempotencyKey: string,
  ): { receipt: PatchTransactionReceipt; replay: boolean } {
    const existing = this.#receipts.get(receipt.receiptId)
    if (existing) {
      if (
        existing.transactionId !== receipt.transactionId
        || existing.phase !== receipt.phase
        || existing.causationId !== receipt.causationId
      ) {
        throw new PatchTransactionRuntimeError(
          "patch_receipt_identity_conflict",
          "Patch receipt identity was reused with different content.",
        )
      }
      return { receipt: existing, replay: true }
    }
    const idempotentReceiptId = this.#byIdempotency.get(idempotencyKey)
    if (idempotentReceiptId) {
      const prior = this.#receipts.get(idempotentReceiptId)
      if (
        prior
        && prior.transactionId !== receipt.transactionId
        && !receipt.idempotentReplay
      ) {
        throw new PatchTransactionRuntimeError(
          "patch_receipt_idempotency_conflict",
          "Idempotency key resolved to a different patch transaction.",
          false,
          {
            prior: prior.transactionId,
            current: receipt.transactionId,
          },
        )
      }
    }
    const transactionReceiptId = this.#byTransaction.get(receipt.transactionId)
    if (transactionReceiptId) {
      const prior = this.#receipts.get(transactionReceiptId)
      if (
        prior
        && prior.phase === "committed"
        && receipt.phase !== "committed"
        && receipt.phase !== "rolled_back"
      ) {
        throw new PatchTransactionRuntimeError(
          "patch_receipt_terminal_regression",
          "Committed patch transaction regressed to a non-terminal phase.",
        )
      }
    }
    this.#receipts.set(receipt.receiptId, receipt)
    this.#byIdempotency.set(idempotencyKey, receipt.receiptId)
    this.#byTransaction.set(receipt.transactionId, receipt.receiptId)
    this.#trim()
    return { receipt, replay: false }
  }

  byTransaction(transactionId: string): PatchTransactionReceipt | undefined {
    const receiptId = this.#byTransaction.get(transactionId)
    return receiptId ? this.#receipts.get(receiptId) : undefined
  }

  list(): readonly PatchTransactionReceipt[] {
    return Object.freeze([...this.#receipts.values()])
  }

  clear(): void {
    this.#receipts.clear()
    this.#byIdempotency.clear()
    this.#byTransaction.clear()
  }

  #trim(): void {
    while (this.#receipts.size > this.#maximum) {
      const oldest = this.#receipts.keys().next().value as string | undefined
      if (!oldest) break
      const receipt = this.#receipts.get(oldest)
      this.#receipts.delete(oldest)
      if (receipt) {
        if (this.#byTransaction.get(receipt.transactionId) === oldest) {
          this.#byTransaction.delete(receipt.transactionId)
        }
        for (const [key, receiptId] of this.#byIdempotency) {
          if (receiptId === oldest) this.#byIdempotency.delete(key)
        }
      }
    }
  }
}

export class PatchTransactionRuntime {
  readonly #api: TaskApi
  readonly #taskId: string
  readonly #runId: string
  readonly #diffId: string
  readonly #journal = new PatchReceiptJournal()
  readonly #listeners = new Set<PatchTransactionListener>()
  readonly #reviewReceiptIds = new Set<string>()
  #state: PatchTransactionRuntimeState
  #controller?: AbortController
  #closed = false

  constructor(input: {
    api: TaskApi
    taskId: string
    runId: string
    diffId: string
  }) {
    this.#api = input.api
    this.#taskId = cleanIdentity(input.taskId, "taskId")
    this.#runId = cleanIdentity(input.runId, "runId")
    this.#diffId = cleanIdentity(input.diffId, "diffId")
    this.#state = freezeState({
      phase: "idle",
      taskId: this.#taskId,
      runId: this.#runId,
      diffId: this.#diffId,
      receipts: [],
      reviewReceipts: [],
      generation: 0,
      updatedAt: Date.now(),
    })
  }

  get state(): PatchTransactionRuntimeState {
    return this.#state
  }

  listen(listener: PatchTransactionListener): () => void {
    this.#requireOpen()
    this.#listeners.add(listener)
    listener(this.#state)
    return () => this.#listeners.delete(listener)
  }

  async submitReview(
    input: ReviewCommentInput,
  ): Promise<DiffReviewReceipt> {
    this.#requireOpen()
    this.#assertManifest(input.manifest)
    const causationId = input.causationId
      ? cleanIdentity(input.causationId, "causationId")
      : stableIdentity("diff-review-cause", {
        diffId: this.#diffId,
        action: input.action,
        fileId: input.selection.fileId,
        hunkId: input.selection.hunkId,
        revision: input.expectedRevision,
      })
    this.#commit({ phase: "submitting-review", error: undefined })
    try {
      const raw = await this.#api.diffReviewComment(
        this.#taskId,
        input.manifest.source.artifactId,
        {
          revision: input.manifest.source.artifactRevision,
          diffId: this.#diffId,
          action: input.action,
          selection: {
            file_id: input.selection.fileId,
            hunk_id: input.selection.hunkId,
            side: input.selection.side,
            start_line: input.selection.startLine,
            end_line: input.selection.endLine,
            anchor_line_ids: input.selection.anchorLineIds,
          },
          body: String(input.body || ""),
          commentId: input.commentId,
          expectedReviewRevision: input.expectedRevision,
          actorId: cleanIdentity(input.actorId, "actorId"),
          causationId,
          sealed: Boolean(input.sealed),
          permissionPermitId: input.permissionPermitId,
          signal: input.signal,
        },
      )
      const receipt = parseReviewReceipt(raw)
      if (
        receipt.taskId !== this.#taskId
        || receipt.diffId !== this.#diffId
        || receipt.causationId !== causationId
      ) {
        throw new PatchTransactionRuntimeError(
          "diff_review_receipt_binding",
          "Review receipt identity does not match its request.",
        )
      }
      if (!receipt.accepted) {
        throw new PatchTransactionRuntimeError(
          receipt.permission.effect === "ask"
            ? "diff_review_permission_pending"
            : "diff_review_permission_denied",
          receipt.permission.effect === "ask"
            ? "Review mutation is waiting for an exact-call permission permit."
            : "Review mutation was denied before canonical event state changed.",
          receipt.permission.effect === "ask",
          {
            requestId: receipt.permission.requestId,
            decisionId: receipt.permission.decisionId,
            reasonCode: receipt.permission.reasonCode,
            deniedManualMutationCount: receipt.deniedManualMutationCount,
          },
        )
      }
      if (!this.#reviewReceiptIds.has(receipt.receiptId)) {
        this.#reviewReceiptIds.add(receipt.receiptId)
        this.#commit({
          phase: "idle",
          reviewReceipts: [
            ...this.#state.reviewReceipts,
            receipt,
          ].slice(-2_000),
          error: undefined,
        })
      } else {
        this.#commit({ phase: "idle", error: undefined })
      }
      return receipt
    } catch (error) {
      const failure = failureFrom(error)
      this.#commit({
        phase: failure.disconnected ? "disconnected" : "failed",
        error: failure,
      })
      throw error
    }
  }

  async apply(input: PatchApplyInput): Promise<PatchTransactionReceipt> {
    this.#requireOpen()
    this.#assertManifest(input.manifest)
    const request = applyRequest(input)
    return this.#executeApply(request, input.signal)
  }

  async retryPermission(
    permitId: string,
    signal?: AbortSignal,
  ): Promise<PatchTransactionReceipt> {
    this.#requireOpen()
    const request = this.#state.activeRequest
    const receipt = this.#state.latestReceipt
    if (
      !request
      || request.schema !== "zyra.patch-review-apply.v1"
      || !receipt
      || receipt.phase !== "permission_pending"
      || !transactionCanRetry(receipt)
    ) {
      throw new PatchTransactionRuntimeError(
        "patch_permission_retry_unavailable",
        "No permission-pending patch request can be retried.",
      )
    }
    const next = Object.freeze({
      ...request,
      permissionPermitId: cleanIdentity(permitId, "permissionPermitId"),
    })
    return this.#executeApply(next, signal)
  }

  async rollback(input: PatchRollbackInput): Promise<PatchTransactionReceipt> {
    this.#requireOpen()
    const request = rollbackRequest(input)
    this.#controller?.abort("Patch rollback superseded.")
    const controller = new AbortController()
    this.#controller = controller
    const dispose = connectSignal(controller, input.signal)
    this.#commit({
      phase: "rolling-back",
      activeRequest: request,
      error: undefined,
    })
    try {
      const raw = await this.#api.diffReviewRollback(
        this.#taskId,
        request.transactionId,
        request,
        {
          signal: controller.signal,
          idempotencyKey: request.idempotencyKey,
          causationId: request.causationId,
        },
      )
      const parsed = parsePatchTransactionReceipt(raw, {
        taskId: this.#taskId,
        runId: this.#runId,
        diffId: this.#diffId,
        causationId: request.causationId,
      })
      const admitted = this.#journal.admit(parsed, request.idempotencyKey)
      this.#commit({
        phase: runtimePhase(admitted.receipt),
        latestReceipt: admitted.receipt,
        receipts: this.#journal.list(),
        pendingPermissionRequestId: admitted.receipt.permission.requestId,
        pendingPermissionPermitId: admitted.receipt.permission.permitId,
        error: undefined,
      })
      return admitted.receipt
    } catch (error) {
      const failure = failureFrom(error)
      this.#commit({
        phase: failure.disconnected ? "disconnected" : "failed",
        error: failure,
      })
      throw error
    } finally {
      dispose()
      if (this.#controller === controller) this.#controller = undefined
    }
  }

  cancel(reason = "Patch transaction cancelled."): void {
    this.#controller?.abort(reason)
    this.#controller = undefined
    this.#commit({
      phase: "idle",
      activeRequest: undefined,
      error: undefined,
    })
  }

  close(reason = "Patch transaction runtime closed."): void {
    if (this.#closed) return
    this.#closed = true
    this.#controller?.abort(reason)
    this.#controller = undefined
    this.#listeners.clear()
    this.#journal.clear()
    this.#state = freezeState({
      ...this.#state,
      phase: "closed",
      activeRequest: undefined,
      generation: this.#state.generation + 1,
      updatedAt: Date.now(),
    })
  }

  async #executeApply(
    request: PatchApplyRequest,
    signal?: AbortSignal,
  ): Promise<PatchTransactionReceipt> {
    this.#controller?.abort("Patch apply superseded.")
    const controller = new AbortController()
    this.#controller = controller
    const dispose = connectSignal(controller, signal)
    this.#commit({
      phase: "applying",
      activeRequest: request,
      error: undefined,
    })
    try {
      const raw = await this.#api.diffReviewApply(
        this.#taskId,
        request.artifactId,
        request,
        {
          signal: controller.signal,
          idempotencyKey: request.idempotencyKey,
          causationId: request.causationId,
        },
      )
      const parsed = parsePatchTransactionReceipt(raw, {
        taskId: this.#taskId,
        runId: this.#runId,
        diffId: this.#diffId,
        causationId: request.causationId,
      })
      const admitted = this.#journal.admit(parsed, request.idempotencyKey)
      this.#commit({
        phase: runtimePhase(admitted.receipt),
        latestReceipt: admitted.receipt,
        receipts: this.#journal.list(),
        pendingPermissionRequestId: admitted.receipt.permission.requestId,
        pendingPermissionPermitId: admitted.receipt.permission.permitId,
        error: undefined,
      })
      return admitted.receipt
    } catch (error) {
      const failure = failureFrom(error)
      this.#commit({
        phase: failure.disconnected ? "disconnected" : "failed",
        error: failure,
      })
      throw error
    } finally {
      dispose()
      if (this.#controller === controller) this.#controller = undefined
    }
  }

  #assertManifest(manifest: DiffManifestContract): void {
    if (
      manifest.source.taskId !== this.#taskId
      || manifest.source.runId !== this.#runId
      || manifest.diffId !== this.#diffId
    ) {
      throw new PatchTransactionRuntimeError(
        "patch_transaction_manifest_binding",
        "Patch manifest does not match the transaction runtime.",
      )
    }
  }

  #commit(patch: Partial<PatchTransactionRuntimeState>): void {
    this.#state = freezeState({
      ...this.#state,
      ...patch,
      generation: this.#state.generation + 1,
      updatedAt: Date.now(),
    })
    for (const listener of this.#listeners) listener(this.#state)
  }

  #requireOpen(): void {
    if (this.#closed) {
      throw new PatchTransactionRuntimeError(
        "patch_transaction_runtime_closed",
        "Patch transaction runtime is closed.",
      )
    }
  }
}

function connectSignal(
  controller: AbortController,
  signal: AbortSignal | undefined,
): () => void {
  if (!signal) return () => undefined
  if (signal.aborted) {
    controller.abort(signal.reason)
    return () => undefined
  }
  const abort = () => controller.abort(signal.reason)
  signal.addEventListener("abort", abort, { once: true })
  return () => signal.removeEventListener("abort", abort)
}

export function preconditionsByFile(
  preconditions: readonly PatchFilePrecondition[],
): ReadonlyMap<string, PatchFilePrecondition> {
  const result = new Map<string, PatchFilePrecondition>()
  for (const precondition of preconditions) {
    if (result.has(precondition.fileId)) {
      throw new PatchTransactionRuntimeError(
        "patch_transaction_duplicate_precondition",
        `Patch preconditions repeat file ${precondition.fileId}.`,
      )
    }
    result.set(precondition.fileId, precondition)
  }
  return result
}
