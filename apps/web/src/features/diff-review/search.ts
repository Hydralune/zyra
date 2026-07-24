import type {
  DiffFileContract,
  DiffHunkContract,
  DiffLineContract,
} from "./contracts.ts"

export interface DiffSearchDocument {
  file: DiffFileContract
  hunks: readonly DiffHunkContract[]
  complete: boolean
  loadedBytes: number
}

export interface DiffSearchOptions {
  query: string
  caseSensitive?: boolean
  wholeWord?: boolean
  useRegExp?: boolean
  includeAdded?: boolean
  includeDeleted?: boolean
  includeContext?: boolean
  pathFilter?: string
  maximumResults?: number
  yieldEveryLines?: number
  signal?: AbortSignal
}

export interface DiffSearchMatch {
  matchId: string
  fileId: string
  path: string
  hunkId: string
  lineId: string
  lineKind: DiffLineContract["kind"]
  oldLine?: number
  newLine?: number
  start: number
  end: number
  text: string
  preview: string
}

export interface DiffSearchResult {
  query: string
  matches: readonly DiffSearchMatch[]
  scannedFiles: number
  scannedHunks: number
  scannedLines: number
  scannedBytes: number
  incompleteFiles: readonly string[]
  truncated: boolean
  cancelled: boolean
  durationMs: number
}

export class DiffSearchError extends Error {
  readonly code: string
  readonly details: Readonly<Record<string, unknown>>

  constructor(
    code: string,
    message: string,
    details: Readonly<Record<string, unknown>> = {},
  ) {
    super(message)
    this.name = "DiffSearchError"
    this.code = code
    this.details = Object.freeze({ ...details })
  }
}

const encoder = new TextEncoder()

function bounded(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
  name: string,
): number {
  if (value === undefined) return fallback
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new DiffSearchError(
      "diff_search_option_invalid",
      `${name} must be an integer from ${minimum} through ${maximum}.`,
      { actual: value },
    )
  }
  return value
}

function safeQuery(value: string): string {
  const query = String(value || "")
  const bytes = encoder.encode(query).byteLength
  if (!query.trim()) {
    throw new DiffSearchError(
      "diff_search_query_empty",
      "Diff search query must not be empty.",
    )
  }
  if (bytes > 8 * 1024) {
    throw new DiffSearchError(
      "diff_search_query_budget",
      "Diff search query exceeds 8 KiB.",
      { bytes },
    )
  }
  if (query.includes("\u0000")) {
    throw new DiffSearchError(
      "diff_search_query_control",
      "Diff search query contains a NUL byte.",
    )
  }
  return query
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
}

function searchPattern(options: DiffSearchOptions): RegExp {
  const query = safeQuery(options.query)
  let source = options.useRegExp ? query : escapeRegExp(query)
  if (options.wholeWord) source = `\\b(?:${source})\\b`
  try {
    return new RegExp(source, options.caseSensitive ? "gu" : "giu")
  } catch (error) {
    throw new DiffSearchError(
      "diff_search_regex_invalid",
      error instanceof Error ? error.message : "Invalid regular expression.",
      { query },
    )
  }
}

function pathMatches(path: string, filterValue: string | undefined): boolean {
  const filter = String(filterValue || "").trim().toLowerCase()
  if (!filter) return true
  const terms = filter.split(/\s+/g).filter(Boolean)
  const normalized = path.toLowerCase()
  return terms.every((term) => normalized.includes(term))
}

function lineIncluded(
  line: DiffLineContract,
  options: DiffSearchOptions,
): boolean {
  if (line.kind === "added") return options.includeAdded !== false
  if (line.kind === "deleted") return options.includeDeleted !== false
  if (line.kind === "context") return options.includeContext !== false
  return false
}

function previewFor(text: string, start: number, end: number): string {
  const maximum = 240
  const radius = Math.floor((maximum - (end - start)) / 2)
  const from = Math.max(0, start - Math.max(20, radius))
  const to = Math.min(text.length, end + Math.max(20, radius))
  return `${from > 0 ? "…" : ""}${text.slice(from, to)}${to < text.length ? "…" : ""}`
}

function matchIdentity(
  fileId: string,
  hunkId: string,
  lineId: string,
  start: number,
  end: number,
): string {
  return `diff-match:${encodeURIComponent(fileId)}:${encodeURIComponent(hunkId)}:${encodeURIComponent(lineId)}:${start}-${end}`
}

function abortError(signal: AbortSignal): DOMException {
  return new DOMException(
    String(signal.reason || "Diff search was cancelled."),
    "AbortError",
  )
}

async function cooperativeYield(signal: AbortSignal | undefined): Promise<void> {
  if (signal?.aborted) throw abortError(signal)
  await new Promise<void>((resolve) => setTimeout(resolve, 0))
  if (signal?.aborted) throw abortError(signal)
}

function matchesForLine(
  pattern: RegExp,
  document: DiffSearchDocument,
  hunk: DiffHunkContract,
  line: DiffLineContract,
  remaining: number,
): DiffSearchMatch[] {
  pattern.lastIndex = 0
  const matches: DiffSearchMatch[] = []
  let result: RegExpExecArray | null
  while (matches.length < remaining && (result = pattern.exec(line.text))) {
    const start = result.index
    const end = start + result[0].length
    matches.push(
      Object.freeze({
        matchId: matchIdentity(
          document.file.fileId,
          hunk.hunkId,
          line.lineId,
          start,
          end,
        ),
        fileId: document.file.fileId,
        path: document.file.path,
        hunkId: hunk.hunkId,
        lineId: line.lineId,
        lineKind: line.kind,
        oldLine: line.oldLine,
        newLine: line.newLine,
        start,
        end,
        text: result[0],
        preview: previewFor(line.text, start, end),
      }),
    )
    if (result[0].length === 0) pattern.lastIndex += 1
  }
  return matches
}

export async function searchDiffDocuments(
  documents: readonly DiffSearchDocument[],
  options: DiffSearchOptions,
): Promise<DiffSearchResult> {
  const startedAt = performance.now()
  const pattern = searchPattern(options)
  const maximumResults = bounded(
    options.maximumResults,
    10_000,
    1,
    100_000,
    "maximumResults",
  )
  const yieldEveryLines = bounded(
    options.yieldEveryLines,
    2_000,
    1,
    100_000,
    "yieldEveryLines",
  )
  const matches: DiffSearchMatch[] = []
  const incompleteFiles: string[] = []
  let scannedFiles = 0
  let scannedHunks = 0
  let scannedLines = 0
  let scannedBytes = 0
  let truncated = false
  try {
    for (const document of documents) {
      if (options.signal?.aborted) throw abortError(options.signal)
      if (!pathMatches(document.file.path, options.pathFilter)) continue
      scannedFiles += 1
      scannedBytes += document.loadedBytes
      if (!document.complete) incompleteFiles.push(document.file.fileId)
      for (const hunk of document.hunks) {
        scannedHunks += 1
        for (const line of hunk.lines) {
          scannedLines += 1
          if (scannedLines % yieldEveryLines === 0) {
            await cooperativeYield(options.signal)
          }
          if (!lineIncluded(line, options)) continue
          const remaining = maximumResults - matches.length
          if (remaining <= 0) {
            truncated = true
            break
          }
          const lineMatches = matchesForLine(
            pattern,
            document,
            hunk,
            line,
            remaining,
          )
          matches.push(...lineMatches)
          pattern.lastIndex = 0
          if (matches.length >= maximumResults) {
            truncated = true
            break
          }
        }
        if (truncated) break
      }
      if (truncated) break
    }
    return Object.freeze({
      query: options.query,
      matches: Object.freeze(matches),
      scannedFiles,
      scannedHunks,
      scannedLines,
      scannedBytes,
      incompleteFiles: Object.freeze(incompleteFiles),
      truncated,
      cancelled: false,
      durationMs: Math.max(0, performance.now() - startedAt),
    })
  } catch (error) {
    if (!(error instanceof DOMException && error.name === "AbortError")) {
      throw error
    }
    return Object.freeze({
      query: options.query,
      matches: Object.freeze(matches),
      scannedFiles,
      scannedHunks,
      scannedLines,
      scannedBytes,
      incompleteFiles: Object.freeze(incompleteFiles),
      truncated,
      cancelled: true,
      durationMs: Math.max(0, performance.now() - startedAt),
    })
  }
}

interface TrigramPosting {
  fileId: string
  hunkId: string
  lineId: string
  line: DiffLineContract
  document: DiffSearchDocument
  hunk: DiffHunkContract
}

function trigrams(value: string): readonly string[] {
  const normalized = value.toLowerCase()
  if (normalized.length < 3) return Object.freeze([normalized])
  const values = new Set<string>()
  for (let index = 0; index <= normalized.length - 3; index += 1) {
    values.add(normalized.slice(index, index + 3))
  }
  return Object.freeze([...values])
}

export class DiffSearchIndex {
  readonly #postings = new Map<string, Set<number>>()
  readonly #lines: TrigramPosting[] = []
  readonly #lineKeys = new Set<string>()
  readonly #documents = new Map<string, DiffSearchDocument>()
  #bytes = 0
  #closed = false

  add(document: DiffSearchDocument): {
    addedLines: number
    totalLines: number
    totalBytes: number
  } {
    this.#requireOpen()
    const prior = this.#documents.get(document.file.fileId)
    if (prior) {
      if (
        prior.loadedBytes === document.loadedBytes
        && prior.hunks.length === document.hunks.length
      ) {
        return {
          addedLines: 0,
          totalLines: this.#lines.length,
          totalBytes: this.#bytes,
        }
      }
    }
    this.#documents.set(document.file.fileId, document)
    let addedLines = 0
    for (const hunk of document.hunks) {
      for (const line of hunk.lines) {
        const key = `${document.file.fileId}:${hunk.hunkId}:${line.lineId}`
        if (this.#lineKeys.has(key)) continue
        this.#lineKeys.add(key)
        const postingIndex = this.#lines.length
        this.#lines.push({
          fileId: document.file.fileId,
          hunkId: hunk.hunkId,
          lineId: line.lineId,
          line,
          document,
          hunk,
        })
        this.#bytes += encoder.encode(line.text).byteLength
        addedLines += 1
        for (const gram of trigrams(line.text)) {
          let posting = this.#postings.get(gram)
          if (!posting) {
            posting = new Set()
            this.#postings.set(gram, posting)
          }
          posting.add(postingIndex)
        }
      }
    }
    return {
      addedLines,
      totalLines: this.#lines.length,
      totalBytes: this.#bytes,
    }
  }

  async search(options: DiffSearchOptions): Promise<DiffSearchResult> {
    this.#requireOpen()
    const query = safeQuery(options.query)
    if (
      options.useRegExp
      || options.caseSensitive
      || options.wholeWord
      || query.length < 3
    ) {
      return searchDiffDocuments([...this.#documents.values()], options)
    }
    const queryGrams = trigrams(query)
    const postingSets = queryGrams
      .map((gram) => this.#postings.get(gram) ?? new Set<number>())
      .sort((left, right) => left.size - right.size)
    if (!postingSets.length || postingSets[0]!.size === 0) {
      return Object.freeze({
        query,
        matches: Object.freeze([]),
        scannedFiles: 0,
        scannedHunks: 0,
        scannedLines: 0,
        scannedBytes: 0,
        incompleteFiles: Object.freeze(
          [...this.#documents.values()]
            .filter((document) => !document.complete)
            .map((document) => document.file.fileId),
        ),
        truncated: false,
        cancelled: false,
        durationMs: 0,
      })
    }
    const candidates = [...postingSets[0]!].filter((index) =>
      postingSets.every((set) => set.has(index)))
    const candidateDocuments = new Map<string, DiffSearchDocument>()
    const candidateHunks = new Map<string, Set<string>>()
    for (const index of candidates) {
      const posting = this.#lines[index]
      if (!posting) continue
      candidateDocuments.set(posting.fileId, posting.document)
      let hunks = candidateHunks.get(posting.fileId)
      if (!hunks) {
        hunks = new Set()
        candidateHunks.set(posting.fileId, hunks)
      }
      hunks.add(posting.hunkId)
    }
    const documents = [...candidateDocuments.values()].map((document) => {
      const hunks = candidateHunks.get(document.file.fileId) ?? new Set()
      return Object.freeze({
        ...document,
        hunks: Object.freeze(
          document.hunks.filter((hunk) => hunks.has(hunk.hunkId)),
        ),
      })
    })
    return searchDiffDocuments(documents, options)
  }

  snapshot(): {
    files: number
    lines: number
    trigrams: number
    bytes: number
    completeFiles: number
    closed: boolean
  } {
    return Object.freeze({
      files: this.#documents.size,
      lines: this.#lines.length,
      trigrams: this.#postings.size,
      bytes: this.#bytes,
      completeFiles: [...this.#documents.values()]
        .filter((document) => document.complete).length,
      closed: this.#closed,
    })
  }

  clear(): void {
    this.#postings.clear()
    this.#lines.length = 0
    this.#lineKeys.clear()
    this.#documents.clear()
    this.#bytes = 0
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    this.clear()
  }

  #requireOpen(): void {
    if (this.#closed) {
      throw new DiffSearchError(
        "diff_search_index_closed",
        "Diff search index is closed.",
      )
    }
  }
}
