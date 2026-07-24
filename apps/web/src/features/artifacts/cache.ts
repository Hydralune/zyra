import {
  artifactRangeKey,
  artifactRevisionKey,
  type ArtifactContract,
  type ArtifactReadRange,
  type ArtifactReadReceipt,
} from "./contracts.ts"
import type { ArtifactSafeText } from "./security.ts"

export interface ArtifactCacheLimits {
  maximumEntries: number
  maximumBytes: number
  maximumArtifactBytes: number
  maximumPinnedBytes: number
}

export interface ArtifactCacheEntry {
  key: string
  artifactKey: string
  artifactId: string
  revision: string
  range: ArtifactReadRange
  bytes: number
  text?: ArtifactSafeText
  binary?: Uint8Array
  receipt: ArtifactReadReceipt
  createdAt: number
  accessedAt: number
  pinCount: number
}

export interface ArtifactCacheSnapshot {
  entryCount: number
  artifactCount: number
  totalBytes: number
  pinnedBytes: number
  hits: number
  misses: number
  evictions: number
  rejected: number
  limits: ArtifactCacheLimits
}

export interface ArtifactCoverage {
  artifactKey: string
  covered: readonly ArtifactInterval[]
  missing: readonly ArtifactInterval[]
  complete: boolean
}

export interface ArtifactInterval {
  start: number
  end: number
}

export interface ArtifactViewPreference {
  taskId: string
  artifactId: string
  revision: string
  scrollTop: number
  scrollLeft: number
  selectedLineStart?: number
  selectedLineEnd?: number
  searchQuery?: string
  searchCursor?: number
  zoom?: number
  updatedAt: number
}

export interface ArtifactBookmark {
  id: string
  taskId: string
  artifactId: string
  revision: string
  title: string
  source: "artifact" | "timeline" | "topology" | "event" | "final-report"
  sourceEventId?: string
  pinned: boolean
  createdAt: number
  updatedAt: number
}

export interface ArtifactPreferenceStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

const DEFAULT_LIMITS: ArtifactCacheLimits = Object.freeze({
  maximumEntries: 96,
  maximumBytes: 32 * 1024 * 1024,
  maximumArtifactBytes: 12 * 1024 * 1024,
  maximumPinnedBytes: 16 * 1024 * 1024,
})

const VIEW_STORAGE_KEY = "zyra.artifact-view.v2"
const BOOKMARK_STORAGE_KEY = "zyra.artifact-bookmarks.v1"
const MAX_VIEW_TASKS = 20
const MAX_VIEW_ARTIFACTS = 500
const MAX_BOOKMARKS = 500

export class ArtifactContentCache {
  readonly #limits: ArtifactCacheLimits
  readonly #entries = new Map<string, ArtifactCacheEntry>()
  readonly #keysByArtifact = new Map<string, Set<string>>()
  #totalBytes = 0
  #pinnedBytes = 0
  #hits = 0
  #misses = 0
  #evictions = 0
  #rejected = 0
  #disabled = false

  constructor(limits: Partial<ArtifactCacheLimits> = {}) {
    this.#limits = Object.freeze({
      maximumEntries: boundedLimit(
        limits.maximumEntries,
        DEFAULT_LIMITS.maximumEntries,
        1,
        10_000,
      ),
      maximumBytes: boundedLimit(
        limits.maximumBytes,
        DEFAULT_LIMITS.maximumBytes,
        1024,
        1024 * 1024 * 1024,
      ),
      maximumArtifactBytes: boundedLimit(
        limits.maximumArtifactBytes,
        DEFAULT_LIMITS.maximumArtifactBytes,
        1024,
        256 * 1024 * 1024,
      ),
      maximumPinnedBytes: boundedLimit(
        limits.maximumPinnedBytes,
        DEFAULT_LIMITS.maximumPinnedBytes,
        0,
        512 * 1024 * 1024,
      ),
    })
    if (this.#limits.maximumArtifactBytes > this.#limits.maximumBytes) {
      throw new TypeError("Artifact cache per-artifact limit exceeds total limit")
    }
    if (this.#limits.maximumPinnedBytes > this.#limits.maximumBytes) {
      throw new TypeError("Artifact cache pinned limit exceeds total limit")
    }
  }

  putText(
    artifact: ArtifactContract,
    range: ArtifactReadRange,
    text: ArtifactSafeText,
    receipt: ArtifactReadReceipt,
  ): ArtifactCacheEntry {
    const bytes = new TextEncoder().encode(text.text).byteLength
    return this.#put({
      artifact,
      range,
      bytes,
      text,
      receipt,
    })
  }

  putBinary(
    artifact: ArtifactContract,
    range: ArtifactReadRange,
    binary: Uint8Array,
    receipt: ArtifactReadReceipt,
  ): ArtifactCacheEntry {
    return this.#put({
      artifact,
      range,
      bytes: binary.byteLength,
      binary: binary.slice(),
      receipt,
    })
  }

  get(
    artifact: Pick<ArtifactContract, "artifactId" | "revision">,
    range: Pick<ArtifactReadRange, "offset" | "endExclusive">,
  ): ArtifactCacheEntry | undefined {
    if (this.#disabled) return undefined
    const key = artifactRangeKey(artifact, range)
    const entry = this.#entries.get(key)
    if (!entry) {
      this.#misses += 1
      return undefined
    }
    this.#hits += 1
    entry.accessedAt = Date.now()
    this.#touch(entry)
    return cloneEntry(entry)
  }

  getContaining(
    artifact: Pick<ArtifactContract, "artifactId" | "revision">,
    offset: number,
    endExclusive: number,
  ): ArtifactCacheEntry | undefined {
    if (this.#disabled) return undefined
    const artifactKey = artifactRevisionKey(artifact)
    const keys = this.#keysByArtifact.get(artifactKey)
    if (!keys) {
      this.#misses += 1
      return undefined
    }
    let selected: ArtifactCacheEntry | undefined
    for (const key of keys) {
      const entry = this.#entries.get(key)
      if (!entry) continue
      if (
        entry.range.offset <= offset
        && entry.range.endExclusive >= endExclusive
      ) {
        if (!selected || entry.range.length < selected.range.length) {
          selected = entry
        }
      }
    }
    if (!selected) {
      this.#misses += 1
      return undefined
    }
    this.#hits += 1
    selected.accessedAt = Date.now()
    this.#touch(selected)
    return cloneEntry(selected)
  }

  ranges(
    artifact: Pick<ArtifactContract, "artifactId" | "revision">,
  ): readonly ArtifactCacheEntry[] {
    const artifactKey = artifactRevisionKey(artifact)
    const keys = this.#keysByArtifact.get(artifactKey)
    if (!keys) return Object.freeze([])
    return Object.freeze(
      [...keys]
        .map((key) => this.#entries.get(key))
        .filter((entry): entry is ArtifactCacheEntry => entry !== undefined)
        .sort((left, right) => left.range.offset - right.range.offset)
        .map(cloneEntry),
    )
  }

  coverage(
    artifact: Pick<ArtifactContract, "artifactId" | "revision" | "sizeBytes">,
    requested: ArtifactInterval,
  ): ArtifactCoverage {
    const start = clampInteger(requested.start, 0, artifact.sizeBytes)
    const end = clampInteger(requested.end, start, artifact.sizeBytes)
    const intervals = this.ranges(artifact).map((entry) => ({
      start: entry.range.offset,
      end: entry.range.endExclusive,
    }))
    const covered = mergeIntervals(intervals, { start, end })
    const missing = subtractIntervals({ start, end }, covered)
    return Object.freeze({
      artifactKey: artifactRevisionKey(artifact),
      covered: Object.freeze(covered),
      missing: Object.freeze(missing),
      complete: missing.length === 0,
    })
  }

  pin(
    artifact: Pick<ArtifactContract, "artifactId" | "revision">,
    range?: Pick<ArtifactReadRange, "offset" | "endExclusive">,
  ): boolean {
    if (this.#disabled) return false
    const entries = range
      ? [this.#entries.get(artifactRangeKey(artifact, range))]
      : this.ranges(artifact).map((entry) => this.#entries.get(entry.key))
    const selected = entries.filter(
      (entry): entry is ArtifactCacheEntry => entry !== undefined,
    )
    const additional = selected
      .filter((entry) => entry.pinCount === 0)
      .reduce((sum, entry) => sum + entry.bytes, 0)
    if (this.#pinnedBytes + additional > this.#limits.maximumPinnedBytes) {
      this.#rejected += 1
      return false
    }
    for (const entry of selected) {
      if (entry.pinCount === 0) this.#pinnedBytes += entry.bytes
      entry.pinCount += 1
      entry.accessedAt = Date.now()
      this.#touch(entry)
    }
    return selected.length > 0
  }

  unpin(
    artifact: Pick<ArtifactContract, "artifactId" | "revision">,
    range?: Pick<ArtifactReadRange, "offset" | "endExclusive">,
  ): void {
    const entries = range
      ? [this.#entries.get(artifactRangeKey(artifact, range))]
      : this.ranges(artifact).map((entry) => this.#entries.get(entry.key))
    for (const entry of entries) {
      if (!entry || entry.pinCount < 1) continue
      entry.pinCount -= 1
      if (entry.pinCount === 0) this.#pinnedBytes -= entry.bytes
    }
    this.#evict()
  }

  removeArtifact(
    artifact: Pick<ArtifactContract, "artifactId" | "revision">,
  ): number {
    const artifactKey = artifactRevisionKey(artifact)
    const keys = [...(this.#keysByArtifact.get(artifactKey) ?? [])]
    for (const key of keys) this.#remove(key)
    return keys.length
  }

  pruneRevisions(
    artifactId: string,
    keepRevision: string,
  ): number {
    let removed = 0
    for (const [artifactKey, keys] of this.#keysByArtifact) {
      if (!artifactKey.startsWith(`${artifactId}@`)) continue
      if (artifactKey === `${artifactId}@${keepRevision}`) continue
      for (const key of [...keys]) {
        this.#remove(key)
        removed += 1
      }
    }
    return removed
  }

  clear(options: { includePinned?: boolean } = {}): void {
    for (const [key, entry] of [...this.#entries]) {
      if (!options.includePinned && entry.pinCount > 0) continue
      this.#remove(key)
    }
  }

  disable(): void {
    this.#disabled = true
    this.clear({ includePinned: true })
  }

  snapshot(): ArtifactCacheSnapshot {
    return Object.freeze({
      entryCount: this.#entries.size,
      artifactCount: this.#keysByArtifact.size,
      totalBytes: this.#totalBytes,
      pinnedBytes: this.#pinnedBytes,
      hits: this.#hits,
      misses: this.#misses,
      evictions: this.#evictions,
      rejected: this.#rejected,
      limits: this.#limits,
    })
  }

  #put(input: {
    artifact: ArtifactContract
    range: ArtifactReadRange
    bytes: number
    text?: ArtifactSafeText
    binary?: Uint8Array
    receipt: ArtifactReadReceipt
  }): ArtifactCacheEntry {
    if (this.#disabled) {
      throw new ArtifactCacheError(
        "cache_disabled",
        "Artifact content cache is disabled.",
      )
    }
    if (input.bytes < 0 || !Number.isSafeInteger(input.bytes)) {
      throw new ArtifactCacheError(
        "cache_size_invalid",
        "Artifact cache entry size is invalid.",
      )
    }
    if (input.bytes > this.#limits.maximumArtifactBytes) {
      this.#rejected += 1
      throw new ArtifactCacheError(
        "artifact_cache_limit",
        "Artifact range exceeds the per-artifact cache limit.",
      )
    }
    if (
      input.range.offset < 0
      || input.range.endExclusive > input.artifact.sizeBytes
      || input.range.endExclusive !== input.range.offset + input.range.length
    ) {
      throw new ArtifactCacheError(
        "cache_range_invalid",
        "Artifact cache range is inconsistent with immutable content.",
      )
    }
    if (
      input.receipt.artifactId !== input.artifact.artifactId
      || input.receipt.revision !== input.artifact.revision
      || input.receipt.sha256 !== input.artifact.sha256
    ) {
      throw new ArtifactCacheError(
        "cache_receipt_mismatch",
        "Artifact cache entry does not match its integrity receipt.",
      )
    }
    const key = artifactRangeKey(input.artifact, input.range)
    const artifactKey = artifactRevisionKey(input.artifact)
    const existing = this.#entries.get(key)
    if (existing) {
      if (existing.receipt.receiptDigest !== input.receipt.receiptDigest) {
        throw new ArtifactCacheError(
          "cache_range_conflict",
          "Immutable artifact range was returned with a conflicting receipt.",
        )
      }
      existing.accessedAt = Date.now()
      this.#touch(existing)
      return cloneEntry(existing)
    }
    const artifactBytes = this.#artifactBytes(artifactKey)
    if (artifactBytes + input.bytes > this.#limits.maximumArtifactBytes) {
      this.#evictArtifactRanges(artifactKey, input.bytes)
    }
    const now = Date.now()
    const entry: ArtifactCacheEntry = {
      key,
      artifactKey,
      artifactId: input.artifact.artifactId,
      revision: input.artifact.revision,
      range: Object.freeze({ ...input.range }),
      bytes: input.bytes,
      text: input.text,
      binary: input.binary?.slice(),
      receipt: input.receipt,
      createdAt: now,
      accessedAt: now,
      pinCount: 0,
    }
    this.#entries.set(key, entry)
    let keys = this.#keysByArtifact.get(artifactKey)
    if (!keys) {
      keys = new Set()
      this.#keysByArtifact.set(artifactKey, keys)
    }
    keys.add(key)
    this.#totalBytes += input.bytes
    this.#evict()
    const admitted = this.#entries.get(key)
    if (!admitted) {
      this.#rejected += 1
      throw new ArtifactCacheError(
        "cache_capacity_exhausted",
        "Artifact range could not fit within the bounded cache.",
      )
    }
    return cloneEntry(admitted)
  }

  #touch(entry: ArtifactCacheEntry): void {
    this.#entries.delete(entry.key)
    this.#entries.set(entry.key, entry)
  }

  #artifactBytes(artifactKey: string): number {
    let total = 0
    for (const key of this.#keysByArtifact.get(artifactKey) ?? []) {
      total += this.#entries.get(key)?.bytes ?? 0
    }
    return total
  }

  #evictArtifactRanges(artifactKey: string, required: number): void {
    const keys = [...(this.#keysByArtifact.get(artifactKey) ?? [])]
    for (const key of keys) {
      if (this.#artifactBytes(artifactKey) + required <= this.#limits.maximumArtifactBytes) {
        break
      }
      const entry = this.#entries.get(key)
      if (!entry || entry.pinCount > 0) continue
      this.#remove(key)
      this.#evictions += 1
    }
  }

  #evict(): void {
    let inspected = 0
    while (
      this.#entries.size > this.#limits.maximumEntries
      || this.#totalBytes > this.#limits.maximumBytes
    ) {
      const oldest = this.#entries.entries().next().value as
        | [string, ArtifactCacheEntry]
        | undefined
      if (!oldest) break
      const [key, entry] = oldest
      if (entry.pinCount > 0) {
        this.#touch(entry)
        inspected += 1
        if (inspected >= this.#entries.size) break
        continue
      }
      this.#remove(key)
      this.#evictions += 1
      inspected = 0
    }
  }

  #remove(key: string): void {
    const entry = this.#entries.get(key)
    if (!entry) return
    this.#entries.delete(key)
    this.#totalBytes -= entry.bytes
    if (entry.pinCount > 0) this.#pinnedBytes -= entry.bytes
    const keys = this.#keysByArtifact.get(entry.artifactKey)
    keys?.delete(key)
    if (keys?.size === 0) this.#keysByArtifact.delete(entry.artifactKey)
  }
}

export class ArtifactViewPreferenceStore {
  readonly #storage?: ArtifactPreferenceStorage
  readonly #views = new Map<string, ArtifactViewPreference>()
  #restored = false

  constructor(storage?: ArtifactPreferenceStorage) {
    this.#storage = storage
  }

  restore(): void {
    if (this.#restored) return
    this.#restored = true
    const raw = this.#storage?.getItem(VIEW_STORAGE_KEY)
    if (!raw) return
    try {
      const parsed = JSON.parse(raw)
      if (!Array.isArray(parsed)) return
      for (const candidate of parsed.slice(-MAX_VIEW_ARTIFACTS)) {
        const view = parseViewPreference(candidate)
        if (view) this.#views.set(viewPreferenceKey(view), view)
      }
      this.#prune()
    } catch {
      this.#storage?.removeItem(VIEW_STORAGE_KEY)
    }
  }

  get(
    taskId: string,
    artifactId: string,
    revision: string,
  ): ArtifactViewPreference | undefined {
    this.restore()
    const key = viewPreferenceKey({ taskId, artifactId, revision })
    const value = this.#views.get(key)
    if (!value) return undefined
    this.#views.delete(key)
    this.#views.set(key, value)
    return Object.freeze({ ...value })
  }

  update(
    input: Omit<ArtifactViewPreference, "updatedAt">,
  ): ArtifactViewPreference {
    this.restore()
    const normalized = normalizeViewPreference({
      ...input,
      updatedAt: Date.now(),
    })
    const key = viewPreferenceKey(normalized)
    this.#views.delete(key)
    this.#views.set(key, normalized)
    this.#prune(key)
    this.#persist()
    return Object.freeze({ ...normalized })
  }

  remove(taskId: string, artifactId: string, revision?: string): number {
    this.restore()
    let removed = 0
    for (const [key, value] of [...this.#views]) {
      if (value.taskId !== taskId || value.artifactId !== artifactId) continue
      if (revision && value.revision !== revision) continue
      this.#views.delete(key)
      removed += 1
    }
    if (removed) this.#persist()
    return removed
  }

  clear(): void {
    this.#views.clear()
    this.#storage?.removeItem(VIEW_STORAGE_KEY)
  }

  #prune(keep?: string): void {
    const tasks = new Map<string, number>()
    for (const value of this.#views.values()) {
      tasks.set(value.taskId, Math.max(tasks.get(value.taskId) ?? 0, value.updatedAt))
    }
    const allowedTasks = new Set(
      [...tasks]
        .sort((left, right) => right[1] - left[1])
        .slice(0, MAX_VIEW_TASKS)
        .map(([taskId]) => taskId),
    )
    for (const [key, value] of [...this.#views]) {
      if (!allowedTasks.has(value.taskId) && key !== keep) this.#views.delete(key)
    }
    while (this.#views.size > MAX_VIEW_ARTIFACTS) {
      const oldest = this.#views.keys().next().value as string | undefined
      if (!oldest) break
      if (oldest === keep) {
        const value = this.#views.get(oldest)!
        this.#views.delete(oldest)
        this.#views.set(oldest, value)
        continue
      }
      this.#views.delete(oldest)
    }
  }

  #persist(): void {
    if (!this.#storage) return
    try {
      this.#storage.setItem(
        VIEW_STORAGE_KEY,
        JSON.stringify([...this.#views.values()]),
      )
    } catch {
      // Preference persistence failure never becomes artifact truth failure.
    }
  }
}

export class ArtifactBookmarkStore {
  readonly #storage?: ArtifactPreferenceStorage
  readonly #bookmarks = new Map<string, ArtifactBookmark>()
  #restored = false

  constructor(storage?: ArtifactPreferenceStorage) {
    this.#storage = storage
  }

  restore(): void {
    if (this.#restored) return
    this.#restored = true
    const raw = this.#storage?.getItem(BOOKMARK_STORAGE_KEY)
    if (!raw) return
    try {
      const parsed = JSON.parse(raw)
      if (!Array.isArray(parsed)) return
      for (const candidate of parsed.slice(-MAX_BOOKMARKS)) {
        const bookmark = parseBookmark(candidate)
        if (bookmark) this.#bookmarks.set(bookmark.id, bookmark)
      }
      this.#prune()
    } catch {
      this.#storage?.removeItem(BOOKMARK_STORAGE_KEY)
    }
  }

  add(
    input: Omit<ArtifactBookmark, "id" | "createdAt" | "updatedAt">,
  ): ArtifactBookmark {
    this.restore()
    const now = Date.now()
    const id = bookmarkIdentity(input)
    const existing = this.#bookmarks.get(id)
    const bookmark = normalizeBookmark({
      ...input,
      id,
      createdAt: existing?.createdAt ?? now,
      updatedAt: now,
    })
    this.#bookmarks.delete(id)
    this.#bookmarks.set(id, bookmark)
    this.#prune(id)
    this.#persist()
    return Object.freeze({ ...bookmark })
  }

  togglePin(id: string): ArtifactBookmark | undefined {
    this.restore()
    const current = this.#bookmarks.get(id)
    if (!current) return undefined
    const updated = normalizeBookmark({
      ...current,
      pinned: !current.pinned,
      updatedAt: Date.now(),
    })
    this.#bookmarks.set(id, updated)
    this.#persist()
    return Object.freeze({ ...updated })
  }

  remove(id: string): boolean {
    this.restore()
    const removed = this.#bookmarks.delete(id)
    if (removed) this.#persist()
    return removed
  }

  list(taskId?: string): readonly ArtifactBookmark[] {
    this.restore()
    return Object.freeze(
      [...this.#bookmarks.values()]
        .filter((bookmark) => !taskId || bookmark.taskId === taskId)
        .sort((left, right) => {
          if (left.pinned !== right.pinned) return left.pinned ? -1 : 1
          return right.updatedAt - left.updatedAt
        })
        .map((bookmark) => Object.freeze({ ...bookmark })),
    )
  }

  #prune(keep?: string): void {
    while (this.#bookmarks.size > MAX_BOOKMARKS) {
      const candidate = [...this.#bookmarks.values()]
        .filter((bookmark) => !bookmark.pinned && bookmark.id !== keep)
        .sort((left, right) => left.updatedAt - right.updatedAt)[0]
      if (!candidate) break
      this.#bookmarks.delete(candidate.id)
    }
  }

  #persist(): void {
    if (!this.#storage) return
    try {
      this.#storage.setItem(
        BOOKMARK_STORAGE_KEY,
        JSON.stringify([...this.#bookmarks.values()]),
      )
    } catch {
      // View-reference persistence is optional and never canonical metadata.
    }
  }
}

export class ArtifactCacheError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = "ArtifactCacheError"
    this.code = code
  }
}

export function browserArtifactPreferenceStorage():
  | ArtifactPreferenceStorage
  | undefined {
  if (typeof window === "undefined") return undefined
  try {
    const storage = window.localStorage
    const probe = "__zyra_artifact_storage_probe__"
    storage.setItem(probe, "1")
    storage.removeItem(probe)
    return storage
  } catch {
    return undefined
  }
}

function mergeIntervals(
  intervals: readonly ArtifactInterval[],
  bounds: ArtifactInterval,
): ArtifactInterval[] {
  const selected = intervals
    .map((interval) => ({
      start: Math.max(bounds.start, interval.start),
      end: Math.min(bounds.end, interval.end),
    }))
    .filter((interval) => interval.end > interval.start)
    .sort((left, right) => left.start - right.start || left.end - right.end)
  const result: ArtifactInterval[] = []
  for (const interval of selected) {
    const previous = result.at(-1)
    if (!previous || interval.start > previous.end) {
      result.push({ ...interval })
      continue
    }
    previous.end = Math.max(previous.end, interval.end)
  }
  return result.map((interval) => Object.freeze(interval))
}

function subtractIntervals(
  requested: ArtifactInterval,
  covered: readonly ArtifactInterval[],
): ArtifactInterval[] {
  const missing: ArtifactInterval[] = []
  let cursor = requested.start
  for (const interval of covered) {
    if (interval.start > cursor) {
      missing.push(Object.freeze({ start: cursor, end: interval.start }))
    }
    cursor = Math.max(cursor, interval.end)
  }
  if (cursor < requested.end) {
    missing.push(Object.freeze({ start: cursor, end: requested.end }))
  }
  return missing
}

function cloneEntry(entry: ArtifactCacheEntry): ArtifactCacheEntry {
  return Object.freeze({
    ...entry,
    range: Object.freeze({ ...entry.range }),
    binary: entry.binary?.slice(),
  })
}

function boundedLimit(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const selected = value ?? fallback
  if (!Number.isSafeInteger(selected) || selected < minimum || selected > maximum) {
    throw new TypeError(`Artifact cache limit must be between ${minimum} and ${maximum}`)
  }
  return selected
}

function clampInteger(value: number, minimum: number, maximum: number): number {
  if (!Number.isFinite(value)) return minimum
  return Math.max(minimum, Math.min(maximum, Math.floor(value)))
}

function viewPreferenceKey(
  value: Pick<ArtifactViewPreference, "taskId" | "artifactId" | "revision">,
): string {
  return `${value.taskId}:${value.artifactId}@${value.revision}`
}

function normalizeViewPreference(
  input: ArtifactViewPreference,
): ArtifactViewPreference {
  const selectedLineStart =
    input.selectedLineStart === undefined
      ? undefined
      : clampInteger(input.selectedLineStart, 1, Number.MAX_SAFE_INTEGER)
  const selectedLineEnd =
    input.selectedLineEnd === undefined
      ? undefined
      : clampInteger(
          input.selectedLineEnd,
          selectedLineStart ?? 1,
          Number.MAX_SAFE_INTEGER,
        )
  return {
    taskId: safeIdentity(input.taskId),
    artifactId: safeIdentity(input.artifactId),
    revision: safeRevision(input.revision),
    scrollTop: Math.max(0, finiteNumber(input.scrollTop, 0)),
    scrollLeft: Math.max(0, finiteNumber(input.scrollLeft, 0)),
    selectedLineStart,
    selectedLineEnd,
    searchQuery: boundedOptionalString(input.searchQuery, 4096),
    searchCursor:
      input.searchCursor === undefined
        ? undefined
        : clampInteger(input.searchCursor, 0, Number.MAX_SAFE_INTEGER),
    zoom:
      input.zoom === undefined
        ? undefined
        : Math.max(0.1, Math.min(16, finiteNumber(input.zoom, 1))),
    updatedAt: Math.max(0, finiteNumber(input.updatedAt, Date.now())),
  }
}

function parseViewPreference(value: unknown): ArtifactViewPreference | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined
  try {
    return normalizeViewPreference(value as ArtifactViewPreference)
  } catch {
    return undefined
  }
}

function bookmarkIdentity(
  input: Pick<
    ArtifactBookmark,
    "taskId" | "artifactId" | "revision" | "source" | "sourceEventId"
  >,
): string {
  return [
    safeIdentity(input.taskId),
    safeIdentity(input.artifactId),
    safeRevision(input.revision),
    input.source,
    input.sourceEventId ?? "",
  ].join(":")
}

function normalizeBookmark(input: ArtifactBookmark): ArtifactBookmark {
  const sources = new Set<ArtifactBookmark["source"]>([
    "artifact",
    "timeline",
    "topology",
    "event",
    "final-report",
  ])
  if (!sources.has(input.source)) throw new TypeError("Artifact bookmark source is invalid")
  return {
    id: boundedRequiredString(input.id, 4096),
    taskId: safeIdentity(input.taskId),
    artifactId: safeIdentity(input.artifactId),
    revision: safeRevision(input.revision),
    title: boundedRequiredString(input.title, 4096),
    source: input.source,
    sourceEventId: input.sourceEventId
      ? safeIdentity(input.sourceEventId)
      : undefined,
    pinned: input.pinned === true,
    createdAt: Math.max(0, finiteNumber(input.createdAt, Date.now())),
    updatedAt: Math.max(0, finiteNumber(input.updatedAt, Date.now())),
  }
}

function parseBookmark(value: unknown): ArtifactBookmark | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined
  try {
    return normalizeBookmark(value as ArtifactBookmark)
  } catch {
    return undefined
  }
}

function safeIdentity(value: string): string {
  const selected = boundedRequiredString(value, 512)
  if (/[\r\n\u0000]/.test(selected)) throw new TypeError("Artifact preference identity is invalid")
  return selected
}

function safeRevision(value: string): string {
  const selected = boundedRequiredString(value, 128).toLowerCase()
  if (!/^sha256:[0-9a-f]{64}$/.test(selected)) {
    throw new TypeError("Artifact preference revision is invalid")
  }
  return selected
}

function boundedRequiredString(value: string, maximumBytes: number): string {
  const selected = String(value || "").trim()
  if (!selected || new TextEncoder().encode(selected).byteLength > maximumBytes) {
    throw new TypeError("Artifact preference string is empty or too large")
  }
  return selected
}

function boundedOptionalString(
  value: string | undefined,
  maximumBytes: number,
): string | undefined {
  const selected = String(value || "")
  if (!selected) return undefined
  if (new TextEncoder().encode(selected).byteLength > maximumBytes) {
    return new TextDecoder().decode(
      new TextEncoder().encode(selected).slice(0, maximumBytes),
    )
  }
  return selected
}

function finiteNumber(value: number, fallback: number): number {
  return Number.isFinite(value) ? value : fallback
}
