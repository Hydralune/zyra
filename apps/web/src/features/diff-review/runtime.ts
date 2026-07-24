import type { TaskApi } from "../../api/task-api.ts"
import {
  parseArtifactCatalogPage,
  type ArtifactContract,
} from "../artifacts/contracts.ts"
import {
  parseDiffFileContent,
  type DiffFileContentContract,
  type DiffHunkContract,
  type DiffManifestContract,
  type DiffReviewLineSelection,
  type PatchTransactionReceipt,
} from "./contracts.ts"
import {
  DiffFetchRuntime,
  type DiffFetchFailure,
  type DiffFetchRuntimeOptions,
  type DiffFetchState,
  type DiffFileLoadState,
} from "./fetch-runtime.ts"
import {
  DiffSnapshotStore,
  preflightHashlinePatch,
  type HashlinePreflightReceipt,
} from "./hashline.ts"
import {
  DiffReviewModel,
  type DiffReviewModelState,
  type DiffReviewPersistence,
} from "./review-model.ts"
import {
  DiffSearchIndex,
  type DiffSearchOptions,
  type DiffSearchResult,
} from "./search.ts"
import {
  PatchTransactionRuntime,
  type PatchApplyIdentity,
  type PatchTransactionRuntimeState,
} from "./transactions.ts"

export type DiffReviewWorkbenchPhase =
  | "idle"
  | "catalog-loading"
  | "catalog-ready"
  | "manifest-loading"
  | "ready"
  | "loading"
  | "preflighting"
  | "permission-pending"
  | "applying"
  | "committed"
  | "rolling-back"
  | "rolled-back"
  | "cancelled"
  | "failed"
  | "disconnected"
  | "closed"

export interface DiffReviewWorkbenchState {
  phase: DiffReviewWorkbenchPhase
  taskId: string
  patchArtifacts: readonly ArtifactContract[]
  selectedArtifact?: ArtifactContract
  manifest?: DiffManifestContract
  fetch?: DiffFetchState
  review?: DiffReviewModelState
  transaction?: PatchTransactionRuntimeState
  preflightReceipts: readonly HashlinePreflightReceipt[]
  search?: DiffSearchResult
  searchBusy: boolean
  error?: DiffReviewWorkbenchFailure
  generation: number
  updatedAt: number
}

export interface DiffReviewWorkbenchFailure {
  code: string
  message: string
  retryable: boolean
  disconnected: boolean
  stage:
    | "catalog"
    | "manifest"
    | "page"
    | "content"
    | "preflight"
    | "review"
    | "apply"
    | "rollback"
    | "search"
  occurredAt: number
}

export interface DiffReviewWorkbenchOptions {
  catalogLimit?: number
  fetch?: DiffFetchRuntimeOptions
  preferences?: DiffReviewPersistence
  maximumPatchArtifacts?: number
}

export type DiffReviewWorkbenchListener = (
  state: DiffReviewWorkbenchState,
) => void

export interface DiffReviewRefreshBridge {
  refreshTask(taskId: string): void | Promise<void>
  refreshArtifacts(taskId: string): void | Promise<void>
  refreshEvents(taskId: string): void | Promise<void>
}

export class DiffReviewWorkbenchError extends Error {
  readonly code: string
  readonly retryable: boolean
  readonly disconnected: boolean
  readonly stage: DiffReviewWorkbenchFailure["stage"]
  readonly details: Readonly<Record<string, unknown>>

  constructor(
    code: string,
    message: string,
    stage: DiffReviewWorkbenchFailure["stage"],
    options: {
      retryable?: boolean
      disconnected?: boolean
      details?: Readonly<Record<string, unknown>>
    } = {},
  ) {
    super(message)
    this.name = "DiffReviewWorkbenchError"
    this.code = code
    this.retryable = options.retryable ?? false
    this.disconnected = options.disconnected ?? false
    this.stage = stage
    this.details = Object.freeze({ ...(options.details ?? {}) })
  }
}

const defaultCatalogLimit = 250
const defaultMaximumPatchArtifacts = 250

function bounded(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
  name: string,
): number {
  if (value === undefined) return fallback
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new DiffReviewWorkbenchError(
      "diff_workbench_option_invalid",
      `${name} must be an integer from ${minimum} through ${maximum}.`,
      "catalog",
    )
  }
  return value
}

function safeIdentity(value: string, name: string): string {
  const selected = String(value || "").trim()
  if (!selected || selected.length > 512 || /[\u0000\r\n]/.test(selected)) {
    throw new DiffReviewWorkbenchError(
      "diff_workbench_identity_invalid",
      `${name} is not a valid identity.`,
      "catalog",
    )
  }
  return selected
}

function isPatchArtifact(artifact: ArtifactContract): boolean {
  if (!artifact.status.exists || !artifact.status.isFile) return false
  if (artifact.status.integrity !== "verified") return false
  if (
    artifact.security.label === "secret"
    || artifact.security.trust === "quarantined"
  ) {
    return false
  }
  const searchable = [
    artifact.kind,
    artifact.title,
    artifact.mediaType,
  ].join(" ").toLowerCase()
  return (
    artifact.contentFamily === "text"
    && (
      searchable.includes("patch")
      || searchable.includes("diff")
      || artifact.mediaType === "text/x-diff"
      || artifact.mediaType === "text/x-patch"
    )
  )
}

function patchArtifacts(
  artifacts: readonly ArtifactContract[],
  maximum: number,
): readonly ArtifactContract[] {
  return Object.freeze(
    artifacts
      .filter(isPatchArtifact)
      .sort((left, right) =>
        Date.parse(right.createdAt) - Date.parse(left.createdAt)
        || left.artifactId.localeCompare(right.artifactId))
      .slice(0, maximum),
  )
}

function frozenState(
  state: DiffReviewWorkbenchState,
): DiffReviewWorkbenchState {
  return Object.freeze({
    ...state,
    patchArtifacts: Object.freeze([...state.patchArtifacts]),
    preflightReceipts: Object.freeze([...state.preflightReceipts]),
  })
}

function stageFailure(
  stage: DiffReviewWorkbenchFailure["stage"],
  error: unknown,
): DiffReviewWorkbenchFailure {
  if (error instanceof DiffReviewWorkbenchError) {
    return Object.freeze({
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      disconnected: error.disconnected,
      stage: error.stage,
      occurredAt: Date.now(),
    })
  }
  const candidate = error as {
    code?: unknown
    message?: unknown
    status?: unknown
    retryable?: unknown
    disconnected?: unknown
  }
  const message = error instanceof Error
    ? error.message
    : String(candidate?.message || error || "Unknown diff workbench failure.")
  const status = typeof candidate?.status === "number" ? candidate.status : 0
  const disconnected =
    candidate?.disconnected === true
    || status === 0
    || status === 503
    || /disabled|unavailable|disconnect|network|fetch/i.test(message)
  return Object.freeze({
    code: String(candidate?.code || (disconnected
      ? "diff_workbench_disconnected"
      : `diff_workbench_${stage}_failed`)),
    message,
    retryable:
      candidate?.retryable === true
      || disconnected
      || status === 408
      || status === 429,
    disconnected,
    stage,
    occurredAt: Date.now(),
  })
}

function bridgeOrNoop(
  bridge: DiffReviewRefreshBridge | undefined,
): DiffReviewRefreshBridge {
  return bridge ?? {
    refreshTask: () => undefined,
    refreshArtifacts: () => undefined,
    refreshEvents: () => undefined,
  }
}

export class DiffReviewWorkbenchRuntime {
  readonly #api: TaskApi
  readonly #taskId: string
  readonly #catalogLimit: number
  readonly #maximumPatchArtifacts: number
  readonly #fetchOptions?: DiffFetchRuntimeOptions
  readonly #preferences?: DiffReviewPersistence
  readonly #refresh: DiffReviewRefreshBridge
  readonly #snapshots = new DiffSnapshotStore()
  readonly #search = new DiffSearchIndex()
  readonly #listeners = new Set<DiffReviewWorkbenchListener>()
  readonly #baseContent = new Map<string, DiffFileContentContract>()
  readonly #currentContent = new Map<string, DiffFileContentContract>()
  #fetch?: DiffFetchRuntime
  #review?: DiffReviewModel
  #transaction?: PatchTransactionRuntime
  #fetchUnsubscribe?: () => void
  #reviewUnsubscribe?: () => void
  #transactionUnsubscribe?: () => void
  #catalogController?: AbortController
  #selectionController?: AbortController
  #preflightController?: AbortController
  #searchController?: AbortController
  #state: DiffReviewWorkbenchState
  #closed = false

  constructor(input: {
    api: TaskApi
    taskId: string
    options?: DiffReviewWorkbenchOptions
    refresh?: DiffReviewRefreshBridge
  }) {
    this.#api = input.api
    this.#taskId = safeIdentity(input.taskId, "taskId")
    this.#catalogLimit = bounded(
      input.options?.catalogLimit,
      defaultCatalogLimit,
      1,
      1_000,
      "catalogLimit",
    )
    this.#maximumPatchArtifacts = bounded(
      input.options?.maximumPatchArtifacts,
      defaultMaximumPatchArtifacts,
      1,
      1_000,
      "maximumPatchArtifacts",
    )
    this.#fetchOptions = input.options?.fetch
    this.#preferences = input.options?.preferences
    this.#refresh = bridgeOrNoop(input.refresh)
    this.#state = frozenState({
      phase: "idle",
      taskId: this.#taskId,
      patchArtifacts: [],
      preflightReceipts: [],
      searchBusy: false,
      generation: 0,
      updatedAt: Date.now(),
    })
  }

  get state(): DiffReviewWorkbenchState {
    return this.#state
  }

  get reviewModel(): DiffReviewModel | undefined {
    return this.#review
  }

  listen(listener: DiffReviewWorkbenchListener): () => void {
    this.#requireOpen()
    this.#listeners.add(listener)
    listener(this.#state)
    return () => this.#listeners.delete(listener)
  }

  async loadCatalog(
    requested?: { artifactId: string; revision?: string },
  ): Promise<readonly ArtifactContract[]> {
    this.#requireOpen()
    this.#catalogController?.abort("Diff artifact catalog superseded.")
    const controller = new AbortController()
    this.#catalogController = controller
    this.#commit({
      phase: "catalog-loading",
      error: undefined,
      generation: this.#state.generation + 1,
    })
    try {
      const raw = await this.#api.artifactCatalog(this.#taskId, {
        limit: this.#catalogLimit,
        contentFamilies: ["text"],
        signal: controller.signal,
      })
      const catalog = parseArtifactCatalogPage(raw)
      if (catalog.taskId !== this.#taskId) {
        throw new DiffReviewWorkbenchError(
          "diff_workbench_catalog_task_mismatch",
          "Artifact catalog belongs to another task.",
          "catalog",
        )
      }
      const artifacts = patchArtifacts(
        catalog.artifacts,
        this.#maximumPatchArtifacts,
      )
      this.#commit({
        phase: "catalog-ready",
        patchArtifacts: artifacts,
        error: undefined,
      })
      const selected = requested
        ? artifacts.find((artifact) =>
          artifact.artifactId === requested.artifactId
          && (!requested.revision || artifact.revision === requested.revision))
        : artifacts[0]
      if (selected) await this.selectArtifact(selected.artifactId, selected.revision)
      return artifacts
    } catch (error) {
      const failure = stageFailure("catalog", error)
      this.#commit({
        phase: failure.disconnected ? "disconnected" : "failed",
        error: failure,
      })
      throw error
    } finally {
      if (this.#catalogController === controller) {
        this.#catalogController = undefined
      }
    }
  }

  async selectArtifact(
    artifactIdValue: string,
    revision?: string,
  ): Promise<DiffManifestContract> {
    this.#requireOpen()
    const artifactId = safeIdentity(artifactIdValue, "artifactId")
    const artifact = this.#state.patchArtifacts.find((candidate) =>
      candidate.artifactId === artifactId
      && (!revision || candidate.revision === revision))
    if (!artifact) {
      throw new DiffReviewWorkbenchError(
        "diff_workbench_artifact_missing",
        `Patch artifact ${artifactId} is not available in the task catalog.`,
        "manifest",
      )
    }
    this.#selectionController?.abort("Diff artifact selection superseded.")
    const controller = new AbortController()
    this.#selectionController = controller
    this.#disposeSelection()
    const fetch = new DiffFetchRuntime({
      api: this.#api,
      taskId: this.#taskId,
      artifactId: artifact.artifactId,
      artifactRevision: artifact.revision,
      options: this.#fetchOptions,
    })
    this.#fetch = fetch
    this.#fetchUnsubscribe = fetch.listen((state) => {
      if (this.#fetch !== fetch) return
      this.#commit({
        fetch: state,
        phase: phaseFromFetch(state, this.#state.phase),
        error: state.error ? failureFromFetch(state.error) : this.#state.error,
      })
    })
    this.#commit({
      phase: "manifest-loading",
      selectedArtifact: artifact,
      manifest: undefined,
      review: undefined,
      transaction: undefined,
      preflightReceipts: [],
      search: undefined,
      error: undefined,
    })
    try {
      const manifest = await fetch.loadManifest(controller.signal)
      this.#review = new DiffReviewModel({
        taskId: this.#taskId,
        diffId: manifest.diffId,
        files: manifest.files,
        persistence: this.#preferences,
      })
      this.#transaction = new PatchTransactionRuntime({
        api: this.#api,
        taskId: this.#taskId,
        runId: manifest.source.runId,
        diffId: manifest.diffId,
      })
      this.#reviewUnsubscribe = this.#review.listen((state) => {
        if (!this.#review || state.diffId !== manifest.diffId) return
        this.#commit({ review: state })
      })
      this.#transactionUnsubscribe = this.#transaction.listen((state) => {
        if (!this.#transaction || state.diffId !== manifest.diffId) return
        this.#commit({
          transaction: state,
          phase: phaseFromTransaction(state, this.#state.phase),
          error: state.error
            ? stageFailure("apply", state.error)
            : this.#state.error,
        })
      })
      this.#baseContent.clear()
      this.#currentContent.clear()
      this.#snapshots.clear()
      this.#search.clear()
      this.#commit({
        phase: "ready",
        manifest,
        fetch: fetch.state,
        review: this.#review.state,
        transaction: this.#transaction.state,
        error: undefined,
      })
      const active = this.#review.state.activeFileId
      if (active) await this.ensureFile(active)
      return manifest
    } catch (error) {
      const failure = stageFailure("manifest", error)
      this.#commit({
        phase: failure.disconnected ? "disconnected" : "failed",
        error: failure,
      })
      throw error
    } finally {
      if (this.#selectionController === controller) {
        this.#selectionController = undefined
      }
    }
  }

  async selectFile(fileId: string): Promise<DiffFileLoadState> {
    const review = this.#requireReview()
    review.selectFile(fileId)
    return this.ensureFile(fileId)
  }

  async ensureFile(
    fileId: string,
    signal?: AbortSignal,
  ): Promise<DiffFileLoadState> {
    const fetch = this.#requireFetch()
    this.#commit({ phase: "loading", error: undefined })
    try {
      const state = await fetch.ensureFile(fileId, {
        throughPage: Math.max(
          0,
          (this.#state.manifest?.files.find((file) => file.fileId === fileId)
            ?.pageCount ?? 1) - 1,
        ),
        priority: 100,
        reason: "active-file",
        signal,
      })
      this.#indexFile(state)
      this.#commit({
        phase: "ready",
        fetch: fetch.state,
        error: undefined,
      })
      return state
    } catch (error) {
      const failure = stageFailure("page", error)
      this.#commit({
        phase: failure.disconnected ? "disconnected" : "failed",
        error: failure,
      })
      throw error
    }
  }

  async loadNextPage(fileId: string): Promise<DiffFileLoadState> {
    const state = await this.#requireFetch().loadNextPage(fileId)
    this.#indexFile(state)
    return state
  }

  async search(options: DiffSearchOptions): Promise<DiffSearchResult> {
    this.#requireOpen()
    this.#searchController?.abort("Diff search superseded.")
    const controller = new AbortController()
    this.#searchController = controller
    const dispose = connectSignal(controller, options.signal)
    this.#commit({ searchBusy: true, error: undefined })
    try {
      const result = await this.#search.search({
        ...options,
        signal: controller.signal,
      })
      this.#commit({
        search: result,
        searchBusy: false,
        error: undefined,
      })
      return result
    } catch (error) {
      const failure = stageFailure("search", error)
      this.#commit({
        searchBusy: false,
        error: failure,
      })
      throw error
    } finally {
      dispose()
      if (this.#searchController === controller) {
        this.#searchController = undefined
      }
    }
  }

  async submitSelection(
    selection: DiffReviewLineSelection,
    actorId: string,
    sealed = false,
  ): Promise<void> {
    const manifest = this.#requireManifest()
    const transaction = this.#requireTransaction()
    try {
      const receipt = await transaction.submitReview({
        manifest,
        action: "select",
        selection,
        expectedRevision: this.#requireReview().state.reviewRevision,
        actorId,
        sealed,
      })
      this.#requireReview().admitReceipt(receipt)
    } catch (error) {
      const failure = stageFailure("review", error)
      this.#commit({ error: failure })
      throw error
    }
  }

  async submitComment(input: {
    body: string
    actorId: string
    commentId?: string
    resolve?: boolean
    sealed?: boolean
  }): Promise<void> {
    const review = this.#requireReview()
    const selection = review.state.selection
    if (!selection) {
      throw new DiffReviewWorkbenchError(
        "diff_workbench_comment_selection_missing",
        "Select reviewed lines before submitting a comment.",
        "review",
      )
    }
    review.setCommentBusy(true)
    try {
      const receipt = await this.#requireTransaction().submitReview({
        manifest: this.#requireManifest(),
        action: input.resolve
          ? "comment_resolve"
          : input.commentId
            ? "comment_update"
            : "comment",
        selection,
        body: input.body,
        commentId: input.commentId,
        expectedRevision: review.state.reviewRevision,
        actorId: input.actorId,
        sealed: Boolean(input.sealed),
      })
      review.admitReceipt(receipt)
    } catch (error) {
      review.setCommentBusy(false)
      const failure = stageFailure("review", error)
      this.#commit({ error: failure })
      throw error
    }
  }

  async preflight(
    selectedFileIds = this.#requireReview().state.selectedFileIds,
    signal?: AbortSignal,
  ): Promise<readonly HashlinePreflightReceipt[]> {
    this.#requireOpen()
    this.#preflightController?.abort("Patch preflight superseded.")
    const controller = new AbortController()
    this.#preflightController = controller
    const dispose = connectSignal(controller, signal)
    this.#commit({
      phase: "preflighting",
      preflightReceipts: [],
      error: undefined,
    })
    const receipts: HashlinePreflightReceipt[] = []
    try {
      for (const fileId of selectedFileIds) {
        if (controller.signal.aborted) {
          throw new DOMException(
            String(controller.signal.reason || "Patch preflight cancelled."),
            "AbortError",
          )
        }
        const fileState = await this.ensureFile(fileId, controller.signal)
        if (!fileState.complete) {
          throw new DiffReviewWorkbenchError(
            "diff_workbench_preflight_partial",
            "Patch preflight requires every hunk page.",
            "preflight",
            { details: { fileId } },
          )
        }
        const content = await this.#loadPreflightContent(
          fileId,
          controller.signal,
        )
        const manifest = this.#requireManifest()
        const file = manifest.files.find((candidate) =>
          candidate.fileId === fileId)
        if (!file) {
          throw new DiffReviewWorkbenchError(
            "diff_workbench_file_missing",
            `Diff file ${fileId} disappeared before preflight.`,
            "preflight",
          )
        }
        const observedLineIds = fileState.hunks.flatMap((hunk) =>
          hunk.lines.map((line) => line.lineId))
        const snapshot = await this.#snapshots.record({
          fileId,
          path: file.path,
          text: content.base.text,
          sha256: content.base.sha256,
          mtimeNs: content.base.mtimeNs,
          mode: content.base.mode,
          encoding: content.base.encoding,
          lineEnding: content.base.lineEnding,
          observedLineIds,
        })
        const receipt = await preflightHashlinePatch({
          file,
          hunks: fileState.hunks,
          baseSnapshot: snapshot,
          current: {
            text: content.current.text,
            sha256: content.current.sha256,
            mtimeNs: content.current.mtimeNs,
            mode: content.current.mode,
          },
          allowThreeWay: true,
        })
        receipts.push(receipt)
      }
      this.#commit({
        phase: "ready",
        preflightReceipts: receipts,
        error: undefined,
      })
      return Object.freeze(receipts)
    } catch (error) {
      const failure = stageFailure("preflight", error)
      this.#commit({
        phase: failure.disconnected ? "disconnected" : "failed",
        preflightReceipts: receipts,
        error: failure,
      })
      throw error
    } finally {
      dispose()
      if (this.#preflightController === controller) {
        this.#preflightController = undefined
      }
    }
  }

  async apply(
    identity: PatchApplyIdentity,
    signal?: AbortSignal,
  ): Promise<PatchTransactionReceipt> {
    const review = this.#requireReview()
    const selected = review.state.selectedFileIds
    if (!selected.length) {
      throw new DiffReviewWorkbenchError(
        "diff_workbench_apply_empty",
        "Select at least one file before applying the patch.",
        "apply",
      )
    }
    let preflight = this.#state.preflightReceipts
    const accepted = new Set(
      preflight
        .filter((receipt) => receipt.accepted)
        .map((receipt) => receipt.fileId),
    )
    if (selected.some((fileId) => !accepted.has(fileId))) {
      preflight = await this.preflight(selected, signal)
    }
    this.#commit({ phase: "applying", error: undefined })
    try {
      const receipt = await this.#requireTransaction().apply({
        manifest: this.#requireManifest(),
        selectedFileIds: selected,
        preflightReceipts: preflight,
        reviewRevision: review.state.reviewRevision,
        identity,
        signal,
      })
      await this.#afterTransaction(receipt)
      return receipt
    } catch (error) {
      const failure = stageFailure("apply", error)
      this.#commit({
        phase: failure.disconnected ? "disconnected" : "failed",
        error: failure,
      })
      throw error
    }
  }

  async retryPermission(
    permitId: string,
    signal?: AbortSignal,
  ): Promise<PatchTransactionReceipt> {
    this.#commit({ phase: "applying", error: undefined })
    try {
      const receipt = await this.#requireTransaction().retryPermission(
        permitId,
        signal,
      )
      await this.#afterTransaction(receipt)
      return receipt
    } catch (error) {
      const failure = stageFailure("apply", error)
      this.#commit({
        phase: failure.disconnected ? "disconnected" : "failed",
        error: failure,
      })
      throw error
    }
  }

  async rollback(
    identity: PatchApplyIdentity,
    signal?: AbortSignal,
  ): Promise<PatchTransactionReceipt> {
    const latest = this.#requireTransaction().state.latestReceipt
    if (!latest) {
      throw new DiffReviewWorkbenchError(
        "diff_workbench_rollback_receipt_missing",
        "No patch transaction is available to roll back.",
        "rollback",
      )
    }
    this.#commit({ phase: "rolling-back", error: undefined })
    try {
      const receipt = await this.#requireTransaction().rollback({
        receipt: latest,
        identity,
        signal,
      })
      await this.#afterTransaction(receipt)
      return receipt
    } catch (error) {
      const failure = stageFailure("rollback", error)
      this.#commit({
        phase: failure.disconnected ? "disconnected" : "failed",
        error: failure,
      })
      throw error
    }
  }

  cancel(reason = "Diff review operation cancelled."): void {
    this.#catalogController?.abort(reason)
    this.#selectionController?.abort(reason)
    this.#preflightController?.abort(reason)
    this.#searchController?.abort(reason)
    const activeFileId = this.#review?.state.activeFileId
    if (activeFileId) this.#fetch?.cancelFile(activeFileId, reason)
    this.#transaction?.cancel(reason)
    this.#commit({
      phase: "cancelled",
      searchBusy: false,
      error: undefined,
    })
  }

  close(reason = "Diff review workbench closed."): void {
    if (this.#closed) return
    this.#closed = true
    this.#catalogController?.abort(reason)
    this.#selectionController?.abort(reason)
    this.#preflightController?.abort(reason)
    this.#searchController?.abort(reason)
    this.#disposeSelection()
    this.#snapshots.close()
    this.#search.close()
    this.#listeners.clear()
    this.#state = frozenState({
      ...this.#state,
      phase: "closed",
      searchBusy: false,
      generation: this.#state.generation + 1,
      updatedAt: Date.now(),
    })
  }

  async #loadPreflightContent(
    fileId: string,
    signal: AbortSignal,
  ): Promise<{
    base: DiffFileContentContract
    current: DiffFileContentContract
  }> {
    const manifest = this.#requireManifest()
    let base = this.#baseContent.get(fileId)
    if (!base) {
      const raw = await this.#api.diffReviewFileContent(
        this.#taskId,
        manifest.source.artifactId,
        fileId,
        {
          revision: manifest.source.artifactRevision,
          version: "base",
          signal,
        },
      )
      base = parseDiffFileContent(raw, manifest, fileId, "base")
      this.#baseContent.set(fileId, base)
    }
    const rawCurrent = await this.#api.diffReviewFileContent(
      this.#taskId,
      manifest.source.artifactId,
      fileId,
      {
        revision: manifest.source.artifactRevision,
        version: "current",
        signal,
      },
    )
    const current = parseDiffFileContent(rawCurrent, manifest, fileId, "current")
    this.#currentContent.set(fileId, current)
    return { base, current }
  }

  #indexFile(fileState: DiffFileLoadState): void {
    const file = this.#state.manifest?.files.find((candidate) =>
      candidate.fileId === fileState.fileId)
    if (!file) return
    this.#search.add({
      file,
      hunks: fileState.hunks,
      complete: fileState.complete,
      loadedBytes: fileState.loadedBytes,
    })
  }

  async #afterTransaction(
    receipt: PatchTransactionReceipt,
  ): Promise<void> {
    if (receipt.phase === "permission_pending") {
      this.#commit({
        phase: "permission-pending",
        transaction: this.#requireTransaction().state,
        error: undefined,
      })
      return
    }
    if (receipt.committed) {
      this.#requireReview().markApplied()
      this.#currentContent.clear()
      this.#commit({
        phase: "committed",
        transaction: this.#requireTransaction().state,
        error: undefined,
      })
      await Promise.all([
        this.#refresh.refreshTask(this.#taskId),
        this.#refresh.refreshArtifacts(this.#taskId),
        this.#refresh.refreshEvents(this.#taskId),
      ])
      return
    }
    if (receipt.rolledBack) {
      this.#currentContent.clear()
      this.#commit({
        phase: "rolled-back",
        transaction: this.#requireTransaction().state,
        error: undefined,
      })
      await Promise.all([
        this.#refresh.refreshTask(this.#taskId),
        this.#refresh.refreshArtifacts(this.#taskId),
        this.#refresh.refreshEvents(this.#taskId),
      ])
      return
    }
    this.#commit({
      phase: "failed",
      transaction: this.#requireTransaction().state,
      error: Object.freeze({
        code: receipt.reasonCode,
        message: receipt.message,
        retryable: false,
        disconnected: false,
        stage: receipt.phase.includes("rollback") ? "rollback" : "apply",
        occurredAt: Date.now(),
      }),
    })
  }

  #disposeSelection(): void {
    this.#fetchUnsubscribe?.()
    this.#reviewUnsubscribe?.()
    this.#transactionUnsubscribe?.()
    this.#fetchUnsubscribe = undefined
    this.#reviewUnsubscribe = undefined
    this.#transactionUnsubscribe = undefined
    this.#fetch?.close()
    this.#review?.close()
    this.#transaction?.close()
    this.#fetch = undefined
    this.#review = undefined
    this.#transaction = undefined
    this.#baseContent.clear()
    this.#currentContent.clear()
    this.#snapshots.clear()
    this.#search.clear()
  }

  #requireManifest(): DiffManifestContract {
    if (!this.#state.manifest) {
      throw new DiffReviewWorkbenchError(
        "diff_workbench_manifest_missing",
        "Select and load a patch artifact first.",
        "manifest",
      )
    }
    return this.#state.manifest
  }

  #requireFetch(): DiffFetchRuntime {
    if (!this.#fetch) {
      throw new DiffReviewWorkbenchError(
        "diff_workbench_fetch_missing",
        "Diff fetch runtime is not connected.",
        "page",
        { disconnected: true },
      )
    }
    return this.#fetch
  }

  #requireReview(): DiffReviewModel {
    if (!this.#review) {
      throw new DiffReviewWorkbenchError(
        "diff_workbench_review_missing",
        "Diff review model is not connected.",
        "review",
        { disconnected: true },
      )
    }
    return this.#review
  }

  #requireTransaction(): PatchTransactionRuntime {
    if (!this.#transaction) {
      throw new DiffReviewWorkbenchError(
        "diff_workbench_transaction_missing",
        "Patch transaction runtime is not connected.",
        "apply",
        { disconnected: true },
      )
    }
    return this.#transaction
  }

  #commit(patch: Partial<DiffReviewWorkbenchState>): void {
    this.#state = frozenState({
      ...this.#state,
      ...patch,
      generation: patch.generation ?? this.#state.generation + 1,
      updatedAt: Date.now(),
    })
    for (const listener of this.#listeners) listener(this.#state)
  }

  #requireOpen(): void {
    if (this.#closed) {
      throw new DiffReviewWorkbenchError(
        "diff_workbench_closed",
        "Diff review workbench is closed.",
        "manifest",
        { disconnected: true },
      )
    }
  }
}

function phaseFromFetch(
  state: DiffFetchState,
  current: DiffReviewWorkbenchPhase,
): DiffReviewWorkbenchPhase {
  if (
    current === "applying"
    || current === "committed"
    || current === "rolling-back"
    || current === "rolled-back"
    || current === "permission-pending"
  ) {
    return current
  }
  if (state.phase === "manifest-loading") return "manifest-loading"
  if (state.phase === "page-loading" || state.phase === "partial") return "loading"
  if (state.phase === "ready" || state.phase === "manifest-ready") return "ready"
  if (state.phase === "cancelled") return "cancelled"
  if (state.phase === "disconnected") return "disconnected"
  if (state.phase === "failed") return "failed"
  return current
}

function phaseFromTransaction(
  state: PatchTransactionRuntimeState,
  current: DiffReviewWorkbenchPhase,
): DiffReviewWorkbenchPhase {
  if (state.phase === "applying" || state.phase === "preparing") return "applying"
  if (state.phase === "permission-pending") return "permission-pending"
  if (state.phase === "committed") return "committed"
  if (state.phase === "rolling-back") return "rolling-back"
  if (state.phase === "rolled-back") return "rolled-back"
  if (state.phase === "disconnected") return "disconnected"
  if (state.phase === "failed" || state.phase === "rejected") return "failed"
  return current
}

function failureFromFetch(error: DiffFetchFailure): DiffReviewWorkbenchFailure {
  return Object.freeze({
    code: error.code,
    message: error.message,
    retryable: error.retryable,
    disconnected: error.disconnected,
    stage: "page",
    occurredAt: error.occurredAt,
  })
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
