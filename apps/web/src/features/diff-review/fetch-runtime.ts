import type { TaskApi } from "../../api/task-api.ts"
import {
  assertManifestBinding,
  assertPageContinuation,
  DiffContractError,
  parseDiffManifest,
  parseDiffPage,
  type DiffFileContract,
  type DiffHunkContract,
  type DiffManifestContract,
  type DiffPageContract,
} from "./contracts.ts"
import {
  DiffBudgetError,
  DiffBudgetLedger,
  DiffRequestQueue,
  type DiffBudgetLimits,
  type DiffBudgetUsage,
  type DiffRequestLease,
} from "./budget.ts"

export type DiffFetchPhase =
  | "idle"
  | "manifest-loading"
  | "manifest-ready"
  | "page-loading"
  | "partial"
  | "ready"
  | "cancelled"
  | "failed"
  | "disconnected"
  | "closed"

export interface DiffFileLoadState {
  fileId: string
  phase: "idle" | "loading" | "partial" | "ready" | "failed" | "cancelled"
  pages: readonly DiffPageContract[]
  hunks: readonly DiffHunkContract[]
  loadedPageIndexes: readonly number[]
  loadedHunkCount: number
  loadedLineCount: number
  loadedBytes: number
  nextPageIndex: number
  complete: boolean
  error?: DiffFetchFailure
  updatedAt: number
}

export interface DiffFetchFailure {
  code: string
  message: string
  retryable: boolean
  disconnected: boolean
  fileId?: string
  pageIndex?: number
  occurredAt: number
}

export interface DiffFetchState {
  phase: DiffFetchPhase
  taskId: string
  artifactId: string
  artifactRevision?: string
  manifest?: DiffManifestContract
  files: Readonly<Record<string, DiffFileLoadState>>
  activeFileId?: string
  usage: DiffBudgetUsage
  error?: DiffFetchFailure
  generation: number
  updatedAt: number
}

export interface DiffFetchRuntimeOptions {
  budget?: Partial<DiffBudgetLimits>
  requestedPageBytes?: number
  requestedPageLines?: number
  prefetchPages?: number
  maximumCachedFiles?: number
}

export type DiffFetchListener = (state: DiffFetchState) => void

interface FileAssembly {
  file: DiffFileContract
  pages: Map<number, DiffPageContract>
  phase: DiffFileLoadState["phase"]
  error?: DiffFetchFailure
  generation: number
  lastAccessedAt: number
}

// The server page identity is hunk-atomic and admits up to these hard bounds.
// Requesting a smaller client default can strand a valid 20k-100k-line hunk:
// it cannot be split without changing immutable line/hunk receipt identity.
const defaultRequestedPageBytes = 8 * 1024 * 1024
const defaultRequestedPageLines = 100_000
const defaultPrefetchPages = 1
const defaultMaximumCachedFiles = 32

function boundedInteger(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
  name: string,
): number {
  if (value === undefined) return fallback
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new DiffBudgetError(
      "diff_fetch_option_invalid",
      `${name} must be an integer from ${minimum} through ${maximum}.`,
    )
  }
  return value
}

function cleanIdentity(value: string, name: string): string {
  const selected = String(value || "").trim()
  if (!selected || selected.length > 512 || /[\u0000\r\n]/.test(selected)) {
    throw new DiffContractError(
      "diff_fetch_identity_invalid",
      `${name} is not a valid identity.`,
    )
  }
  return selected
}

function pageKey(fileId: string, pageIndex: number): string {
  return `${fileId}:${pageIndex}`
}

function lineCount(page: DiffPageContract): number {
  return page.hunks.reduce((total, hunk) => total + hunk.lines.length, 0)
}

function canonicalPagePayload(page: DiffPageContract): string {
  return JSON.stringify({
    diff_id: page.diffId,
    file_id: page.fileId,
    artifact_revision: page.artifactRevision,
    page_index: page.pageIndex,
    page_count: page.pageCount,
    hunk_start: page.hunkStart,
    hunk_end: page.hunkEnd,
    hunks: page.hunks.map((hunk) => ({
      hunk_id: hunk.hunkId,
      index: hunk.index,
      header: hunk.header,
      old_start: hunk.oldStart,
      old_count: hunk.oldCount,
      new_start: hunk.newStart,
      new_count: hunk.newCount,
      lines: hunk.lines.map((line) => ({
        line_id: line.lineId,
        kind: line.kind,
        text: line.text,
        old_line: line.oldLine ?? null,
        new_line: line.newLine ?? null,
        patch_line: line.patchLine,
        no_newline: line.noNewline,
      })),
    })),
  })
}

function hex(bytes: Uint8Array): string {
  let value = ""
  for (const byte of bytes) value += byte.toString(16).padStart(2, "0")
  return value
}

async function sha256(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value)
  const digest = await crypto.subtle.digest("SHA-256", bytes)
  return `sha256:${hex(new Uint8Array(digest))}`
}

async function verifyPageDigest(page: DiffPageContract): Promise<void> {
  const actual = await sha256(canonicalPagePayload(page))
  if (actual !== page.contentDigest) {
    throw new DiffContractError(
      "diff_fetch_page_digest_mismatch",
      "Diff page content digest does not match its verified payload.",
      "$.content_digest",
      { expected: page.contentDigest, actual },
    )
  }
}

function frozenFileState(assembly: FileAssembly): DiffFileLoadState {
  const pages = [...assembly.pages.values()]
    .sort((left, right) => left.pageIndex - right.pageIndex)
  const hunks = pages.flatMap((page) => page.hunks)
  const loadedPageIndexes = pages.map((page) => page.pageIndex)
  const loadedBytes = pages.reduce((total, page) => total + page.utf8Bytes, 0)
  const loadedLineCount = pages.reduce(
    (total, page) => total + lineCount(page),
    0,
  )
  let nextPageIndex = 0
  while (assembly.pages.has(nextPageIndex)) nextPageIndex += 1
  const complete =
    assembly.file.pageCount === 0
    || (
      nextPageIndex === assembly.file.pageCount
      && pages.every((page, index) => page.pageIndex === index)
      && pages.at(-1)?.complete === true
    )
  return Object.freeze({
    fileId: assembly.file.fileId,
    phase: complete ? "ready" : assembly.phase,
    pages: Object.freeze(pages),
    hunks: Object.freeze(hunks),
    loadedPageIndexes: Object.freeze(loadedPageIndexes),
    loadedHunkCount: hunks.length,
    loadedLineCount,
    loadedBytes,
    nextPageIndex,
    complete,
    error: assembly.error,
    updatedAt: assembly.lastAccessedAt,
  })
}

function failureFrom(
  error: unknown,
  fileId?: string,
  pageIndex?: number,
): DiffFetchFailure {
  if (error instanceof DiffContractError) {
    return Object.freeze({
      code: error.code,
      message: error.message,
      retryable: false,
      disconnected: false,
      fileId,
      pageIndex,
      occurredAt: Date.now(),
    })
  }
  if (error instanceof DiffBudgetError) {
    return Object.freeze({
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      disconnected: error.code.includes("closed"),
      fileId,
      pageIndex,
      occurredAt: Date.now(),
    })
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return Object.freeze({
      code: "diff_fetch_cancelled",
      message: error.message || "Diff request was cancelled.",
      retryable: true,
      disconnected: false,
      fileId,
      pageIndex,
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
    : String(candidate?.message || error || "Unknown diff fetch failure.")
  const status = typeof candidate?.status === "number" ? candidate.status : 0
  const disconnected =
    status === 0
    || status === 503
    || /disabled|unavailable|disconnect|network|fetch/i.test(message)
  return Object.freeze({
    code: String(candidate?.code || (disconnected
      ? "diff_fetch_disconnected"
      : "diff_fetch_failed")),
    message,
    retryable: disconnected || status === 408 || status === 429,
    disconnected,
    fileId,
    pageIndex,
    occurredAt: Date.now(),
  })
}

export class DiffPageStore {
  readonly #assemblies = new Map<string, FileAssembly>()
  readonly #receipts = new Map<string, string>()
  readonly #pageDigests = new Map<string, string>()
  readonly #maximumCachedFiles: number

  constructor(
    manifest: DiffManifestContract,
    maximumCachedFiles = defaultMaximumCachedFiles,
  ) {
    this.#maximumCachedFiles = boundedInteger(
      maximumCachedFiles,
      defaultMaximumCachedFiles,
      1,
      1_000,
      "maximumCachedFiles",
    )
    const now = Date.now()
    for (const file of manifest.files) {
      this.#assemblies.set(file.fileId, {
        file,
        pages: new Map(),
        phase: file.pageCount === 0 ? "ready" : "idle",
        generation: 0,
        lastAccessedAt: now,
      })
    }
  }

  require(fileId: string): FileAssembly {
    const assembly = this.#assemblies.get(fileId)
    if (!assembly) {
      throw new DiffContractError(
        "diff_fetch_unknown_file",
        `Diff file ${fileId} does not exist.`,
      )
    }
    assembly.lastAccessedAt = Date.now()
    return assembly
  }

  state(fileId: string): DiffFileLoadState {
    return frozenFileState(this.require(fileId))
  }

  states(): Readonly<Record<string, DiffFileLoadState>> {
    return Object.freeze(
      Object.fromEntries(
        [...this.#assemblies.entries()].map(([fileId, assembly]) => [
          fileId,
          frozenFileState(assembly),
        ]),
      ),
    )
  }

  begin(fileId: string): number {
    const assembly = this.require(fileId)
    assembly.generation += 1
    assembly.phase = assembly.pages.size ? "partial" : "loading"
    assembly.error = undefined
    assembly.lastAccessedAt = Date.now()
    return assembly.generation
  }

  async admit(
    page: DiffPageContract,
    generation: number,
  ): Promise<{ cacheHit: boolean; state: DiffFileLoadState }> {
    const assembly = this.require(page.fileId)
    if (generation !== assembly.generation) {
      throw new DiffContractError(
        "diff_fetch_stale_generation",
        "Diff page returned for a stale file generation.",
        "$.page_index",
        { expected: assembly.generation, actual: generation },
      )
    }
    const key = pageKey(page.fileId, page.pageIndex)
    const existing = assembly.pages.get(page.pageIndex)
    if (existing) {
      if (
        existing.contentDigest !== page.contentDigest
        || existing.receiptId !== page.receiptId
      ) {
        throw new DiffContractError(
          "diff_fetch_immutable_page_conflict",
          "Previously admitted diff page changed under the same identity.",
          "$.content_digest",
        )
      }
      assembly.lastAccessedAt = Date.now()
      return { cacheHit: true, state: frozenFileState(assembly) }
    }
    const receiptDigest = this.#receipts.get(page.receiptId)
    if (receiptDigest && receiptDigest !== page.contentDigest) {
      throw new DiffContractError(
        "diff_fetch_receipt_reuse",
        "Diff receipt identity was reused for different content.",
        "$.receipt_id",
      )
    }
    const priorDigest = this.#pageDigests.get(key)
    if (priorDigest && priorDigest !== page.contentDigest) {
      throw new DiffContractError(
        "diff_fetch_page_digest_conflict",
        "Diff page identity was reused for different content.",
        "$.content_digest",
      )
    }
    const previous = page.pageIndex > 0
      ? assembly.pages.get(page.pageIndex - 1)
      : undefined
    assertPageContinuation(previous, page)
    if (page.pageIndex > 0 && !previous) {
      throw new DiffContractError(
        "diff_fetch_page_gap",
        "Diff page cannot be admitted before its predecessor.",
        "$.page_index",
      )
    }
    const next = assembly.pages.get(page.pageIndex + 1)
    if (next) assertPageContinuation(page, next)
    await verifyPageDigest(page)
    assembly.pages.set(page.pageIndex, page)
    this.#receipts.set(page.receiptId, page.contentDigest)
    this.#pageDigests.set(key, page.contentDigest)
    assembly.phase =
      assembly.pages.size === assembly.file.pageCount
        ? "ready"
        : "partial"
    assembly.error = undefined
    assembly.lastAccessedAt = Date.now()
    this.#evictColdFiles(page.fileId)
    return { cacheHit: false, state: frozenFileState(assembly) }
  }

  fail(
    fileId: string,
    generation: number,
    failure: DiffFetchFailure,
  ): DiffFileLoadState {
    const assembly = this.require(fileId)
    if (generation !== assembly.generation) return frozenFileState(assembly)
    assembly.error = failure
    assembly.phase =
      failure.code === "diff_fetch_cancelled"
        ? "cancelled"
        : "failed"
    assembly.lastAccessedAt = Date.now()
    return frozenFileState(assembly)
  }

  cancel(fileId: string): DiffFileLoadState {
    const assembly = this.require(fileId)
    assembly.generation += 1
    assembly.phase = "cancelled"
    assembly.error = undefined
    assembly.lastAccessedAt = Date.now()
    return frozenFileState(assembly)
  }

  clear(fileId: string): DiffFileLoadState {
    const assembly = this.require(fileId)
    assembly.generation += 1
    for (const page of assembly.pages.values()) {
      this.#pageDigests.delete(pageKey(fileId, page.pageIndex))
      this.#receipts.delete(page.receiptId)
    }
    assembly.pages.clear()
    assembly.phase = assembly.file.pageCount === 0 ? "ready" : "idle"
    assembly.error = undefined
    assembly.lastAccessedAt = Date.now()
    return frozenFileState(assembly)
  }

  #evictColdFiles(protectedFileId: string): void {
    const loaded = [...this.#assemblies.values()]
      .filter((assembly) => assembly.pages.size > 0)
    if (loaded.length <= this.#maximumCachedFiles) return
    const candidates = loaded
      .filter((assembly) => assembly.file.fileId !== protectedFileId)
      .sort((left, right) => left.lastAccessedAt - right.lastAccessedAt)
    while (
      [...this.#assemblies.values()].filter((assembly) => assembly.pages.size > 0)
        .length > this.#maximumCachedFiles
      && candidates.length
    ) {
      const victim = candidates.shift()!
      this.clear(victim.file.fileId)
    }
  }
}

export class DiffFetchRuntime {
  readonly #api: TaskApi
  readonly #taskId: string
  readonly #artifactId: string
  readonly #artifactRevision?: string
  readonly #budget: DiffBudgetLedger
  readonly #queue: DiffRequestQueue
  readonly #requestedPageBytes: number
  readonly #requestedPageLines: number
  readonly #prefetchPages: number
  readonly #maximumCachedFiles: number
  readonly #listeners = new Set<DiffFetchListener>()
  readonly #inFlight = new Map<string, Promise<void>>()
  #store?: DiffPageStore
  #manifestController?: AbortController
  #state: DiffFetchState
  #generation = 0
  #closed = false

  constructor(input: {
    api: TaskApi
    taskId: string
    artifactId: string
    artifactRevision?: string
    options?: DiffFetchRuntimeOptions
  }) {
    this.#api = input.api
    this.#taskId = cleanIdentity(input.taskId, "taskId")
    this.#artifactId = cleanIdentity(input.artifactId, "artifactId")
    this.#artifactRevision = input.artifactRevision
      ? cleanIdentity(input.artifactRevision, "artifactRevision")
      : undefined
    this.#budget = new DiffBudgetLedger(input.options?.budget)
    this.#queue = new DiffRequestQueue(this.#budget)
    this.#requestedPageBytes = boundedInteger(
      input.options?.requestedPageBytes,
      defaultRequestedPageBytes,
      1_024,
      this.#budget.limits.maximumBytes,
      "requestedPageBytes",
    )
    this.#requestedPageLines = boundedInteger(
      input.options?.requestedPageLines,
      defaultRequestedPageLines,
      1,
      this.#budget.limits.maximumLines,
      "requestedPageLines",
    )
    this.#prefetchPages = boundedInteger(
      input.options?.prefetchPages,
      defaultPrefetchPages,
      0,
      16,
      "prefetchPages",
    )
    this.#maximumCachedFiles = boundedInteger(
      input.options?.maximumCachedFiles,
      defaultMaximumCachedFiles,
      1,
      1_000,
      "maximumCachedFiles",
    )
    this.#state = Object.freeze({
      phase: "idle",
      taskId: this.#taskId,
      artifactId: this.#artifactId,
      artifactRevision: this.#artifactRevision,
      files: Object.freeze({}),
      usage: this.#budget.usage,
      generation: 0,
      updatedAt: Date.now(),
    })
  }

  get state(): DiffFetchState {
    return this.#state
  }

  listen(listener: DiffFetchListener): () => void {
    this.#listeners.add(listener)
    listener(this.#state)
    return () => this.#listeners.delete(listener)
  }

  async loadManifest(signal?: AbortSignal): Promise<DiffManifestContract> {
    this.#requireOpen()
    this.#manifestController?.abort("Diff manifest superseded.")
    const controller = new AbortController()
    this.#manifestController = controller
    const dispose = connectSignal(controller, signal)
    const generation = ++this.#generation
    this.#commit({
      phase: "manifest-loading",
      error: undefined,
      generation,
    })
    try {
      const raw = await this.#api.diffReviewManifest(
        this.#taskId,
        this.#artifactId,
        {
          revision: this.#artifactRevision,
          signal: controller.signal,
        },
      )
      if (generation !== this.#generation) {
        throw new DOMException("Diff manifest request superseded.", "AbortError")
      }
      const manifest = parseDiffManifest(raw)
      assertManifestBinding(manifest, {
        taskId: this.#taskId,
        artifactId: this.#artifactId,
        artifactRevision: this.#artifactRevision,
      })
      this.#store = new DiffPageStore(manifest, this.#maximumCachedFiles)
      this.#commit({
        phase: "manifest-ready",
        artifactRevision: manifest.source.artifactRevision,
        manifest,
        files: this.#store.states(),
        activeFileId: manifest.files[0]?.fileId,
        error: undefined,
        generation,
      })
      return manifest
    } catch (error) {
      const failure = failureFrom(error)
      this.#commit({
        phase:
          failure.code === "diff_fetch_cancelled"
            ? "cancelled"
            : failure.disconnected
              ? "disconnected"
              : "failed",
        error: failure,
        generation,
      })
      throw error
    } finally {
      dispose()
      if (this.#manifestController === controller) {
        this.#manifestController = undefined
      }
    }
  }

  selectFile(fileId: string): DiffFileLoadState {
    const store = this.#requireStore()
    const state = store.state(fileId)
    this.#commit({ activeFileId: fileId })
    return state
  }

  async ensureFile(
    fileId: string,
    input: {
      throughPage?: number
      priority?: number
      reason?: string
      signal?: AbortSignal
    } = {},
  ): Promise<DiffFileLoadState> {
    this.#requireOpen()
    const store = this.#requireStore()
    const assembly = store.require(fileId)
    const throughPage = boundedInteger(
      input.throughPage,
      Math.max(0, Math.min(assembly.file.pageCount - 1, this.#prefetchPages)),
      0,
      Math.max(0, assembly.file.pageCount - 1),
      "throughPage",
    )
    if (assembly.file.pageCount === 0) return store.state(fileId)
    const generation = store.begin(fileId)
    this.#commit({
      phase: "page-loading",
      activeFileId: fileId,
      files: store.states(),
      error: undefined,
    })
    const required: number[] = []
    for (let pageIndex = 0; pageIndex <= throughPage; pageIndex += 1) {
      if (!assembly.pages.has(pageIndex)) required.push(pageIndex)
    }
    try {
      for (const pageIndex of required) {
        if (input.signal?.aborted) {
          throw new DOMException(
            String(input.signal.reason || "Diff load cancelled."),
            "AbortError",
          )
        }
        await this.#loadPage({
          fileId,
          pageIndex,
          generation,
          priority: input.priority ?? 0,
          reason: input.reason ?? "ensure-file",
          signal: input.signal,
        })
      }
      const state = store.state(fileId)
      this.#commit({
        phase: state.complete ? "ready" : "partial",
        files: store.states(),
        error: undefined,
      })
      return state
    } catch (error) {
      const failure = failureFrom(error, fileId)
      store.fail(fileId, generation, failure)
      this.#commit({
        phase:
          failure.code === "diff_fetch_cancelled"
            ? "cancelled"
            : failure.disconnected
              ? "disconnected"
              : "failed",
        files: store.states(),
        error: failure,
      })
      throw error
    }
  }

  async loadNextPage(
    fileId: string,
    signal?: AbortSignal,
  ): Promise<DiffFileLoadState> {
    const store = this.#requireStore()
    const current = store.state(fileId)
    if (current.complete) return current
    return this.ensureFile(fileId, {
      throughPage: current.nextPageIndex,
      priority: 100,
      reason: "next-page",
      signal,
    })
  }

  async prefetchViewport(
    fileId: string,
    visibleHunkEnd: number,
    signal?: AbortSignal,
  ): Promise<DiffFileLoadState> {
    const store = this.#requireStore()
    const assembly = store.require(fileId)
    if (!assembly.file.pageCount) return store.state(fileId)
    const loaded = store.state(fileId)
    if (loaded.complete || visibleHunkEnd < loaded.loadedHunkCount - 2) {
      return loaded
    }
    const target = Math.min(
      assembly.file.pageCount - 1,
      loaded.nextPageIndex + this.#prefetchPages,
    )
    return this.ensureFile(fileId, {
      throughPage: target,
      priority: 25,
      reason: "viewport-prefetch",
      signal,
    })
  }

  cancelFile(fileId: string, reason = "Diff file load cancelled."): void {
    this.#queue.cancelFile(fileId, reason)
    this.#store?.cancel(fileId)
    this.#commit({
      phase: "cancelled",
      files: this.#store?.states() ?? this.#state.files,
      error: Object.freeze({
        code: "diff_fetch_cancelled",
        message: reason,
        retryable: true,
        disconnected: false,
        fileId,
        occurredAt: Date.now(),
      }),
    })
  }

  evictFile(fileId: string): DiffFileLoadState {
    const state = this.#requireStore().clear(fileId)
    this.#commit({ files: this.#requireStore().states() })
    return state
  }

  close(reason = "Diff fetch runtime closed."): void {
    if (this.#closed) return
    this.#closed = true
    this.#generation += 1
    this.#manifestController?.abort(reason)
    this.#manifestController = undefined
    this.#queue.close(reason)
    this.#budget.close()
    this.#listeners.clear()
    this.#state = Object.freeze({
      ...this.#state,
      phase: "closed",
      usage: this.#budget.usage,
      generation: this.#generation,
      updatedAt: Date.now(),
    })
  }

  async #loadPage(input: {
    fileId: string
    pageIndex: number
    generation: number
    priority: number
    reason: string
    signal?: AbortSignal
  }): Promise<void> {
    const key = pageKey(input.fileId, input.pageIndex)
    const existing = this.#inFlight.get(key)
    if (existing) return existing
    const operation = this.#performPageLoad(input)
    this.#inFlight.set(key, operation)
    try {
      await operation
    } finally {
      if (this.#inFlight.get(key) === operation) this.#inFlight.delete(key)
    }
  }

  async #performPageLoad(input: {
    fileId: string
    pageIndex: number
    generation: number
    priority: number
    reason: string
    signal?: AbortSignal
  }): Promise<void> {
    this.#queue.enqueue({
      fileId: input.fileId,
      pageIndex: input.pageIndex,
      priority: input.priority,
      reason: input.reason,
      signal: input.signal,
    })
    const lease = await this.#waitForLease(input.fileId, input.pageIndex)
    try {
      const manifest = this.#state.manifest
      if (!manifest) {
        throw new DiffContractError(
          "diff_fetch_manifest_missing",
          "Cannot fetch a diff page before its manifest.",
        )
      }
      const raw = await this.#api.diffReviewPage(
        this.#taskId,
        this.#artifactId,
        input.fileId,
        {
          revision: manifest.source.artifactRevision,
          page: input.pageIndex,
          maximumBytes: this.#requestedPageBytes,
          maximumLines: this.#requestedPageLines,
          signal: lease.controller.signal,
        },
      )
      const page = parseDiffPage(raw, manifest)
      if (page.pageIndex !== input.pageIndex || page.fileId !== input.fileId) {
        throw new DiffContractError(
          "diff_fetch_page_identity_mismatch",
          "Diff page response does not match its request.",
        )
      }
      const admitted = await this.#requireStore().admit(page, input.generation)
      this.#queue.complete(lease.ticket.ticketId, {
        admittedBytes: page.utf8Bytes,
        admittedLines: lineCount(page),
        admittedHunks: page.hunks.length,
        cacheHit: admitted.cacheHit,
      })
      this.#commit({
        phase: admitted.state.complete ? "ready" : "partial",
        files: this.#requireStore().states(),
        usage: this.#budget.usage,
        error: undefined,
      })
    } catch (error) {
      this.#queue.fail(
        lease.ticket.ticketId,
        lease.controller.signal.aborted ? "cancelled" : "failed",
      )
      this.#commit({ usage: this.#budget.usage })
      throw error
    }
  }

  async #waitForLease(
    fileId: string,
    pageIndex: number,
  ): Promise<DiffRequestLease> {
    const deadline = Date.now() + this.#budget.limits.requestTimeoutMs
    while (Date.now() < deadline) {
      const lease = this.#queue.next({
        requestedBytes: this.#requestedPageBytes,
        requestedLines: this.#requestedPageLines,
      })
      if (lease) {
        if (
          lease.ticket.fileId === fileId
          && lease.ticket.pageIndex === pageIndex
        ) {
          return lease
        }
        this.#queue.fail(lease.ticket.ticketId, "cancelled")
        continue
      }
      await new Promise<void>((resolve) => setTimeout(resolve, 0))
    }
    throw new DiffBudgetError(
      "diff_fetch_queue_timeout",
      "Diff request could not acquire a budget lease before its deadline.",
      true,
      { fileId, pageIndex },
    )
  }

  #requireStore(): DiffPageStore {
    if (!this.#store) {
      throw new DiffContractError(
        "diff_fetch_manifest_missing",
        "Diff manifest has not been loaded.",
      )
    }
    return this.#store
  }

  #commit(patch: Partial<DiffFetchState>): void {
    this.#state = Object.freeze({
      ...this.#state,
      ...patch,
      usage: patch.usage ?? this.#budget.usage,
      updatedAt: Date.now(),
    })
    for (const listener of this.#listeners) listener(this.#state)
  }

  #requireOpen(): void {
    if (this.#closed) {
      throw new DiffBudgetError(
        "diff_fetch_closed",
        "Diff fetch runtime is closed.",
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
