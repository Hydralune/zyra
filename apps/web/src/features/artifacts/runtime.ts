import type { TaskApi } from "../../api/task-api.ts"
import {
  ArtifactBookmarkStore,
  ArtifactContentCache,
  ArtifactViewPreferenceStore,
  browserArtifactPreferenceStorage,
  type ArtifactBookmark,
  type ArtifactCacheSnapshot,
  type ArtifactViewPreference,
} from "./cache.ts"
import {
  ArtifactCatalogModel,
  ArtifactCatalogNavigation,
  catalogServerFilters,
  type ArtifactCatalogSelection,
  type ArtifactCatalogState,
  type ArtifactCatalogViewQuery,
} from "./catalog.ts"
import {
  ArtifactContentError,
  ArtifactContentSession,
  ArtifactRangeScheduler,
  type ArtifactContentState,
  type ArtifactSearchOptions,
  type ArtifactSearchResult,
  type ArtifactTextLineIndex,
} from "./content.ts"
import {
  artifactRevisionKey,
  parseArtifactCatalogPage,
  parseArtifactReadResponse,
  type ArtifactContract,
} from "./contracts.ts"
import {
  ArtifactReceiptAdmission,
  downloadDecision,
  type ArtifactAdmission,
} from "./security.ts"
import {
  ArtifactViewerRegistry,
  buildBinaryViewerModel,
  buildJsonViewerModel,
  buildMarkdownViewerModel,
  buildMetadataViewerModel,
  buildTextViewerModel,
  chooseArtifactViewer,
  type ArtifactBinaryViewerModel,
  type ArtifactJsonViewerModel,
  type ArtifactMarkdownViewerModel,
  type ArtifactMetadataViewerModel,
  type ArtifactTextViewerModel,
  type ArtifactViewerDecision,
  type ArtifactMediaViewerModel,
} from "./viewers.ts"
import { ArtifactMediaResourceRegistry } from "./media.ts"
import {
  ArtifactOperationLedger,
  type ArtifactAuditSnapshot,
} from "./audit.ts"

export type ArtifactWorkbenchPhase =
  | "idle"
  | "catalog-loading"
  | "catalog-ready"
  | "artifact-loading"
  | "artifact-ready"
  | "partial"
  | "cancelled"
  | "failed"
  | "disconnected"
  | "closed"

export type ArtifactRenderableModel =
  | ArtifactTextViewerModel
  | ArtifactMarkdownViewerModel
  | ArtifactJsonViewerModel
  | ArtifactBinaryViewerModel
  | ArtifactMediaViewerModel
  | ArtifactMetadataViewerModel

export interface ArtifactWorkbenchState {
  taskId: string
  phase: ArtifactWorkbenchPhase
  catalog: ArtifactCatalogState
  selected?: ArtifactContract
  viewer?: ArtifactViewerDecision
  model?: ArtifactRenderableModel
  content?: ArtifactContentState
  search?: ArtifactSearchResult
  bookmarks: readonly ArtifactBookmark[]
  admission?: ArtifactAdmission
  error?: ArtifactWorkbenchFailure
  generation: number
  updatedAt: number
}

export interface ArtifactWorkbenchFailure {
  code: string
  message: string
  retryable: boolean
  disconnected: boolean
  occurredAt: number
}

export interface ArtifactWorkbenchOptions {
  catalogLimit?: number
  initialRangeBytes?: number
  maximumPreviewBytes?: number
  searchPrefetchBytes?: number
}

export type ArtifactWorkbenchListener = (state: ArtifactWorkbenchState) => void

const DEFAULT_CATALOG_LIMIT = 100
const DEFAULT_INITIAL_RANGE = 256 * 1024
const DEFAULT_MAXIMUM_PREVIEW = 8 * 1024 * 1024
const DEFAULT_SEARCH_PREFETCH = 2 * 1024 * 1024

export class ArtifactWorkbenchRuntime {
  readonly #api: TaskApi
  readonly #taskId: string
  readonly #catalog: ArtifactCatalogModel
  readonly #navigation = new ArtifactCatalogNavigation()
  readonly #cache: ArtifactContentCache
  readonly #admission = new ArtifactReceiptAdmission()
  readonly #scheduler: ArtifactRangeScheduler
  readonly #viewers = new ArtifactViewerRegistry()
  readonly #media = new ArtifactMediaResourceRegistry()
  readonly #preferences: ArtifactViewPreferenceStore
  readonly #bookmarks: ArtifactBookmarkStore
  readonly #audit: ArtifactOperationLedger
  readonly #listeners = new Set<ArtifactWorkbenchListener>()
  readonly #catalogLimit: number
  readonly #initialRangeBytes: number
  readonly #maximumPreviewBytes: number
  readonly #searchPrefetchBytes: number
  #contentSession?: ArtifactContentSession
  #contentUnsubscribe?: () => void
  #catalogUnsubscribe?: () => void
  #navigationUnsubscribe?: () => void
  #disposeBrowserNavigation?: () => void
  #loadController?: AbortController
  #selectionController?: AbortController
  #searchController?: AbortController
  #state: ArtifactWorkbenchState
  #generation = 0
  #closed = false

  constructor(input: {
    api: TaskApi
    taskId: string
    cache?: ArtifactContentCache
    preferences?: ArtifactViewPreferenceStore
    bookmarks?: ArtifactBookmarkStore
    options?: ArtifactWorkbenchOptions
  }) {
    this.#api = input.api
    this.#taskId = safeIdentity(input.taskId, "task")
    this.#audit = new ArtifactOperationLedger(this.#taskId)
    this.#catalog = new ArtifactCatalogModel(this.#taskId)
    this.#cache = input.cache ?? new ArtifactContentCache()
    this.#preferences =
      input.preferences
      ?? new ArtifactViewPreferenceStore(browserArtifactPreferenceStorage())
    this.#bookmarks =
      input.bookmarks
      ?? new ArtifactBookmarkStore(browserArtifactPreferenceStorage())
    this.#catalogLimit = boundedInteger(
      input.options?.catalogLimit,
      DEFAULT_CATALOG_LIMIT,
      1,
      500,
    )
    this.#initialRangeBytes = boundedInteger(
      input.options?.initialRangeBytes,
      DEFAULT_INITIAL_RANGE,
      1024,
      1024 * 1024,
    )
    this.#maximumPreviewBytes = boundedInteger(
      input.options?.maximumPreviewBytes,
      DEFAULT_MAXIMUM_PREVIEW,
      this.#initialRangeBytes,
      64 * 1024 * 1024,
    )
    this.#searchPrefetchBytes = boundedInteger(
      input.options?.searchPrefetchBytes,
      DEFAULT_SEARCH_PREFETCH,
      this.#initialRangeBytes,
      this.#maximumPreviewBytes,
    )
    this.#scheduler = new ArtifactRangeScheduler({
      api: this.#api,
      taskId: this.#taskId,
      cache: this.#cache,
      admission: this.#admission,
      options: { chunkBytes: this.#initialRangeBytes },
    })
    this.#state = freezeWorkbenchState({
      taskId: this.#taskId,
      phase: "idle",
      catalog: this.#catalog.state,
      bookmarks: this.#bookmarks.list(this.#taskId),
      generation: 0,
      updatedAt: Date.now(),
    })
    this.#catalogUnsubscribe = this.#catalog.listen((catalog) => {
      this.#commit({
        catalog,
        phase:
          catalog.phase === "loading"
            ? "catalog-loading"
            : catalog.phase === "failed"
              ? "failed"
              : catalog.phase === "disconnected"
                ? "disconnected"
                : this.#state.selected
                  ? this.#state.phase
                  : "catalog-ready",
      })
    })
    this.#navigationUnsubscribe = this.#navigation.listen((selection) => {
      void this.select(selection)
    })
    if (typeof window !== "undefined") {
      this.#disposeBrowserNavigation = this.#navigation.installBrowserBridge(window)
    }
  }

  get state(): ArtifactWorkbenchState {
    return this.#state
  }

  get navigation(): ArtifactCatalogNavigation {
    return this.#navigation
  }

  listen(listener: ArtifactWorkbenchListener): () => void {
    this.#assertOpen()
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  async loadCatalog(options: { reset?: boolean } = {}): Promise<void> {
    this.#assertOpen()
    this.#loadController?.abort("Superseded artifact catalog load.")
    const controller = new AbortController()
    this.#loadController = controller
    const generation = ++this.#generation
    this.#catalog.loading({ reset: options.reset ?? true })
    this.#audit.catalogRequest({
      reset: options.reset ?? true,
      limit: this.#catalogLimit,
      cursor: null,
    })
    try {
      const raw = await this.#api.artifactCatalog(this.#taskId, {
        limit: this.#catalogLimit,
        ...catalogServerFilters(this.#catalog.state.query),
        signal: controller.signal,
      })
      if (controller.signal.aborted || generation !== this.#generation) return
      const page = parseArtifactCatalogPage(raw)
      this.#audit.catalogPage(page)
      this.#catalog.mergePage(page, { append: false })
      this.#commit({
        phase: "catalog-ready",
        error: undefined,
      })
      if (!this.#state.selected && page.artifacts[0]) {
        await this.select({
          artifactId: page.artifacts[0].artifactId,
          revision: page.artifacts[0].revision,
          source: "artifact",
          focus: false,
        })
      }
    } catch (error) {
      if (controller.signal.aborted) return
      const failure = workbenchFailure(error)
      this.#audit.failure({
        operation: "catalog-request",
        code: failure.code,
        message: failure.message,
        disconnected: failure.disconnected,
      })
      this.#catalog.failure(failure.code, failure.message, {
        disconnected: failure.disconnected,
        retryable: failure.retryable,
      })
      this.#commit({
        phase: failure.disconnected ? "disconnected" : "failed",
        error: failure,
      })
    } finally {
      if (this.#loadController === controller) this.#loadController = undefined
    }
  }

  async loadNextCatalogPage(): Promise<void> {
    this.#assertOpen()
    const cursor = this.#catalog.state.cursor
    if (!cursor) return
    this.#loadController?.abort("Superseded artifact catalog page.")
    const controller = new AbortController()
    this.#loadController = controller
    const generation = ++this.#generation
    this.#audit.catalogRequest({
      reset: false,
      limit: this.#catalogLimit,
      cursor,
    })
    try {
      const raw = await this.#api.artifactCatalog(this.#taskId, {
        cursor,
        limit: this.#catalogLimit,
        ...catalogServerFilters(this.#catalog.state.query),
        signal: controller.signal,
      })
      if (controller.signal.aborted || generation !== this.#generation) return
      const page = parseArtifactCatalogPage(raw)
      this.#audit.catalogPage(page)
      this.#catalog.mergePage(page, { append: true })
    } catch (error) {
      if (controller.signal.aborted) return
      const failure = workbenchFailure(error)
      this.#audit.failure({
        operation: "catalog-request",
        code: failure.code,
        message: failure.message,
        disconnected: failure.disconnected,
      })
      this.#catalog.failure(failure.code, failure.message, {
        disconnected: failure.disconnected,
        retryable: failure.retryable,
      })
      this.#commit({
        phase: failure.disconnected ? "disconnected" : "failed",
        error: failure,
      })
    } finally {
      if (this.#loadController === controller) this.#loadController = undefined
    }
  }

  updateCatalogQuery(
    patch: Partial<ArtifactCatalogViewQuery>,
  ): ArtifactCatalogState {
    this.#assertOpen()
    return this.#catalog.updateQuery(patch)
  }

  async select(selection: ArtifactCatalogSelection): Promise<void> {
    this.#assertOpen()
    const projected = this.#catalog.select(selection)
    if (!projected) {
      this.#commit({
        phase: "failed",
        error: Object.freeze({
          code: "artifact_not_in_catalog",
          message: "Requested artifact revision is not in the canonical catalog.",
          retryable: true,
          disconnected: false,
          occurredAt: Date.now(),
        }),
      })
      this.#audit.failure({
        operation: "select",
        code: "artifact_not_in_catalog",
        message: "Requested artifact revision is not in the canonical catalog.",
      })
      return
    }
    // Repeated clicks on the revision already loading must not cancel its
    // shared metadata request and then rejoin that cancelled request.
    if (this.#selectionController && !this.#selectionController.signal.aborted
      && this.#state.selected?.artifactId === projected.artifactId
      && this.#state.selected.revision === projected.revision) return
    this.#audit.selection(projected, selection.source)
    this.#selectionController?.abort("Superseded artifact selection.")
    this.#searchController?.abort("Artifact selection changed.")
    this.#contentUnsubscribe?.()
    this.#contentUnsubscribe = undefined
    if (this.#state.selected) this.#media.release(this.#state.selected)
    this.#contentSession?.close()
    this.#contentSession = undefined
    const controller = new AbortController()
    this.#selectionController = controller
    const generation = ++this.#generation
    this.#commit({
      phase: "artifact-loading",
      selected: projected,
      viewer: chooseArtifactViewer(projected),
      model: undefined,
      content: undefined,
      search: undefined,
      admission: undefined,
      error: undefined,
    })
    try {
      const metadataRaw = await this.#api.artifactMetadata(
        this.#taskId,
        projected.artifactId,
        {
          revision: projected.revision,
          signal: controller.signal,
        },
      )
      if (controller.signal.aborted || generation !== this.#generation) return
      const metadata = parseArtifactReadResponse(metadataRaw)
      this.#audit.metadata(metadata)
      const artifact = metadata.artifact
      if (
        artifact.artifactId !== projected.artifactId
        || artifact.revision !== projected.revision
      ) {
        throw new ArtifactWorkbenchError(
          "metadata_selection_mismatch",
          "Artifact metadata does not match the selected immutable revision.",
          false,
        )
      }
      const viewer = this.#viewers.resolve(artifact)
      if (!viewer.allowed || viewer.kind === "metadata" || viewer.kind === "refused") {
        this.#commit({
          phase: "artifact-ready",
          selected: artifact,
          viewer,
          model: buildMetadataViewerModel(artifact, viewer.reason),
          admission: {
            allowed: false,
            mode: "metadata",
            quarantined: artifact.security.trust !== "trusted",
            reasons: Object.freeze(viewer.reason ? [viewer.reason] : []),
            artifactKey: artifactRevisionKey(artifact),
            receiptKey:
              `${metadata.receipt.artifactId}@${metadata.receipt.revision}:`
              + metadata.receipt.receiptDigest,
          },
        })
        return
      }
      const session = new ArtifactContentSession(
        artifact,
        this.#scheduler,
        this.#cache,
      )
      this.#contentSession = session
      this.#contentUnsubscribe = session.listen((content) => {
        const phase =
          content.phase === "failed"
            ? "failed"
            : content.phase === "disconnected"
              ? "disconnected"
              : content.phase === "cancelled"
                ? "cancelled"
                : content.complete
                  ? "artifact-ready"
                  : "partial"
        this.#commit({ content, phase })
      })
      const requested = initialReadLength(artifact, this.#initialRangeBytes)
      const initialResults = await session.ensure(
        { start: 0, end: requested },
        {
          purpose: isMediaViewer(viewer) ? "media" : "preview",
          priority: 100,
          signal: controller.signal,
        },
      )
      for (const result of initialResults) {
        if (!result.fromCache) this.#audit.range(result.response)
      }
      if (
        isMediaViewer(viewer)
        && artifact.sizeBytes <= this.#maximumPreviewBytes
        && artifact.sizeBytes > requested
      ) {
        const mediaResults = await session.ensure(
          { start: requested, end: artifact.sizeBytes },
          {
            purpose: "media",
            priority: 90,
            signal: controller.signal,
          },
        )
        for (const result of mediaResults) {
          if (!result.fromCache) this.#audit.range(result.response)
        }
      }
      if (controller.signal.aborted || generation !== this.#generation) return
      await this.#rebuildModel(artifact, viewer)
    } catch (error) {
      if (controller.signal.aborted) return
      const failure = workbenchFailure(error)
      this.#audit.failure({
        operation: "metadata",
        artifact: projected,
        code: failure.code,
        message: failure.message,
        disconnected: failure.disconnected,
      })
      this.#commit({
        phase: failure.disconnected ? "disconnected" : "failed",
        error: failure,
      })
    } finally {
      if (this.#selectionController === controller) {
        this.#selectionController = undefined
      }
    }
  }

  async loadMoreContent(): Promise<void> {
    this.#assertOpen()
    const artifact = this.#state.selected
    const session = this.#contentSession
    const viewer = this.#state.viewer
    if (!artifact || !session || !viewer) return
    const coverage = this.#scheduler.coverage(artifact)
    const next = coverage.missing[0]
    if (!next) return
    const maximumEnd = Math.min(
      artifact.sizeBytes,
      Math.max(this.#maximumPreviewBytes, next.start + this.#initialRangeBytes),
    )
    if (next.start >= maximumEnd) {
      throw new ArtifactWorkbenchError(
        "preview_bound_reached",
        "Artifact preview reached its configured byte bound.",
        true,
      )
    }
    const results = await session.ensure(
      {
        start: next.start,
        end: Math.min(next.end, next.start + this.#initialRangeBytes, maximumEnd),
      },
      {
        purpose: viewer.capabilities.search ? "search" : "preview",
        priority: 50,
      },
    )
    for (const result of results) {
      if (!result.fromCache) this.#audit.range(result.response)
    }
    await this.#rebuildModel(artifact, viewer)
  }

  async search(
    query: string,
    options: ArtifactSearchOptions = {},
  ): Promise<ArtifactSearchResult | undefined> {
    this.#assertOpen()
    const artifact = this.#state.selected
    const session = this.#contentSession
    const viewer = this.#state.viewer
    if (!artifact || !session || !viewer?.capabilities.search) return undefined
    this.#searchController?.abort("Superseded artifact search.")
    const controller = new AbortController()
    this.#searchController = controller
    const relay = () => controller.abort(options.signal?.reason)
    options.signal?.addEventListener("abort", relay, { once: true })
    try {
      const prefetchEnd = Math.min(
        artifact.sizeBytes,
        this.#searchPrefetchBytes,
      )
      const results = await session.ensure(
        { start: 0, end: prefetchEnd },
        { purpose: "search", priority: 20, signal: controller.signal },
      )
      for (const result of results) {
        if (!result.fromCache) this.#audit.range(result.response)
      }
      const index = session.textIndex()
      const result = await index.search(query, {
        ...options,
        signal: controller.signal,
      })
      if (controller.signal.aborted) return undefined
      this.#audit.append({
        operation: "search",
        artifact,
        details: {
          query_bytes: new TextEncoder().encode(query).byteLength,
          matches: result.matches.length,
          truncated: result.truncated,
          indexed_bytes: result.scannedBytes,
        },
      })
      this.#commit({ search: result })
      await this.#rebuildModel(artifact, viewer, index)
      return result
    } finally {
      options.signal?.removeEventListener("abort", relay)
      if (this.#searchController === controller) this.#searchController = undefined
    }
  }

  bookmark(input: {
    source?: ArtifactBookmark["source"]
    sourceEventId?: string
    pinned?: boolean
  } = {}): ArtifactBookmark | undefined {
    this.#assertOpen()
    const artifact = this.#state.selected
    if (!artifact) return undefined
    const bookmark = this.#bookmarks.add({
      taskId: this.#taskId,
      artifactId: artifact.artifactId,
      revision: artifact.revision,
      title: artifact.title || artifact.artifactId,
      source: input.source ?? "artifact",
      sourceEventId: input.sourceEventId,
      pinned: input.pinned ?? false,
    })
    if (bookmark.pinned) this.#cache.pin(artifact)
    this.#audit.append({
      operation: "bookmark",
      artifact,
      decision: "allow",
      details: {
        action: "add",
        pinned: bookmark.pinned,
        source: bookmark.source,
      },
    })
    this.#commit({ bookmarks: this.#bookmarks.list(this.#taskId) })
    return bookmark
  }

  toggleBookmarkPin(id: string): ArtifactBookmark | undefined {
    this.#assertOpen()
    const before = this.#bookmarks.list(this.#taskId).find((item) => item.id === id)
    const updated = this.#bookmarks.togglePin(id)
    if (!updated) return undefined
    const artifact = this.#catalog.state.artifacts.find(
      (candidate) =>
        candidate.artifactId === updated.artifactId
        && candidate.revision === updated.revision,
    )
    if (artifact) {
      if (updated.pinned && !before?.pinned) this.#cache.pin(artifact)
      if (!updated.pinned && before?.pinned) this.#cache.unpin(artifact)
    }
    this.#audit.append({
      operation: "bookmark",
      artifact: artifact ?? {
        artifactId: updated.artifactId,
        revision: updated.revision,
      },
      decision: "allow",
      details: {
        action: "toggle-pin",
        pinned: updated.pinned,
      },
    })
    this.#commit({ bookmarks: this.#bookmarks.list(this.#taskId) })
    return updated
  }

  removeBookmark(id: string): boolean {
    this.#assertOpen()
    const existing = this.#bookmarks.list(this.#taskId).find((item) => item.id === id)
    const removed = this.#bookmarks.remove(id)
    if (removed && existing?.pinned) {
      const artifact = this.#catalog.state.artifacts.find(
        (candidate) =>
          candidate.artifactId === existing.artifactId
          && candidate.revision === existing.revision,
      )
      if (artifact) this.#cache.unpin(artifact)
    }
    if (removed) this.#commit({ bookmarks: this.#bookmarks.list(this.#taskId) })
    if (removed && existing) {
      this.#audit.append({
        operation: "bookmark",
        artifact: {
          artifactId: existing.artifactId,
          revision: existing.revision,
        },
        decision: "allow",
        details: {
          action: "remove",
          pinned: existing.pinned,
        },
      })
    }
    return removed
  }

  restoreView(): ArtifactViewPreference | undefined {
    const artifact = this.#state.selected
    if (!artifact) return undefined
    return this.#preferences.get(
      this.#taskId,
      artifact.artifactId,
      artifact.revision,
    )
  }

  saveView(
    input: Omit<
      ArtifactViewPreference,
      "taskId" | "artifactId" | "revision" | "updatedAt"
    >,
  ): ArtifactViewPreference | undefined {
    const artifact = this.#state.selected
    if (!artifact) return undefined
    return this.#preferences.update({
      taskId: this.#taskId,
      artifactId: artifact.artifactId,
      revision: artifact.revision,
      ...input,
    })
  }

  downloadInfo():
    | {
        href: string
        allowed: boolean
        confirmationRequired: boolean
        reasons: readonly string[]
      }
    | undefined {
    const artifact = this.#state.selected
    if (!artifact) return undefined
    const decision = downloadDecision(artifact)
    this.#audit.append({
      operation: "download-decision",
      artifact,
      decision: decision.allowed ? "allow" : "deny",
      details: {
        confirmation_required: decision.confirmationRequired,
        reasons: decision.reasons.join(","),
      },
    })
    const query = new URLSearchParams({
      revision: artifact.revision,
      offset: "0",
      length: String(Math.min(artifact.sizeBytes, 1024 * 1024)),
      purpose: "download",
    })
    return Object.freeze({
      href:
        `/tasks/${encodeURIComponent(this.#taskId)}/artifacts/`
        + `${encodeURIComponent(artifact.artifactId)}/download?${query}`,
      ...decision,
    })
  }

  cacheSnapshot(): ArtifactCacheSnapshot {
    return this.#cache.snapshot()
  }

  auditSnapshot(): ArtifactAuditSnapshot {
    this.#audit.updateCache(this.#cache.snapshot())
    return this.#audit.snapshot()
  }

  cancel(reason = "Artifact workbench request cancelled."): void {
    if (this.#closed) return
    this.#loadController?.abort(reason)
    this.#selectionController?.abort(reason)
    this.#searchController?.abort(reason)
    this.#contentSession?.cancel(reason)
    this.#audit.append({
      operation: "cancel",
      artifact: this.#state.selected,
      decision: "cancel",
      details: {
        reason: reason.slice(0, 2048),
      },
    })
    this.#commit({
      phase: "cancelled",
      error: Object.freeze({
        code: "workbench_cancelled",
        message: reason,
        retryable: true,
        disconnected: false,
        occurredAt: Date.now(),
      }),
    })
  }

  close(reason = "Artifact workbench closed."): void {
    if (this.#closed) return
    this.cancel(reason)
    this.#closed = true
    this.#contentUnsubscribe?.()
    this.#catalogUnsubscribe?.()
    this.#navigationUnsubscribe?.()
    this.#disposeBrowserNavigation?.()
    this.#contentSession?.close()
    this.#scheduler.close(reason)
    this.#media.close()
    this.#audit.updateCache(this.#cache.snapshot())
    this.#audit.close()
    this.#navigation.close()
    this.#catalog.close()
    this.#listeners.clear()
    this.#state = freezeWorkbenchState({
      ...this.#state,
      phase: "closed",
      updatedAt: Date.now(),
    })
  }

  async #rebuildModel(
    artifact: ArtifactContract,
    viewer: ArtifactViewerDecision,
    existingIndex?: ArtifactTextLineIndex,
  ): Promise<void> {
    const session = this.#contentSession
    if (!session) return
    const entries = session.entries()
    const safeRanges = entries
      .map((entry) => entry.text)
      .filter((entry): entry is NonNullable<typeof entry> => entry !== undefined)
    const complete = session.state.complete
    let model: ArtifactRenderableModel
    let admission: ArtifactAdmission | undefined
    const newest = entries.at(-1)
    if (newest) {
      admission = {
        allowed: true,
        mode: newest.text ? "text" : "binary",
        quarantined: newest.text?.quarantined ?? false,
        reasons: Object.freeze([]),
        artifactKey: newest.artifactKey,
        receiptKey:
          `${newest.receipt.artifactId}@${newest.receipt.revision}:`
          + newest.receipt.receiptDigest,
      }
    }
    if (viewer.kind === "text") {
      model = buildTextViewerModel({
        artifact,
        index: existingIndex ?? session.textIndex(),
        matches: this.#state.search?.matches,
        partial: !complete,
        safeRanges,
      })
    } else if (viewer.kind === "markdown") {
      const text = combineSafeRanges(safeRanges)
      model = text
        ? buildMarkdownViewerModel({ artifact, text, complete })
        : buildMetadataViewerModel(artifact, "Markdown content is not available.")
    } else if (viewer.kind === "json") {
      const text = combineSafeRanges(safeRanges)
      model = text
        ? buildJsonViewerModel({ artifact, text, complete })
        : buildMetadataViewerModel(artifact, "JSON content is not available.")
    } else if (viewer.kind === "binary") {
      const binary = combineBinaryRanges(entries)
      model = binary
        ? buildBinaryViewerModel({
            artifact,
            bytes: binary.bytes,
            offset: binary.offset,
            complete,
          })
        : buildMetadataViewerModel(artifact, "Binary content is not available.")
    } else if (isMediaViewer(viewer)) {
      if (!complete) {
        model = buildMetadataViewerModel(
          artifact,
          artifact.sizeBytes > this.#maximumPreviewBytes
            ? "Media exceeds the bounded in-browser assembly limit."
            : "Load every verified media range before rendering.",
        )
      } else {
        await this.#media.acquire(artifact, entries)
        model = this.#media.model(artifact, { fit: "contain" })
      }
    } else {
      model = buildMetadataViewerModel(artifact, viewer.reason)
    }
    this.#commit({
      selected: artifact,
      viewer,
      model,
      content: session.state,
      admission,
      phase: complete ? "artifact-ready" : "partial",
      error: undefined,
    })
    this.#audit.updateCache(this.#cache.snapshot())
    if (isMediaViewer(viewer) && complete) {
      this.#audit.append({
        operation: "media-acquire",
        artifact,
        decision: "allow",
        details: {
          bytes: artifact.sizeBytes,
          content_family: artifact.contentFamily,
        },
      })
    }
  }

  #commit(patch: Partial<ArtifactWorkbenchState>): void {
    this.#state = freezeWorkbenchState({
      ...this.#state,
      ...patch,
      generation: patch.generation ?? this.#state.generation + 1,
      updatedAt: patch.updatedAt ?? Date.now(),
    })
    for (const listener of this.#listeners) listener(this.#state)
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new ArtifactWorkbenchError(
        "workbench_closed",
        "Artifact workbench is closed.",
        false,
      )
    }
  }
}

export class ArtifactWorkbenchError extends Error {
  readonly code: string
  readonly retryable: boolean

  constructor(code: string, message: string, retryable: boolean) {
    super(message)
    this.name = "ArtifactWorkbenchError"
    this.code = code
    this.retryable = retryable
  }
}

function combineSafeRanges(
  ranges: readonly NonNullable<
    ReturnType<ArtifactContentSession["entries"]>[number]["text"]
  >[],
): NonNullable<
  ReturnType<ArtifactContentSession["entries"]>[number]["text"]
> | undefined {
  if (!ranges.length) return undefined
  const sorted = [...ranges].sort((left, right) => {
    const leftOffset = Number(left.rangeKey.match(/:(\d+)-/)?.[1] ?? 0)
    const rightOffset = Number(right.rangeKey.match(/:(\d+)-/)?.[1] ?? 0)
    return leftOffset - rightOffset
  })
  const first = sorted[0]!
  return Object.freeze({
    ...first,
    text: sorted.map((range) => range.text).join(""),
    clientRedacted: sorted.some((range) => range.clientRedacted),
    serverRedacted: sorted.some((range) => range.serverRedacted),
    quarantined: sorted.some((range) => range.quarantined),
    redactions: Object.freeze(sorted.flatMap((range) => range.redactions)),
    serverRedactions: Object.freeze(
      sorted.flatMap((range) => range.serverRedactions),
    ),
    promptFindings: Object.freeze(
      sorted.flatMap((range) => range.promptFindings),
    ),
    serverPromptFindings: Object.freeze(
      sorted.flatMap((range) => range.serverPromptFindings),
    ),
    transformations: Object.freeze(
      [...new Set(sorted.flatMap((range) => range.transformations))],
    ),
  })
}

function combineBinaryRanges(
  entries: ReturnType<ArtifactContentSession["entries"]>,
): { offset: number; bytes: Uint8Array } | undefined {
  const selected = entries
    .filter((entry) => entry.binary)
    .sort((left, right) => left.range.offset - right.range.offset)
  if (!selected.length) return undefined
  const start = selected[0]!.range.offset
  let end = start
  const chunks: Uint8Array[] = []
  for (const entry of selected) {
    if (entry.range.offset > end) break
    const binary = entry.binary!
    const overlap = Math.max(0, end - entry.range.offset)
    if (overlap < binary.length) chunks.push(binary.slice(overlap))
    end = Math.max(end, entry.range.endExclusive)
  }
  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
  const bytes = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    bytes.set(chunk, offset)
    offset += chunk.length
  }
  return { offset: start, bytes }
}

function initialReadLength(
  artifact: ArtifactContract,
  chunkBytes: number,
): number {
  if (artifact.sizeBytes === 0) return 0
  if (isMediaViewer(chooseArtifactViewer(artifact))) {
    return Math.min(artifact.sizeBytes, chunkBytes)
  }
  return Math.min(artifact.sizeBytes, chunkBytes)
}

function isMediaViewer(viewer: ArtifactViewerDecision): boolean {
  return viewer.kind === "image" || viewer.kind === "audio" || viewer.kind === "video"
}

function workbenchFailure(error: unknown): ArtifactWorkbenchFailure {
  if (error instanceof ArtifactWorkbenchError) {
    return Object.freeze({
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      disconnected: false,
      occurredAt: Date.now(),
    })
  }
  if (error instanceof ArtifactContentError) {
    return Object.freeze({
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      disconnected: error.disconnected,
      occurredAt: Date.now(),
    })
  }
  const message = error instanceof Error ? error.message : String(error)
  const lower = message.toLowerCase()
  const disconnected =
    lower.includes("network")
    || lower.includes("fetch")
    || lower.includes("connection")
    || lower.includes("disconnected")
  return Object.freeze({
    code: disconnected ? "artifact_disconnected" : "artifact_workbench_failed",
    message: message || "Artifact workbench operation failed.",
    retryable: true,
    disconnected,
    occurredAt: Date.now(),
  })
}

function freezeWorkbenchState(
  state: ArtifactWorkbenchState,
): ArtifactWorkbenchState {
  return Object.freeze({
    ...state,
    bookmarks: Object.freeze([...state.bookmarks]),
  })
}

function safeIdentity(value: string, label: string): string {
  const selected = String(value || "").trim()
  if (
    !selected
    || /[\r\n\u0000]/.test(selected)
    || new TextEncoder().encode(selected).byteLength > 512
  ) {
    throw new ArtifactWorkbenchError(
      `${label}_identity_invalid`,
      `${label} identity is invalid.`,
      false,
    )
  }
  return selected
}

function boundedInteger(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const selected = value ?? fallback
  if (!Number.isSafeInteger(selected) || selected < minimum || selected > maximum) {
    throw new ArtifactWorkbenchError(
      "workbench_limit_invalid",
      `Artifact workbench limit must be between ${minimum} and ${maximum}.`,
      false,
    )
  }
  return selected
}
