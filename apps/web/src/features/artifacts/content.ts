import type { TaskApi } from "../../api/task-api.ts"
import {
  artifactRevisionKey,
  parseArtifactReadResponse,
  type ArtifactContract,
  type ArtifactReadPurpose,
  type ArtifactReadRange,
  type ArtifactReadResponse,
} from "./contracts.ts"
import {
  ArtifactContentCache,
  type ArtifactCacheEntry,
  type ArtifactCoverage,
  type ArtifactInterval,
} from "./cache.ts"
import {
  ArtifactReceiptAdmission,
  ArtifactSecurityError,
  secureArtifactText,
  type ArtifactAdmission,
  type ArtifactSafeText,
} from "./security.ts"
import { ArtifactTextSearchIndex } from "./search-index.ts"

export type ArtifactContentPhase =
  | "idle"
  | "queued"
  | "loading"
  | "partial"
  | "ready"
  | "cancelled"
  | "failed"
  | "disconnected"

export interface ArtifactContentState {
  artifactKey: string
  phase: ArtifactContentPhase
  generation: number
  requested: readonly ArtifactInterval[]
  admitted: readonly ArtifactInterval[]
  activeRequests: number
  loadedBytes: number
  totalBytes: number
  complete: boolean
  error?: ArtifactContentFailure
  updatedAt: number
}

export interface ArtifactContentFailure {
  code: string
  message: string
  retryable: boolean
  disconnected: boolean
  range?: ArtifactInterval
  occurredAt: number
}

export interface ArtifactRangeRequest {
  artifact: ArtifactContract
  offset: number
  length: number
  purpose?: ArtifactReadPurpose
  priority?: number
  signal?: AbortSignal
}

export interface ArtifactRangeResult {
  artifact: ArtifactContract
  response: ArtifactReadResponse
  admission: ArtifactAdmission
  cacheEntry: ArtifactCacheEntry
  fromCache: boolean
}

export interface ArtifactRangeSchedulerOptions {
  chunkBytes?: number
  maximumConcurrent?: number
  maximumQueued?: number
  timeoutMs?: number
}

interface QueuedRead {
  id: number
  artifact: ArtifactContract
  offset: number
  length: number
  purpose: ArtifactReadPurpose
  priority: number
  signal?: AbortSignal
  resolve: (value: ArtifactRangeResult) => void
  reject: (reason: unknown) => void
  queuedAt: number
  controller: AbortController
}

export interface ArtifactTextLine {
  lineNumber: number
  startOffset: number
  endOffset: number
  text: string
  complete: boolean
  rangeKeys: readonly string[]
}

export interface ArtifactLineWindow {
  startLine: number
  endLine: number
  overscanStartLine: number
  overscanEndLine: number
  beforeHeight: number
  afterHeight: number
  totalHeight: number
  lines: readonly ArtifactTextLine[]
}

export interface ArtifactSearchOptions {
  caseSensitive?: boolean
  wholeWord?: boolean
  regularExpression?: boolean
  maximumResults?: number
  signal?: AbortSignal
}

export interface ArtifactSearchMatch {
  id: string
  lineNumber: number
  startColumn: number
  endColumn: number
  startOffset: number
  endOffset: number
  preview: string
  completeLine: boolean
}

export interface ArtifactSearchResult {
  query: string
  matches: readonly ArtifactSearchMatch[]
  scannedLines: number
  scannedBytes: number
  truncated: boolean
  cancelled: boolean
  elapsedMs: number
}

export type ArtifactContentListener = (state: ArtifactContentState) => void

const DEFAULT_CHUNK_BYTES = 256 * 1024
const MAX_CHUNK_BYTES = 1024 * 1024
const DEFAULT_CONCURRENT = 3
const DEFAULT_QUEUE = 128
const DEFAULT_TIMEOUT = 30_000

export class ArtifactRangeScheduler {
  readonly #api: TaskApi
  readonly #taskId: string
  readonly #cache: ArtifactContentCache
  readonly #admission: ArtifactReceiptAdmission
  readonly #chunkBytes: number
  readonly #maximumConcurrent: number
  readonly #maximumQueued: number
  readonly #timeoutMs: number
  readonly #queue: QueuedRead[] = []
  readonly #active = new Map<number, QueuedRead>()
  readonly #deduplicated = new Map<string, Promise<ArtifactRangeResult>>()
  #nextId = 1
  #closed = false

  constructor(input: {
    api: TaskApi
    taskId: string
    cache: ArtifactContentCache
    admission: ArtifactReceiptAdmission
    options?: ArtifactRangeSchedulerOptions
  }) {
    this.#api = input.api
    this.#taskId = safeIdentity(input.taskId, "task")
    this.#cache = input.cache
    this.#admission = input.admission
    this.#chunkBytes = boundedInteger(
      input.options?.chunkBytes,
      DEFAULT_CHUNK_BYTES,
      1024,
      MAX_CHUNK_BYTES,
    )
    this.#maximumConcurrent = boundedInteger(
      input.options?.maximumConcurrent,
      DEFAULT_CONCURRENT,
      1,
      16,
    )
    this.#maximumQueued = boundedInteger(
      input.options?.maximumQueued,
      DEFAULT_QUEUE,
      1,
      10_000,
    )
    this.#timeoutMs = boundedInteger(
      input.options?.timeoutMs,
      DEFAULT_TIMEOUT,
      1_000,
      300_000,
    )
  }

  read(input: ArtifactRangeRequest): Promise<ArtifactRangeResult> {
    this.#assertOpen()
    const request = normalizeRangeRequest(input, this.#chunkBytes)
    if (request.artifact.security.label === "secret") {
      return Promise.reject(
        new ArtifactContentError(
          "secret_content_refused",
          "Secret artifact content cannot be scheduled.",
          false,
        ),
      )
    }
    const endExclusive = Math.min(
      request.artifact.sizeBytes,
      request.offset + request.length,
    )
    const cached = this.#cache.getContaining(
      request.artifact,
      request.offset,
      endExclusive,
    )
    if (cached) {
      return Promise.resolve({
        artifact: request.artifact,
        response: cacheEntryResponse(request.artifact, cached),
        admission: {
          allowed: true,
          mode: cached.text ? "text" : "binary",
          quarantined: cached.text?.quarantined ?? false,
          reasons: [],
          artifactKey: cached.artifactKey,
          receiptKey:
            `${cached.receipt.artifactId}@${cached.receipt.revision}:`
            + cached.receipt.receiptDigest,
        },
        cacheEntry: cached,
        fromCache: true,
      })
    }
    const key = requestKey(request)
    const existing = this.#deduplicated.get(key)
    if (existing) return existing
    if (this.#queue.length >= this.#maximumQueued) {
      return Promise.reject(
        new ArtifactContentError(
          "range_queue_full",
          "Artifact range queue reached its bounded capacity.",
          true,
        ),
      )
    }
    const promise = new Promise<ArtifactRangeResult>((resolve, reject) => {
      const controller = new AbortController()
      const queued: QueuedRead = {
        id: this.#nextId++,
        artifact: request.artifact,
        offset: request.offset,
        length: request.length,
        purpose: request.purpose,
        priority: request.priority,
        signal: request.signal,
        resolve,
        reject,
        queuedAt: Date.now(),
        controller,
      }
      if (request.signal?.aborted) {
        reject(
          new ArtifactContentError(
            "range_cancelled",
            "Artifact range request was cancelled before admission.",
            true,
          ),
        )
        return
      }
      this.#queue.push(queued)
      this.#sortQueue()
      this.#pump()
    }).finally(() => {
      this.#deduplicated.delete(key)
    })
    this.#deduplicated.set(key, promise)
    return promise
  }

  async readCoverage(
    artifact: ArtifactContract,
    interval: ArtifactInterval,
    options: {
      purpose?: ArtifactReadPurpose
      priority?: number
      signal?: AbortSignal
    } = {},
  ): Promise<readonly ArtifactRangeResult[]> {
    this.#assertOpen()
    const coverage = this.#cache.coverage(artifact, interval)
    if (coverage.complete) {
      return Object.freeze(
        this.#cache
          .ranges(artifact)
          .filter(
            (entry) =>
              entry.range.endExclusive > interval.start
              && entry.range.offset < interval.end,
          )
          .map((entry) => ({
            artifact,
            response: cacheEntryResponse(artifact, entry),
            admission: {
              allowed: true,
              mode: entry.text ? "text" as const : "binary" as const,
              quarantined: entry.text?.quarantined ?? false,
              reasons: Object.freeze([]),
              artifactKey: entry.artifactKey,
              receiptKey:
                `${entry.receipt.artifactId}@${entry.receipt.revision}:`
                + entry.receipt.receiptDigest,
            },
            cacheEntry: entry,
            fromCache: true,
          })),
      )
    }
    const requests: Promise<ArtifactRangeResult>[] = []
    for (const missing of coverage.missing) {
      let offset = missing.start
      while (offset < missing.end) {
        const length = Math.min(this.#chunkBytes, missing.end - offset)
        requests.push(
          this.read({
            artifact,
            offset,
            length,
            purpose: options.purpose,
            priority: options.priority,
            signal: options.signal,
          }),
        )
        offset += length
      }
    }
    return Object.freeze(await Promise.all(requests))
  }

  coverage(
    artifact: ArtifactContract,
    interval: ArtifactInterval = { start: 0, end: artifact.sizeBytes },
  ): ArtifactCoverage {
    return this.#cache.coverage(artifact, interval)
  }

  cancelArtifact(
    artifact: Pick<ArtifactContract, "artifactId" | "revision">,
    reason = "Artifact viewer closed.",
  ): number {
    const key = artifactRevisionKey(artifact)
    let cancelled = 0
    for (let index = this.#queue.length - 1; index >= 0; index -= 1) {
      const request = this.#queue[index]!
      if (artifactRevisionKey(request.artifact) !== key) continue
      this.#queue.splice(index, 1)
      request.controller.abort(reason)
      request.reject(
        new ArtifactContentError("range_cancelled", reason, true),
      )
      cancelled += 1
    }
    for (const request of this.#active.values()) {
      if (artifactRevisionKey(request.artifact) !== key) continue
      request.controller.abort(reason)
      cancelled += 1
    }
    return cancelled
  }

  snapshot(): {
    queued: number
    active: number
    deduplicated: number
    closed: boolean
  } {
    return Object.freeze({
      queued: this.#queue.length,
      active: this.#active.size,
      deduplicated: this.#deduplicated.size,
      closed: this.#closed,
    })
  }

  close(reason = "Artifact range scheduler closed."): void {
    if (this.#closed) return
    this.#closed = true
    for (const request of this.#queue.splice(0)) {
      request.controller.abort(reason)
      request.reject(new ArtifactContentError("scheduler_closed", reason, false))
    }
    for (const request of this.#active.values()) request.controller.abort(reason)
    this.#active.clear()
    this.#deduplicated.clear()
  }

  #sortQueue(): void {
    this.#queue.sort((left, right) => {
      if (left.priority !== right.priority) return right.priority - left.priority
      return left.queuedAt - right.queuedAt || left.id - right.id
    })
  }

  #pump(): void {
    if (this.#closed) return
    while (
      this.#active.size < this.#maximumConcurrent
      && this.#queue.length > 0
    ) {
      const request = this.#queue.shift()!
      this.#active.set(request.id, request)
      void this.#execute(request)
    }
  }

  async #execute(request: QueuedRead): Promise<void> {
    const timeout = setTimeout(
      () => request.controller.abort("Artifact range request timed out."),
      this.#timeoutMs,
    )
    const relayAbort = () =>
      request.controller.abort(request.signal?.reason ?? "Artifact range cancelled.")
    request.signal?.addEventListener("abort", relayAbort, { once: true })
    try {
      const raw = await this.#api.artifactContent(
        this.#taskId,
        request.artifact.artifactId,
        {
          revision: request.artifact.revision,
          offset: request.offset,
          length: request.length,
          purpose: request.purpose,
          signal: request.controller.signal,
          timeoutMs: this.#timeoutMs,
        },
      )
      if (request.controller.signal.aborted) {
        throw new ArtifactContentError(
          "range_cancelled",
          String(request.controller.signal.reason || "Artifact range cancelled."),
          true,
        )
      }
      const response = parseArtifactReadResponse(raw)
      assertResponseIdentity(request, response)
      const admission = this.#admission.admit(response)
      const range = response.range
      const content = response.content
      if (!range || !content) {
        throw new ArtifactContentError(
          "range_content_missing",
          "Artifact range response contains no content.",
          true,
        )
      }
      let cacheEntry: ArtifactCacheEntry
      if (content.text !== undefined) {
        const safe = await secureArtifactText(response, admission)
        cacheEntry = this.#cache.putText(
          response.artifact,
          range,
          safe,
          response.receipt,
        )
      } else if (content.base64 !== undefined) {
        const binary = decodeBase64(content.base64)
        if (binary.byteLength !== range.length) {
          throw new ArtifactContentError(
            "binary_range_length_mismatch",
            "Decoded artifact bytes do not match the admitted range.",
            false,
          )
        }
        cacheEntry = this.#cache.putBinary(
          response.artifact,
          range,
          binary,
          response.receipt,
        )
      } else {
        throw new ArtifactContentError(
          "content_representation_missing",
          "Artifact response contains no supported representation.",
          false,
        )
      }
      request.resolve({
        artifact: response.artifact,
        response,
        admission,
        cacheEntry,
        fromCache: false,
      })
    } catch (error) {
      request.reject(normalizeContentError(error, request.controller.signal))
    } finally {
      clearTimeout(timeout)
      request.signal?.removeEventListener("abort", relayAbort)
      this.#active.delete(request.id)
      this.#pump()
    }
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new ArtifactContentError(
        "scheduler_closed",
        "Artifact range scheduler is closed.",
        false,
      )
    }
  }
}

export class ArtifactContentSession {
  readonly #artifact: ArtifactContract
  readonly #scheduler: ArtifactRangeScheduler
  readonly #cache: ArtifactContentCache
  readonly #listeners = new Set<ArtifactContentListener>()
  #state: ArtifactContentState
  #generation = 0
  #closed = false

  constructor(
    artifact: ArtifactContract,
    scheduler: ArtifactRangeScheduler,
    cache: ArtifactContentCache,
  ) {
    this.#artifact = artifact
    this.#scheduler = scheduler
    this.#cache = cache
    this.#state = freezeContentState({
      artifactKey: artifactRevisionKey(artifact),
      phase: "idle",
      generation: 0,
      requested: [],
      admitted: [],
      activeRequests: 0,
      loadedBytes: 0,
      totalBytes: artifact.sizeBytes,
      complete: artifact.sizeBytes === 0,
      updatedAt: Date.now(),
    })
  }

  get state(): ArtifactContentState {
    return this.#state
  }

  listen(listener: ArtifactContentListener): () => void {
    this.#assertOpen()
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  async ensure(
    interval: ArtifactInterval,
    options: {
      purpose?: ArtifactReadPurpose
      priority?: number
      signal?: AbortSignal
    } = {},
  ): Promise<readonly ArtifactRangeResult[]> {
    this.#assertOpen()
    const request = normalizeInterval(interval, this.#artifact.sizeBytes)
    const generation = ++this.#generation
    this.#commit({
      phase: "loading",
      generation,
      requested: mergeContentIntervals([...this.#state.requested, request]),
      activeRequests: this.#state.activeRequests + 1,
      error: undefined,
    })
    try {
      const results = await this.#scheduler.readCoverage(
        this.#artifact,
        request,
        options,
      )
      if (this.#closed || generation !== this.#generation) return results
      this.#refreshCoverage()
      return results
    } catch (error) {
      if (this.#closed || generation !== this.#generation) throw error
      const failure = normalizeContentError(error, options.signal)
      this.#commit({
        phase:
          failure.code === "range_cancelled"
            ? "cancelled"
            : failure.disconnected
              ? "disconnected"
              : "failed",
        activeRequests: Math.max(0, this.#state.activeRequests - 1),
        error: Object.freeze({
          code: failure.code,
          message: failure.message,
          retryable: failure.retryable,
          disconnected: failure.disconnected,
          range: request,
          occurredAt: Date.now(),
        }),
      })
      throw failure
    }
  }

  entries(): readonly ArtifactCacheEntry[] {
    return this.#cache.ranges(this.#artifact)
  }

  textIndex(): ArtifactTextLineIndex {
    const index = new ArtifactTextLineIndex(this.#artifact)
    for (const entry of this.entries()) {
      if (entry.text) index.admit(entry)
    }
    return index
  }

  cancel(reason = "Artifact content session cancelled."): void {
    if (this.#closed) return
    this.#generation += 1
    this.#scheduler.cancelArtifact(this.#artifact, reason)
    this.#commit({
      phase: "cancelled",
      activeRequests: 0,
      error: Object.freeze({
        code: "range_cancelled",
        message: reason,
        retryable: true,
        disconnected: false,
        occurredAt: Date.now(),
      }),
    })
  }

  close(): void {
    if (this.#closed) return
    this.cancel("Artifact viewer closed.")
    this.#closed = true
    this.#listeners.clear()
  }

  #refreshCoverage(): void {
    const coverage = this.#scheduler.coverage(this.#artifact)
    const entries = this.#cache.ranges(this.#artifact)
    const loadedBytes = coverage.covered.reduce(
      (sum, interval) => sum + interval.end - interval.start,
      0,
    )
    this.#commit({
      phase: coverage.complete ? "ready" : "partial",
      admitted: coverage.covered,
      activeRequests: Math.max(0, this.#state.activeRequests - 1),
      loadedBytes,
      complete: coverage.complete,
      error: undefined,
      updatedAt: entries.reduce(
        (latest, entry) => Math.max(latest, entry.accessedAt),
        Date.now(),
      ),
    })
  }

  #commit(patch: Partial<ArtifactContentState>): void {
    this.#state = freezeContentState({
      ...this.#state,
      ...patch,
      generation: patch.generation ?? this.#state.generation,
      updatedAt: patch.updatedAt ?? Date.now(),
    })
    for (const listener of this.#listeners) listener(this.#state)
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new ArtifactContentError(
        "content_session_closed",
        "Artifact content session is closed.",
        false,
      )
    }
  }
}

export class ArtifactTextLineIndex {
  readonly #artifact: ArtifactContract
  readonly #segments = new Map<string, {
    key: string
    offset: number
    endExclusive: number
    text: ArtifactSafeText
  }>()
  readonly #searchIndex = new ArtifactTextSearchIndex({
    maximumDocuments: 250_000,
    maximumBytes: 64 * 1024 * 1024,
  })
  #lines: ArtifactTextLine[] = []
  #dirty = true

  constructor(artifact: ArtifactContract) {
    this.#artifact = artifact
  }

  admit(entry: ArtifactCacheEntry): void {
    if (
      entry.artifactId !== this.#artifact.artifactId
      || entry.revision !== this.#artifact.revision
      || !entry.text
    ) {
      throw new ArtifactContentError(
        "line_index_identity_mismatch",
        "Text range does not belong to this artifact revision.",
        false,
      )
    }
    const existing = this.#segments.get(entry.key)
    if (existing && existing.text.text !== entry.text.text) {
      throw new ArtifactContentError(
        "line_index_range_conflict",
        "Immutable text range changed after admission.",
        false,
      )
    }
    this.#segments.set(entry.key, {
      key: entry.key,
      offset: entry.range.offset,
      endExclusive: entry.range.endExclusive,
      text: entry.text,
    })
    this.#dirty = true
  }

  lines(): readonly ArtifactTextLine[] {
    this.#rebuild()
    return Object.freeze(this.#lines.map((line) => Object.freeze({ ...line })))
  }

  line(lineNumber: number): ArtifactTextLine | undefined {
    this.#rebuild()
    const selected = this.#lines[lineNumber - 1]
    return selected ? Object.freeze({ ...selected }) : undefined
  }

  window(options: {
    scrollTop: number
    viewportHeight: number
    lineHeight?: number
    overscan?: number
  }): ArtifactLineWindow {
    this.#rebuild()
    const lineHeight = clampNumber(options.lineHeight ?? 20, 12, 80)
    const scrollTop = Math.max(0, finiteNumber(options.scrollTop, 0))
    const viewport = Math.max(1, finiteNumber(options.viewportHeight, 1))
    const overscan = boundedInteger(options.overscan, 20, 0, 500)
    const start = Math.min(
      this.#lines.length,
      Math.floor(scrollTop / lineHeight),
    )
    const end = Math.min(
      this.#lines.length,
      Math.ceil((scrollTop + viewport) / lineHeight),
    )
    const overscanStart = Math.max(0, start - overscan)
    const overscanEnd = Math.min(this.#lines.length, end + overscan)
    return Object.freeze({
      startLine: start + 1,
      endLine: end,
      overscanStartLine: overscanStart + 1,
      overscanEndLine: overscanEnd,
      beforeHeight: overscanStart * lineHeight,
      afterHeight: Math.max(0, (this.#lines.length - overscanEnd) * lineHeight),
      totalHeight: this.#lines.length * lineHeight,
      lines: Object.freeze(
        this.#lines
          .slice(overscanStart, overscanEnd)
          .map((line) => Object.freeze({ ...line })),
      ),
    })
  }

  async search(
    query: string,
    options: ArtifactSearchOptions = {},
  ): Promise<ArtifactSearchResult> {
    this.#rebuild()
    const result = await this.#searchIndex.search(query, options)
    return Object.freeze({
      query: result.query,
      matches: Object.freeze(
        result.matches.map((match) =>
          Object.freeze({
            id: match.id,
            lineNumber: match.lineNumber,
            startColumn: match.startColumn,
            endColumn: match.endColumn,
            startOffset: match.startOffset,
            endOffset: match.endOffset,
            preview: match.preview,
            completeLine: match.completeLine,
          }),
        ),
      ),
      scannedLines: result.scannedDocuments,
      scannedBytes: result.scannedBytes,
      truncated: result.truncated,
      cancelled: result.cancelled,
      elapsedMs: result.elapsedMs,
    })
  }

  #rebuild(): void {
    if (!this.#dirty) return
    const segments = [...this.#segments.values()].sort(
      (left, right) => left.offset - right.offset || left.endExclusive - right.endExclusive,
    )
    const lines: ArtifactTextLine[] = []
    let lineNumber = 1
    for (const segment of segments) {
      const normalized = normalizeLineEndings(segment.text.text)
      let relativeOffset = 0
      const rawLines = normalized.split("\n")
      rawLines.forEach((text, index) => {
        const newlineBytes = index < rawLines.length - 1 ? 1 : 0
        const startOffset = segment.offset + relativeOffset
        const encodedBytes = new TextEncoder().encode(text).byteLength
        const endOffset = startOffset + encodedBytes
        const previous = lines.at(-1)
        const segmentStartsMidLine =
          segment.offset > 0
          && index === 0
          && !segment.text.text.startsWith("\n")
        if (segmentStartsMidLine && previous && !previous.complete) {
          const mergedText = `${previous.text}${text}`
          lines[lines.length - 1] = Object.freeze({
            ...previous,
            endOffset,
            text: mergedText,
            complete: newlineBytes === 1,
            rangeKeys: Object.freeze(
              [...new Set([...previous.rangeKeys, segment.key])],
            ),
          })
        } else {
          lines.push(
            Object.freeze({
              lineNumber,
              startOffset,
              endOffset,
              text,
              complete:
                newlineBytes === 1
                || segment.endExclusive >= this.#artifact.sizeBytes,
              rangeKeys: Object.freeze([segment.key]),
            }),
          )
          lineNumber += 1
        }
        relativeOffset += encodedBytes + newlineBytes
      })
    }
    this.#lines = lines.map((line, index) =>
      Object.freeze({ ...line, lineNumber: index + 1 }),
    )
    this.#searchIndex.clear()
    this.#searchIndex.upsertMany(
      this.#lines.map((line) => ({
        id:
          `${this.#artifact.artifactId}@${this.#artifact.revision}:`
          + `${line.lineNumber}:${line.startOffset}-${line.endOffset}`,
        lineNumber: line.lineNumber,
        text: line.text,
        startOffset: line.startOffset,
        endOffset: line.endOffset,
        complete: line.complete,
      })),
    )
    this.#dirty = false
  }
}

export class ArtifactContentError extends Error {
  readonly code: string
  readonly retryable: boolean
  readonly disconnected: boolean

  constructor(
    code: string,
    message: string,
    retryable: boolean,
    disconnected = false,
  ) {
    super(message)
    this.name = "ArtifactContentError"
    this.code = code
    this.retryable = retryable
    this.disconnected = disconnected
  }
}

function normalizeRangeRequest(
  input: ArtifactRangeRequest,
  defaultLength: number,
): Required<Omit<ArtifactRangeRequest, "signal">> & { signal?: AbortSignal } {
  const artifact = input.artifact
  const offset = boundedInteger(
    input.offset,
    0,
    0,
    Math.max(0, artifact.sizeBytes),
  )
  const length = boundedInteger(
    input.length,
    defaultLength,
    1,
    MAX_CHUNK_BYTES,
  )
  const purposes = new Set<ArtifactReadPurpose>([
    "preview",
    "search",
    "media",
    "download",
    "metadata",
  ])
  const purpose = input.purpose ?? "preview"
  if (!purposes.has(purpose)) {
    throw new ArtifactContentError(
      "range_purpose_invalid",
      "Artifact range purpose is invalid.",
      false,
    )
  }
  return {
    artifact,
    offset,
    length: Math.min(length, Math.max(1, artifact.sizeBytes - offset || 1)),
    purpose,
    priority: boundedInteger(input.priority, 0, -1000, 1000),
    signal: input.signal,
  }
}

function assertResponseIdentity(
  request: QueuedRead,
  response: ArtifactReadResponse,
): void {
  if (request.artifact.artifactId !== response.artifact.artifactId) {
    throw new ArtifactContentError(
      "response_artifact_mismatch",
      "Artifact range response belongs to a different artifact.",
      false,
    )
  }
  if (request.artifact.revision !== response.artifact.revision) {
    throw new ArtifactContentError(
      "response_revision_mismatch",
      "Artifact range response belongs to a different immutable revision.",
      false,
    )
  }
  if (request.artifact.sha256 !== response.artifact.sha256) {
    throw new ArtifactContentError(
      "response_digest_mismatch",
      "Artifact range response digest changed.",
      false,
    )
  }
  if (!response.range) {
    throw new ArtifactContentError(
      "response_range_missing",
      "Artifact range response omitted range metadata.",
      false,
    )
  }
  if (response.range.offset !== request.offset) {
    throw new ArtifactContentError(
      "response_range_offset_mismatch",
      "Artifact range response starts at an unexpected offset.",
      false,
    )
  }
  if (response.range.length > request.length) {
    throw new ArtifactContentError(
      "response_range_overflow",
      "Artifact range response exceeds the requested bound.",
      false,
    )
  }
}

function cacheEntryResponse(
  artifact: ArtifactContract,
  entry: ArtifactCacheEntry,
): ArtifactReadResponse {
  return Object.freeze({
    schema: "zyra.artifact-read.v2",
    taskId: entry.receipt.taskId,
    artifact,
    policy: Object.freeze({
      securityLabel: artifact.security.label,
      trustDisposition: artifact.security.trust,
      downloadPolicy: artifact.security.downloadPolicy,
      allowInline: true,
      allowDownload: artifact.security.downloadPolicy !== "deny",
      quarantine: entry.text?.quarantined ?? false,
      reasons: Object.freeze([]),
    }),
    range: entry.range,
    content: Object.freeze({
      text: entry.text?.text,
      base64: entry.binary ? encodeBase64(entry.binary) : undefined,
      encoding: artifact.encoding,
      decodeStatus: entry.text ? "cached" : "binary",
      serverRedacted: entry.text?.serverRedacted ?? false,
      redactions: entry.text?.serverRedactions ?? Object.freeze([]),
      promptFindings: entry.text?.serverPromptFindings ?? Object.freeze([]),
      quarantined: entry.text?.quarantined ?? false,
    }),
    receipt: entry.receipt,
  })
}

function requestKey(
  request: Required<Omit<ArtifactRangeRequest, "signal">> & {
    signal?: AbortSignal
  },
): string {
  return [
    request.artifact.artifactId,
    request.artifact.revision,
    request.offset,
    request.length,
    request.purpose,
  ].join(":")
}

function decodeBase64(value: string): Uint8Array {
  if (typeof atob === "function") {
    const decoded = atob(value)
    const result = new Uint8Array(decoded.length)
    for (let index = 0; index < decoded.length; index += 1) {
      result[index] = decoded.charCodeAt(index)
    }
    return result
  }
  const alphabet =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
  const cleaned = value.replace(/=+$/g, "")
  const bytes: number[] = []
  let buffer = 0
  let bits = 0
  for (const character of cleaned) {
    const index = alphabet.indexOf(character)
    if (index < 0) {
      throw new ArtifactContentError(
        "base64_invalid",
        "Artifact base64 content is invalid.",
        false,
      )
    }
    buffer = (buffer << 6) | index
    bits += 6
    if (bits >= 8) {
      bits -= 8
      bytes.push((buffer >> bits) & 0xff)
    }
  }
  return new Uint8Array(bytes)
}

function encodeBase64(value: Uint8Array): string {
  if (typeof btoa === "function") {
    let binary = ""
    for (let index = 0; index < value.length; index += 1) {
      binary += String.fromCharCode(value[index]!)
    }
    return btoa(binary)
  }
  const alphabet =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
  let result = ""
  for (let index = 0; index < value.length; index += 3) {
    const left = value[index]!
    const middle = value[index + 1]
    const right = value[index + 2]
    const packed = (left << 16) | ((middle ?? 0) << 8) | (right ?? 0)
    result += alphabet[(packed >> 18) & 63]
    result += alphabet[(packed >> 12) & 63]
    result += middle === undefined ? "=" : alphabet[(packed >> 6) & 63]
    result += right === undefined ? "=" : alphabet[packed & 63]
  }
  return result
}

function normalizeContentError(
  error: unknown,
  signal?: AbortSignal,
): ArtifactContentError {
  if (error instanceof ArtifactContentError) return error
  if (error instanceof ArtifactSecurityError) {
    return new ArtifactContentError(error.code, error.message, false)
  }
  if (signal?.aborted || (error instanceof Error && error.name === "AbortError")) {
    return new ArtifactContentError(
      "range_cancelled",
      String(signal?.reason || "Artifact range request was cancelled."),
      true,
    )
  }
  const message = error instanceof Error ? error.message : String(error)
  const lower = message.toLowerCase()
  const disconnected =
    lower.includes("network")
    || lower.includes("fetch")
    || lower.includes("disconnected")
    || lower.includes("connection")
  return new ArtifactContentError(
    disconnected ? "artifact_disconnected" : "artifact_read_failed",
    message || "Artifact range read failed.",
    true,
    disconnected,
  )
}

function normalizeLineEndings(value: string): string {
  return value.replace(/\r\n/g, "\n").replace(/\r/g, "\n")
}

function normalizeInterval(
  interval: ArtifactInterval,
  totalBytes: number,
): ArtifactInterval {
  const start = boundedInteger(interval.start, 0, 0, totalBytes)
  const end = boundedInteger(interval.end, totalBytes, start, totalBytes)
  return Object.freeze({ start, end })
}

function mergeContentIntervals(
  intervals: readonly ArtifactInterval[],
): readonly ArtifactInterval[] {
  const sorted = [...intervals]
    .filter((interval) => interval.end > interval.start)
    .sort((left, right) => left.start - right.start || left.end - right.end)
  const result: ArtifactInterval[] = []
  for (const interval of sorted) {
    const previous = result.at(-1)
    if (!previous || interval.start > previous.end) {
      result.push({ ...interval })
      continue
    }
    previous.end = Math.max(previous.end, interval.end)
  }
  return Object.freeze(result.map((interval) => Object.freeze(interval)))
}

function freezeContentState(state: ArtifactContentState): ArtifactContentState {
  return Object.freeze({
    ...state,
    requested: Object.freeze(
      state.requested.map((interval) => Object.freeze({ ...interval })),
    ),
    admitted: Object.freeze(
      state.admitted.map((interval) => Object.freeze({ ...interval })),
    ),
  })
}

function safeIdentity(value: string, label: string): string {
  const selected = String(value || "").trim()
  if (
    !selected
    || /[\r\n\u0000]/.test(selected)
    || new TextEncoder().encode(selected).byteLength > 512
  ) {
    throw new ArtifactContentError(
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
  if (!Number.isFinite(selected)) return fallback
  return Math.max(minimum, Math.min(maximum, Math.floor(selected)))
}

function clampNumber(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, finiteNumber(value, minimum)))
}

function finiteNumber(value: number, fallback: number): number {
  return Number.isFinite(value) ? value : fallback
}
