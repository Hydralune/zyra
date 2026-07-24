import {
  artifactDisplayName,
  artifactRevisionKey,
  type ArtifactCatalogFilters,
  type ArtifactCatalogPage,
  type ArtifactContentFamily,
  type ArtifactContract,
} from "./contracts.ts"

export interface ArtifactCatalogState {
  taskId: string
  phase: "idle" | "loading" | "ready" | "partial" | "failed" | "disconnected"
  artifacts: readonly ArtifactContract[]
  filtered: readonly ArtifactContract[]
  selectedKey?: string
  focusedKey?: string
  cursor?: string
  total: number
  query: ArtifactCatalogViewQuery
  facets: ArtifactCatalogFacets
  error?: ArtifactCatalogFailure
  generation: number
  updatedAt: number
}

export interface ArtifactCatalogViewQuery {
  text: string
  nodeIds: readonly string[]
  workerIds: readonly string[]
  mediaTypes: readonly string[]
  contentFamilies: readonly ArtifactContentFamily[]
  revisions: readonly string[]
  integrity: readonly string[]
  securityLabels: readonly string[]
  createdAfter?: string
  createdBefore?: string
  includeDeleted: boolean
  sort: ArtifactCatalogSort
}

export type ArtifactCatalogSort =
  | "created-desc"
  | "created-asc"
  | "title-asc"
  | "size-desc"
  | "size-asc"
  | "producer-asc"

export interface ArtifactCatalogFacets {
  nodes: ReadonlyMap<string, number>
  workers: ReadonlyMap<string, number>
  mediaTypes: ReadonlyMap<string, number>
  contentFamilies: ReadonlyMap<string, number>
  integrity: ReadonlyMap<string, number>
  securityLabels: ReadonlyMap<string, number>
  revisions: ReadonlyMap<string, number>
  totalBytes: number
  verifiedBytes: number
  legacyCount: number
  missingCount: number
}

export interface ArtifactCatalogFailure {
  code: string
  message: string
  retryable: boolean
  occurredAt: number
}

export interface ArtifactCatalogWindow {
  startIndex: number
  endIndex: number
  overscanStartIndex: number
  overscanEndIndex: number
  beforeHeight: number
  afterHeight: number
  totalHeight: number
  rows: readonly ArtifactCatalogVirtualRow[]
}

export interface ArtifactCatalogVirtualRow {
  key: string
  artifact: ArtifactContract
  index: number
  top: number
  height: number
  selected: boolean
  focused: boolean
  searchableText: string
}

export interface ArtifactCatalogVirtualOptions {
  scrollTop: number
  viewportHeight: number
  estimatedRowHeight?: number
  overscan?: number
  measuredHeights?: ReadonlyMap<string, number>
}

export interface ArtifactCatalogSelection {
  artifactId: string
  revision?: string
  source: "artifact" | "timeline" | "topology" | "event" | "final-report"
  sourceEventId?: string
  focus: boolean
}

export type ArtifactCatalogListener = (state: ArtifactCatalogState) => void

const EMPTY_QUERY: ArtifactCatalogViewQuery = Object.freeze({
  text: "",
  nodeIds: Object.freeze([]),
  workerIds: Object.freeze([]),
  mediaTypes: Object.freeze([]),
  contentFamilies: Object.freeze([]),
  revisions: Object.freeze([]),
  integrity: Object.freeze([]),
  securityLabels: Object.freeze([]),
  includeDeleted: false,
  sort: "created-desc",
})

export class ArtifactCatalogModel {
  readonly #listeners = new Set<ArtifactCatalogListener>()
  readonly #artifacts = new Map<string, ArtifactContract>()
  readonly #artifactOrder = new Map<string, number>()
  #state: ArtifactCatalogState
  #sequence = 0
  #closed = false

  constructor(taskId: string) {
    const normalizedTaskId = safeIdentity(taskId)
    this.#state = freezeState({
      taskId: normalizedTaskId,
      phase: "idle",
      artifacts: [],
      filtered: [],
      total: 0,
      query: EMPTY_QUERY,
      facets: emptyFacets(),
      generation: 0,
      updatedAt: Date.now(),
    })
  }

  get state(): ArtifactCatalogState {
    return this.#state
  }

  listen(listener: ArtifactCatalogListener): () => void {
    this.#assertOpen()
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  loading(options: { reset?: boolean } = {}): void {
    this.#assertOpen()
    if (options.reset) {
      this.#artifacts.clear()
      this.#artifactOrder.clear()
      this.#sequence = 0
    }
    this.#commit({
      phase: "loading",
      error: undefined,
      generation: this.#state.generation + 1,
    })
  }

  mergePage(
    page: ArtifactCatalogPage,
    options: { append: boolean },
  ): void {
    this.#assertOpen()
    if (page.taskId !== this.#state.taskId) {
      throw new ArtifactCatalogError(
        "catalog_task_mismatch",
        "Artifact catalog page belongs to a different task.",
      )
    }
    if (!options.append) {
      this.#artifacts.clear()
      this.#artifactOrder.clear()
      this.#sequence = 0
    }
    for (const artifact of page.artifacts) {
      const key = artifactRevisionKey(artifact)
      const existing = this.#artifacts.get(key)
      if (existing && !sameArtifact(existing, artifact)) {
        throw new ArtifactCatalogError(
          "catalog_revision_conflict",
          `Immutable artifact revision ${key} changed across pages.`,
        )
      }
      if (!existing) this.#artifactOrder.set(key, this.#sequence++)
      this.#artifacts.set(key, artifact)
    }
    const artifacts = this.#orderedArtifacts()
    const filtered = filterArtifacts(artifacts, this.#state.query)
    const selectedKey = this.#resolveSelection(
      this.#state.selectedKey,
      artifacts,
    )
    const phase =
      page.cursor || this.#artifacts.size < page.total ? "partial" : "ready"
    this.#state = freezeState({
      ...this.#state,
      phase,
      artifacts,
      filtered,
      selectedKey,
      focusedKey: this.#resolveSelection(this.#state.focusedKey, artifacts),
      cursor: page.cursor,
      total: page.total,
      facets: buildArtifactFacets(artifacts),
      error: undefined,
      generation: this.#state.generation + 1,
      updatedAt: Date.now(),
    })
    this.#notify()
  }

  updateQuery(
    patch: Partial<ArtifactCatalogViewQuery>,
  ): ArtifactCatalogState {
    this.#assertOpen()
    const query = normalizeCatalogQuery({ ...this.#state.query, ...patch })
    const filtered = filterArtifacts(this.#state.artifacts, query)
    this.#state = freezeState({
      ...this.#state,
      query,
      filtered,
      selectedKey: this.#resolveSelection(this.#state.selectedKey, filtered),
      focusedKey: this.#resolveSelection(this.#state.focusedKey, filtered),
      generation: this.#state.generation + 1,
      updatedAt: Date.now(),
    })
    this.#notify()
    return this.#state
  }

  select(selection: ArtifactCatalogSelection): ArtifactContract | undefined {
    this.#assertOpen()
    const matches = this.#state.artifacts.filter(
      (artifact) =>
        artifact.artifactId === selection.artifactId
        && (!selection.revision || artifact.revision === selection.revision),
    )
    const selected = matches.sort(compareCreatedDesc)[0]
    if (!selected) return undefined
    const key = artifactRevisionKey(selected)
    this.#commit({
      selectedKey: key,
      focusedKey: selection.focus ? key : this.#state.focusedKey,
    })
    return selected
  }

  selectKey(key: string, options: { focus?: boolean } = {}): ArtifactContract | undefined {
    this.#assertOpen()
    const artifact = this.#artifacts.get(key)
    if (!artifact) return undefined
    this.#commit({
      selectedKey: key,
      focusedKey: options.focus ? key : this.#state.focusedKey,
    })
    return artifact
  }

  moveFocus(
    direction: "next" | "previous" | "first" | "last",
  ): ArtifactContract | undefined {
    this.#assertOpen()
    const rows = this.#state.filtered
    if (!rows.length) return undefined
    const current = this.#state.focusedKey
      ? rows.findIndex(
          (artifact) => artifactRevisionKey(artifact) === this.#state.focusedKey,
        )
      : -1
    let index = current
    if (direction === "first") index = 0
    if (direction === "last") index = rows.length - 1
    if (direction === "next") index = Math.min(rows.length - 1, current + 1)
    if (direction === "previous") {
      index = current < 0 ? rows.length - 1 : Math.max(0, current - 1)
    }
    const selected = rows[index]
    if (!selected) return undefined
    const key = artifactRevisionKey(selected)
    this.#commit({ focusedKey: key })
    return selected
  }

  activateFocused(): ArtifactContract | undefined {
    const key = this.#state.focusedKey
    if (!key) return undefined
    return this.selectKey(key)
  }

  failure(
    code: string,
    message: string,
    options: { disconnected?: boolean; retryable?: boolean } = {},
  ): void {
    this.#assertOpen()
    this.#commit({
      phase: options.disconnected ? "disconnected" : "failed",
      error: Object.freeze({
        code: boundedString(code, 256),
        message: boundedString(message, 4096),
        retryable: options.retryable ?? true,
        occurredAt: Date.now(),
      }),
    })
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    this.#listeners.clear()
    this.#artifacts.clear()
    this.#artifactOrder.clear()
  }

  #orderedArtifacts(): ArtifactContract[] {
    return [...this.#artifacts.entries()]
      .sort((left, right) => {
        const leftOrder = this.#artifactOrder.get(left[0]) ?? 0
        const rightOrder = this.#artifactOrder.get(right[0]) ?? 0
        return leftOrder - rightOrder
      })
      .map(([, artifact]) => artifact)
  }

  #resolveSelection(
    key: string | undefined,
    artifacts: readonly ArtifactContract[],
  ): string | undefined {
    if (key && artifacts.some((artifact) => artifactRevisionKey(artifact) === key)) {
      return key
    }
    return artifacts[0] ? artifactRevisionKey(artifacts[0]) : undefined
  }

  #commit(patch: Partial<ArtifactCatalogState>): void {
    const next = {
      ...this.#state,
      ...patch,
      generation: patch.generation ?? this.#state.generation + 1,
      updatedAt: patch.updatedAt ?? Date.now(),
    }
    this.#state = freezeState(next)
    this.#notify()
  }

  #notify(): void {
    for (const listener of this.#listeners) listener(this.#state)
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new ArtifactCatalogError(
        "catalog_closed",
        "Artifact catalog model is closed.",
      )
    }
  }
}

export class ArtifactCatalogNavigation {
  readonly #listeners = new Set<(selection: ArtifactCatalogSelection) => void>()
  #installed = false
  #disposeBrowser?: () => void

  listen(listener: (selection: ArtifactCatalogSelection) => void): () => void {
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  open(selection: ArtifactCatalogSelection): void {
    const normalized = normalizeSelection(selection)
    for (const listener of this.#listeners) listener(normalized)
  }

  installBrowserBridge(target: Window = window): () => void {
    if (this.#installed) return this.#disposeBrowser ?? (() => undefined)
    this.#installed = true
    const handle = (event: Event) => {
      const custom = event as CustomEvent<unknown>
      const detail = custom.detail
      if (!detail || typeof detail !== "object" || Array.isArray(detail)) return
      try {
        this.open(detail as ArtifactCatalogSelection)
      } catch {
        // Malformed cross-panel navigation is ignored; it never changes truth.
      }
    }
    target.addEventListener("zyra:open-artifact", handle)
    this.#disposeBrowser = () => {
      target.removeEventListener("zyra:open-artifact", handle)
      this.#installed = false
      this.#disposeBrowser = undefined
    }
    return this.#disposeBrowser
  }

  close(): void {
    this.#disposeBrowser?.()
    this.#listeners.clear()
  }
}

export class ArtifactCatalogError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = "ArtifactCatalogError"
    this.code = code
  }
}

export function normalizeCatalogQuery(
  input: ArtifactCatalogViewQuery,
): ArtifactCatalogViewQuery {
  const sorts = new Set<ArtifactCatalogSort>([
    "created-desc",
    "created-asc",
    "title-asc",
    "size-desc",
    "size-asc",
    "producer-asc",
  ])
  return Object.freeze({
    text: boundedString(input.text || "", 4096).trim().toLocaleLowerCase(),
    nodeIds: uniqueStrings(input.nodeIds, 128, 512),
    workerIds: uniqueStrings(input.workerIds, 128, 512),
    mediaTypes: uniqueStrings(input.mediaTypes, 128, 256).map((value) =>
      value.toLowerCase(),
    ),
    contentFamilies: uniqueStrings(
      input.contentFamilies,
      128,
      64,
    ) as ArtifactContentFamily[],
    revisions: uniqueStrings(input.revisions, 128, 128),
    integrity: uniqueStrings(input.integrity, 64, 128),
    securityLabels: uniqueStrings(input.securityLabels, 64, 64),
    createdAfter: normalizeOptionalTimestamp(input.createdAfter),
    createdBefore: normalizeOptionalTimestamp(input.createdBefore),
    includeDeleted: input.includeDeleted === true,
    sort: sorts.has(input.sort) ? input.sort : "created-desc",
  })
}

export function filterArtifacts(
  artifacts: readonly ArtifactContract[],
  query: ArtifactCatalogViewQuery,
): ArtifactContract[] {
  const normalized = normalizeCatalogQuery(query)
  const after = normalized.createdAfter
    ? Date.parse(normalized.createdAfter)
    : Number.NEGATIVE_INFINITY
  const before = normalized.createdBefore
    ? Date.parse(normalized.createdBefore)
    : Number.POSITIVE_INFINITY
  const terms = tokenizeSearch(normalized.text)
  const filtered = artifacts.filter((artifact) => {
    const created = Date.parse(artifact.createdAt)
    if (created < after || created > before) return false
    if (
      normalized.nodeIds.length
      && (!artifact.producer.nodeId
        || !normalized.nodeIds.includes(artifact.producer.nodeId))
    ) {
      return false
    }
    if (
      normalized.workerIds.length
      && (!artifact.producer.workerId
        || !normalized.workerIds.includes(artifact.producer.workerId))
    ) {
      return false
    }
    if (
      normalized.mediaTypes.length
      && !normalized.mediaTypes.includes(artifact.mediaType)
    ) {
      return false
    }
    if (
      normalized.contentFamilies.length
      && !normalized.contentFamilies.includes(artifact.contentFamily)
    ) {
      return false
    }
    if (
      normalized.revisions.length
      && !normalized.revisions.includes(artifact.revision)
    ) {
      return false
    }
    if (
      normalized.integrity.length
      && !normalized.integrity.includes(artifact.status.integrity)
    ) {
      return false
    }
    if (
      normalized.securityLabels.length
      && !normalized.securityLabels.includes(artifact.security.label)
    ) {
      return false
    }
    if (terms.length) {
      const searchable = artifactSearchText(artifact)
      if (!terms.every((term) => searchable.includes(term))) return false
    }
    return true
  })
  return filtered.sort(comparatorFor(normalized.sort))
}

export function buildArtifactFacets(
  artifacts: readonly ArtifactContract[],
): ArtifactCatalogFacets {
  const nodes = new Map<string, number>()
  const workers = new Map<string, number>()
  const mediaTypes = new Map<string, number>()
  const contentFamilies = new Map<string, number>()
  const integrity = new Map<string, number>()
  const securityLabels = new Map<string, number>()
  const revisions = new Map<string, number>()
  let totalBytes = 0
  let verifiedBytes = 0
  let legacyCount = 0
  let missingCount = 0
  for (const artifact of artifacts) {
    increment(nodes, artifact.producer.nodeId ?? "unassigned")
    increment(workers, artifact.producer.workerId ?? "unassigned")
    increment(mediaTypes, artifact.mediaType)
    increment(contentFamilies, artifact.contentFamily)
    increment(integrity, artifact.status.integrity)
    increment(securityLabels, artifact.security.label)
    increment(revisions, artifact.revision)
    totalBytes += artifact.sizeBytes
    if (artifact.status.integrity === "verified") verifiedBytes += artifact.sizeBytes
    if (artifact.status.legacyMetadata) legacyCount += 1
    if (!artifact.status.exists) missingCount += 1
  }
  return Object.freeze({
    nodes: freezeMap(nodes),
    workers: freezeMap(workers),
    mediaTypes: freezeMap(mediaTypes),
    contentFamilies: freezeMap(contentFamilies),
    integrity: freezeMap(integrity),
    securityLabels: freezeMap(securityLabels),
    revisions: freezeMap(revisions),
    totalBytes,
    verifiedBytes,
    legacyCount,
    missingCount,
  })
}

export function catalogServerFilters(
  query: ArtifactCatalogViewQuery,
): ArtifactCatalogFilters {
  const normalized = normalizeCatalogQuery(query)
  return Object.freeze({
    nodeIds: normalized.nodeIds,
    workerIds: normalized.workerIds,
    mediaTypes: normalized.mediaTypes,
    contentFamilies: normalized.contentFamilies,
    revisions: normalized.revisions,
    createdAfter: normalized.createdAfter,
    createdBefore: normalized.createdBefore,
    includeDeleted: normalized.includeDeleted,
  })
}

export function artifactCatalogWindow(
  artifacts: readonly ArtifactContract[],
  options: ArtifactCatalogVirtualOptions,
  selection: { selectedKey?: string; focusedKey?: string } = {},
): ArtifactCatalogWindow {
  const estimated = clampNumber(options.estimatedRowHeight ?? 74, 24, 512)
  const overscan = clampInteger(options.overscan ?? 8, 0, 100)
  const viewport = Math.max(1, finiteNumber(options.viewportHeight, 1))
  const scrollTop = Math.max(0, finiteNumber(options.scrollTop, 0))
  const heights = artifacts.map((artifact) =>
    clampNumber(
      options.measuredHeights?.get(artifactRevisionKey(artifact)) ?? estimated,
      24,
      1024,
    ),
  )
  const offsets = new Array<number>(artifacts.length + 1)
  offsets[0] = 0
  for (let index = 0; index < heights.length; index += 1) {
    offsets[index + 1] = offsets[index]! + heights[index]!
  }
  const totalHeight = offsets.at(-1) ?? 0
  const startIndex = lowerBound(offsets, scrollTop)
  const endIndex = Math.min(
    artifacts.length,
    lowerBound(offsets, scrollTop + viewport) + 1,
  )
  const overscanStartIndex = Math.max(0, startIndex - overscan)
  const overscanEndIndex = Math.min(artifacts.length, endIndex + overscan)
  const rows: ArtifactCatalogVirtualRow[] = []
  for (let index = overscanStartIndex; index < overscanEndIndex; index += 1) {
    const artifact = artifacts[index]!
    const key = artifactRevisionKey(artifact)
    rows.push(
      Object.freeze({
        key,
        artifact,
        index,
        top: offsets[index]!,
        height: heights[index]!,
        selected: key === selection.selectedKey,
        focused: key === selection.focusedKey,
        searchableText: artifactSearchText(artifact),
      }),
    )
  }
  return Object.freeze({
    startIndex,
    endIndex,
    overscanStartIndex,
    overscanEndIndex,
    beforeHeight: offsets[overscanStartIndex] ?? 0,
    afterHeight: Math.max(0, totalHeight - (offsets[overscanEndIndex] ?? 0)),
    totalHeight,
    rows: Object.freeze(rows),
  })
}

export function artifactSearchText(artifact: ArtifactContract): string {
  return [
    artifact.title,
    artifact.artifactId,
    artifact.kind,
    artifact.revision,
    artifact.sha256,
    artifact.mediaType,
    artifact.contentFamily,
    artifact.status.integrity,
    artifact.security.label,
    artifact.security.trust,
    artifact.producer.nodeId,
    artifact.producer.workerId,
    artifact.producer.toolCallId,
    artifact.producer.spanId,
    artifact.retention.policy,
  ]
    .filter(Boolean)
    .join("\n")
    .toLocaleLowerCase()
}

export function dispatchArtifactNavigation(
  selection: ArtifactCatalogSelection,
  target: Window = window,
): void {
  const normalized = normalizeSelection(selection)
  target.dispatchEvent(
    new CustomEvent("zyra:open-artifact", {
      detail: normalized,
    }),
  )
}

function normalizeSelection(
  selection: ArtifactCatalogSelection,
): ArtifactCatalogSelection {
  const sources = new Set<ArtifactCatalogSelection["source"]>([
    "artifact",
    "timeline",
    "topology",
    "event",
    "final-report",
  ])
  if (!sources.has(selection.source)) {
    throw new ArtifactCatalogError(
      "navigation_source_invalid",
      "Artifact navigation source is invalid.",
    )
  }
  const revision = selection.revision?.trim().toLowerCase()
  if (revision && !/^sha256:[0-9a-f]{64}$/.test(revision)) {
    throw new ArtifactCatalogError(
      "navigation_revision_invalid",
      "Artifact navigation revision is invalid.",
    )
  }
  return Object.freeze({
    artifactId: safeIdentity(selection.artifactId),
    revision,
    source: selection.source,
    sourceEventId: selection.sourceEventId
      ? safeIdentity(selection.sourceEventId)
      : undefined,
    focus: selection.focus === true,
  })
}

function comparatorFor(
  sort: ArtifactCatalogSort,
): (left: ArtifactContract, right: ArtifactContract) => number {
  if (sort === "created-asc") {
    return (left, right) =>
      Date.parse(left.createdAt) - Date.parse(right.createdAt)
      || artifactRevisionKey(left).localeCompare(artifactRevisionKey(right))
  }
  if (sort === "title-asc") {
    return (left, right) =>
      artifactDisplayName(left).localeCompare(artifactDisplayName(right))
      || compareCreatedDesc(left, right)
  }
  if (sort === "size-desc") {
    return (left, right) =>
      right.sizeBytes - left.sizeBytes || compareCreatedDesc(left, right)
  }
  if (sort === "size-asc") {
    return (left, right) =>
      left.sizeBytes - right.sizeBytes || compareCreatedDesc(left, right)
  }
  if (sort === "producer-asc") {
    return (left, right) =>
      (left.producer.nodeId ?? "").localeCompare(right.producer.nodeId ?? "")
      || (left.producer.workerId ?? "").localeCompare(
        right.producer.workerId ?? "",
      )
      || compareCreatedDesc(left, right)
  }
  return compareCreatedDesc
}

function compareCreatedDesc(
  left: ArtifactContract,
  right: ArtifactContract,
): number {
  return (
    Date.parse(right.createdAt) - Date.parse(left.createdAt)
    || artifactRevisionKey(right).localeCompare(artifactRevisionKey(left))
  )
}

function sameArtifact(
  left: ArtifactContract,
  right: ArtifactContract,
): boolean {
  return JSON.stringify(left) === JSON.stringify(right)
}

function tokenizeSearch(value: string): string[] {
  const tokens = value
    .split(/\s+/g)
    .map((token) => token.trim())
    .filter(Boolean)
  return [...new Set(tokens)].slice(0, 64)
}

function increment(map: Map<string, number>, key: string): void {
  map.set(key, (map.get(key) ?? 0) + 1)
}

function freezeMap(map: Map<string, number>): ReadonlyMap<string, number> {
  return new Map(
    [...map].sort((left, right) => {
      if (left[1] !== right[1]) return right[1] - left[1]
      return left[0].localeCompare(right[0])
    }),
  )
}

function emptyFacets(): ArtifactCatalogFacets {
  return Object.freeze({
    nodes: new Map(),
    workers: new Map(),
    mediaTypes: new Map(),
    contentFamilies: new Map(),
    integrity: new Map(),
    securityLabels: new Map(),
    revisions: new Map(),
    totalBytes: 0,
    verifiedBytes: 0,
    legacyCount: 0,
    missingCount: 0,
  })
}

function freezeState(state: ArtifactCatalogState): ArtifactCatalogState {
  return Object.freeze({
    ...state,
    artifacts: Object.freeze([...state.artifacts]),
    filtered: Object.freeze([...state.filtered]),
    query: Object.freeze({
      ...state.query,
      nodeIds: Object.freeze([...state.query.nodeIds]),
      workerIds: Object.freeze([...state.query.workerIds]),
      mediaTypes: Object.freeze([...state.query.mediaTypes]),
      contentFamilies: Object.freeze([...state.query.contentFamilies]),
      revisions: Object.freeze([...state.query.revisions]),
      integrity: Object.freeze([...state.query.integrity]),
      securityLabels: Object.freeze([...state.query.securityLabels]),
    }),
  })
}

function lowerBound(offsets: readonly number[], target: number): number {
  let low = 0
  let high = Math.max(0, offsets.length - 1)
  while (low < high) {
    const middle = Math.floor((low + high) / 2)
    if ((offsets[middle] ?? 0) < target) low = middle + 1
    else high = middle
  }
  return Math.max(0, Math.min(offsets.length - 1, low))
}

function uniqueStrings(
  values: readonly string[],
  maximumEntries: number,
  maximumBytes: number,
): string[] {
  const result: string[] = []
  for (const value of values) {
    const selected = boundedString(value, maximumBytes).trim()
    if (!selected || result.includes(selected)) continue
    result.push(selected)
    if (result.length >= maximumEntries) break
  }
  return result
}

function normalizeOptionalTimestamp(value: string | undefined): string | undefined {
  const selected = String(value || "").trim()
  if (!selected) return undefined
  if (!Number.isFinite(Date.parse(selected))) {
    throw new ArtifactCatalogError(
      "catalog_timestamp_invalid",
      "Artifact catalog timestamp filter is invalid.",
    )
  }
  return selected
}

function safeIdentity(value: string): string {
  const selected = boundedString(value, 512).trim()
  if (!selected || /[\r\n\u0000]/.test(selected)) {
    throw new ArtifactCatalogError(
      "catalog_identity_invalid",
      "Artifact catalog identity is invalid.",
    )
  }
  return selected
}

function boundedString(value: string, maximumBytes: number): string {
  const selected = String(value || "")
  if (new TextEncoder().encode(selected).byteLength > maximumBytes) {
    throw new ArtifactCatalogError(
      "catalog_value_too_large",
      `Artifact catalog value exceeds ${maximumBytes} bytes.`,
    )
  }
  return selected
}

function finiteNumber(value: number, fallback: number): number {
  return Number.isFinite(value) ? value : fallback
}

function clampNumber(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, finiteNumber(value, minimum)))
}

function clampInteger(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, Math.floor(finiteNumber(value, minimum))))
}
