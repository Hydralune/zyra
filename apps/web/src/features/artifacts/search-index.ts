export interface ArtifactSearchDocument {
  id: string
  lineNumber: number
  text: string
  startOffset: number
  endOffset: number
  complete: boolean
}

export interface ArtifactSearchIndexOptions {
  maximumDocuments?: number
  maximumBytes?: number
  maximumPostingsPerTerm?: number
  maximumTerms?: number
}

export interface ArtifactIndexedSearchOptions {
  caseSensitive?: boolean
  wholeWord?: boolean
  regularExpression?: boolean
  prefix?: boolean
  maximumResults?: number
  signal?: AbortSignal
}

export interface ArtifactIndexedSearchMatch {
  id: string
  documentId: string
  lineNumber: number
  startColumn: number
  endColumn: number
  startOffset: number
  endOffset: number
  preview: string
  completeLine: boolean
  score: number
}

export interface ArtifactIndexedSearchResult {
  query: string
  matches: readonly ArtifactIndexedSearchMatch[]
  candidateDocuments: number
  scannedDocuments: number
  scannedBytes: number
  indexedDocuments: number
  indexedBytes: number
  truncated: boolean
  cancelled: boolean
  elapsedMs: number
  strategy: "regular-expression" | "trigram" | "prefix" | "linear"
}

export interface ArtifactSearchIndexSnapshot {
  documents: number
  bytes: number
  trigrams: number
  prefixes: number
  evictedDocuments: number
  rejectedDocuments: number
  revisions: number
}

interface IndexedDocument extends ArtifactSearchDocument {
  normalized: string
  byteLength: number
  revision: number
  trigrams: readonly string[]
  prefixes: readonly string[]
}

interface Posting {
  documentId: string
  revision: number
}

const DEFAULT_DOCUMENTS = 250_000
const DEFAULT_BYTES = 64 * 1024 * 1024
const DEFAULT_POSTINGS = 250_000
const DEFAULT_TERMS = 1_000_000
const MAX_QUERY_BYTES = 4096
const MAX_RESULTS = 100_000

export class ArtifactTextSearchIndex {
  readonly #maximumDocuments: number
  readonly #maximumBytes: number
  readonly #maximumPostingsPerTerm: number
  readonly #maximumTerms: number
  readonly #documents = new Map<string, IndexedDocument>()
  readonly #trigrams = new Map<string, Posting[]>()
  readonly #prefixes = new Map<string, Posting[]>()
  #bytes = 0
  #revision = 0
  #evictedDocuments = 0
  #rejectedDocuments = 0
  #closed = false

  constructor(options: ArtifactSearchIndexOptions = {}) {
    this.#maximumDocuments = boundedInteger(
      options.maximumDocuments,
      DEFAULT_DOCUMENTS,
      1,
      1_000_000,
    )
    this.#maximumBytes = boundedInteger(
      options.maximumBytes,
      DEFAULT_BYTES,
      1024,
      1024 * 1024 * 1024,
    )
    this.#maximumPostingsPerTerm = boundedInteger(
      options.maximumPostingsPerTerm,
      DEFAULT_POSTINGS,
      1,
      1_000_000,
    )
    this.#maximumTerms = boundedInteger(
      options.maximumTerms,
      DEFAULT_TERMS,
      1,
      5_000_000,
    )
  }

  upsert(document: ArtifactSearchDocument): void {
    this.#assertOpen()
    const normalized = normalizeDocument(document)
    const previous = this.#documents.get(normalized.id)
    if (previous && sameDocument(previous, normalized)) {
      this.#touch(previous)
      return
    }
    if (normalized.byteLength > this.#maximumBytes) {
      this.#rejectedDocuments += 1
      throw new ArtifactSearchIndexError(
        "search_document_too_large",
        "Artifact search document exceeds the complete index byte bound.",
      )
    }
    if (previous) this.#remove(previous.id)
    const revision = ++this.#revision
    const indexed: IndexedDocument = {
      ...normalized,
      revision,
      trigrams: Object.freeze(uniqueTrigrams(normalized.normalized)),
      prefixes: Object.freeze(indexPrefixes(normalized.normalized)),
    }
    this.#documents.set(indexed.id, indexed)
    this.#bytes += indexed.byteLength
    this.#addPostings(this.#trigrams, indexed.trigrams, indexed)
    this.#addPostings(this.#prefixes, indexed.prefixes, indexed)
    this.#evict()
  }

  upsertMany(documents: readonly ArtifactSearchDocument[]): void {
    this.#assertOpen()
    for (const document of documents) this.upsert(document)
  }

  remove(documentId: string): boolean {
    this.#assertOpen()
    return this.#remove(documentId)
  }

  clear(): void {
    this.#documents.clear()
    this.#trigrams.clear()
    this.#prefixes.clear()
    this.#bytes = 0
  }

  async search(
    query: string,
    options: ArtifactIndexedSearchOptions = {},
  ): Promise<ArtifactIndexedSearchResult> {
    this.#assertOpen()
    const started = now()
    const selected = String(query || "")
    if (!selected) {
      return emptyResult(selected, this.snapshot(), now() - started)
    }
    if (new TextEncoder().encode(selected).byteLength > MAX_QUERY_BYTES) {
      throw new ArtifactSearchIndexError(
        "search_query_too_large",
        `Artifact search query exceeds ${MAX_QUERY_BYTES} bytes.`,
      )
    }
    const maximumResults = boundedInteger(
      options.maximumResults,
      10_000,
      1,
      MAX_RESULTS,
    )
    const strategy = chooseStrategy(selected, options)
    const candidates = this.#candidates(selected, strategy, options)
    const matcher = compileMatcher(selected, options)
    const matches: ArtifactIndexedSearchMatch[] = []
    let scannedDocuments = 0
    let scannedBytes = 0
    let cancelled = false
    for (const document of candidates) {
      if (options.signal?.aborted) {
        cancelled = true
        break
      }
      scannedDocuments += 1
      scannedBytes += document.byteLength
      const found = matcher(document.text)
      for (const occurrence of found) {
        matches.push(
          Object.freeze({
            id: `${document.id}:${occurrence.start}-${occurrence.end}`,
            documentId: document.id,
            lineNumber: document.lineNumber,
            startColumn: occurrence.start + 1,
            endColumn: occurrence.end + 1,
            startOffset: document.startOffset + occurrence.start,
            endOffset: document.startOffset + occurrence.end,
            preview: preview(document.text, occurrence.start, occurrence.end),
            completeLine: document.complete,
            score: matchScore(
              document,
              selected,
              occurrence.start,
              occurrence.end,
              options,
            ),
          }),
        )
        if (matches.length >= maximumResults) break
      }
      if (matches.length >= maximumResults) break
      if (scannedDocuments % 500 === 0) await yieldThread()
    }
    matches.sort((left, right) => {
      if (left.score !== right.score) return right.score - left.score
      if (left.lineNumber !== right.lineNumber) {
        return left.lineNumber - right.lineNumber
      }
      return left.startColumn - right.startColumn
    })
    return Object.freeze({
      query: selected,
      matches: Object.freeze(matches),
      candidateDocuments: candidates.length,
      scannedDocuments,
      scannedBytes,
      indexedDocuments: this.#documents.size,
      indexedBytes: this.#bytes,
      truncated: matches.length >= maximumResults,
      cancelled,
      elapsedMs: now() - started,
      strategy,
    })
  }

  snapshot(): ArtifactSearchIndexSnapshot {
    return Object.freeze({
      documents: this.#documents.size,
      bytes: this.#bytes,
      trigrams: this.#trigrams.size,
      prefixes: this.#prefixes.size,
      evictedDocuments: this.#evictedDocuments,
      rejectedDocuments: this.#rejectedDocuments,
      revisions: this.#revision,
    })
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    this.clear()
  }

  #candidates(
    query: string,
    strategy: ArtifactIndexedSearchResult["strategy"],
    options: ArtifactIndexedSearchOptions,
  ): IndexedDocument[] {
    if (strategy === "regular-expression" || strategy === "linear") {
      return [...this.#documents.values()]
    }
    const normalized = options.caseSensitive
      ? query
      : query.toLocaleLowerCase()
    if (strategy === "prefix") {
      const prefix = normalized.slice(0, Math.min(12, normalized.length))
      return this.#postingDocuments(this.#prefixes.get(prefix) ?? [])
    }
    const trigrams = uniqueTrigrams(normalized)
    if (!trigrams.length) return [...this.#documents.values()]
    const postingLists = trigrams
      .map((term) => this.#trigrams.get(term) ?? [])
      .sort((left, right) => left.length - right.length)
    if (!postingLists.length || postingLists[0]!.length === 0) return []
    const candidates = new Map<string, number>()
    for (const posting of postingLists[0]!) {
      candidates.set(posting.documentId, posting.revision)
    }
    for (let index = 1; index < postingLists.length; index += 1) {
      const accepted = new Set(
        postingLists[index]!.map(
          (posting) => `${posting.documentId}:${posting.revision}`,
        ),
      )
      for (const [documentId, revision] of [...candidates]) {
        if (!accepted.has(`${documentId}:${revision}`)) {
          candidates.delete(documentId)
        }
      }
      if (!candidates.size) break
    }
    return [...candidates]
      .map(([documentId, revision]) => {
        const document = this.#documents.get(documentId)
        return document?.revision === revision ? document : undefined
      })
      .filter((document): document is IndexedDocument => document !== undefined)
  }

  #postingDocuments(postings: readonly Posting[]): IndexedDocument[] {
    const result: IndexedDocument[] = []
    const seen = new Set<string>()
    for (const posting of postings) {
      if (seen.has(posting.documentId)) continue
      const document = this.#documents.get(posting.documentId)
      if (!document || document.revision !== posting.revision) continue
      seen.add(posting.documentId)
      result.push(document)
    }
    return result
  }

  #addPostings(
    index: Map<string, Posting[]>,
    terms: readonly string[],
    document: IndexedDocument,
  ): void {
    for (const term of terms) {
      let postings = index.get(term)
      if (!postings) {
        if (index.size >= this.#maximumTerms) continue
        postings = []
        index.set(term, postings)
      }
      if (postings.length >= this.#maximumPostingsPerTerm) continue
      postings.push({
        documentId: document.id,
        revision: document.revision,
      })
    }
  }

  #remove(documentId: string): boolean {
    const document = this.#documents.get(documentId)
    if (!document) return false
    this.#documents.delete(documentId)
    this.#bytes -= document.byteLength
    return true
  }

  #touch(document: IndexedDocument): void {
    this.#documents.delete(document.id)
    this.#documents.set(document.id, document)
  }

  #evict(): void {
    while (
      this.#documents.size > this.#maximumDocuments
      || this.#bytes > this.#maximumBytes
    ) {
      const oldest = this.#documents.keys().next().value as string | undefined
      if (!oldest) break
      this.#remove(oldest)
      this.#evictedDocuments += 1
    }
    this.#compactPostings(this.#trigrams)
    this.#compactPostings(this.#prefixes)
  }

  #compactPostings(index: Map<string, Posting[]>): void {
    if (this.#revision % 128 !== 0 && index.size < this.#maximumTerms) return
    for (const [term, postings] of index) {
      const valid = postings.filter((posting) => {
        const document = this.#documents.get(posting.documentId)
        return document?.revision === posting.revision
      })
      if (!valid.length) index.delete(term)
      else index.set(term, valid)
    }
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new ArtifactSearchIndexError(
        "search_index_closed",
        "Artifact search index is closed.",
      )
    }
  }
}

export class ArtifactSearchIndexError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = "ArtifactSearchIndexError"
    this.code = code
  }
}

function normalizeDocument(
  document: ArtifactSearchDocument,
): Omit<IndexedDocument, "revision" | "trigrams" | "prefixes"> {
  const id = safeDocumentId(document.id)
  const text = String(document.text ?? "")
  const startOffset = safeOffset(document.startOffset, "start")
  const endOffset = safeOffset(document.endOffset, "end")
  if (endOffset < startOffset) {
    throw new ArtifactSearchIndexError(
      "search_document_range",
      "Artifact search document ends before it begins.",
    )
  }
  const lineNumber = safeLine(document.lineNumber)
  const byteLength = new TextEncoder().encode(text).byteLength
  return {
    id,
    lineNumber,
    text,
    normalized: text.toLocaleLowerCase(),
    startOffset,
    endOffset,
    complete: document.complete === true,
    byteLength,
  }
}

function chooseStrategy(
  query: string,
  options: ArtifactIndexedSearchOptions,
): ArtifactIndexedSearchResult["strategy"] {
  if (options.regularExpression) return "regular-expression"
  if (options.prefix) return "prefix"
  if ([...query].length >= 3) return "trigram"
  return "linear"
}

function compileMatcher(
  query: string,
  options: ArtifactIndexedSearchOptions,
): (text: string) => readonly { start: number; end: number }[] {
  if (options.regularExpression) {
    let expression: RegExp
    try {
      expression = new RegExp(
        query,
        `${options.caseSensitive ? "" : "i"}gu`,
      )
    } catch (error) {
      throw new ArtifactSearchIndexError(
        "search_expression_invalid",
        error instanceof Error ? error.message : "Search expression is invalid.",
      )
    }
    return (text) => {
      expression.lastIndex = 0
      const matches: { start: number; end: number }[] = []
      for (const match of text.matchAll(expression)) {
        if (match.index === undefined) continue
        const end = match.index + Math.max(1, match[0].length)
        if (
          !options.wholeWord
          || wholeWord(text, match.index, end)
        ) {
          matches.push({ start: match.index, end })
        }
        if (matches.length >= 10_000) break
      }
      return matches
    }
  }
  const needle = options.caseSensitive ? query : query.toLocaleLowerCase()
  return (text) => {
    const haystack = options.caseSensitive ? text : text.toLocaleLowerCase()
    const matches: { start: number; end: number }[] = []
    let cursor = 0
    while (cursor <= haystack.length - needle.length) {
      const index = options.prefix
        ? prefixIndex(haystack, needle, cursor)
        : haystack.indexOf(needle, cursor)
      if (index < 0) break
      const end = index + needle.length
      if (!options.wholeWord || wholeWord(haystack, index, end)) {
        matches.push({ start: index, end })
      }
      cursor = Math.max(index + 1, end)
      if (matches.length >= 10_000) break
    }
    return matches
  }
}

function prefixIndex(text: string, query: string, from: number): number {
  const expression = /[\p{L}\p{N}_-]+/gu
  expression.lastIndex = from
  while (true) {
    const match = expression.exec(text)
    if (!match) return -1
    if (match[0].startsWith(query)) return match.index
  }
}

function uniqueTrigrams(value: string): string[] {
  const points = [...value]
  if (points.length < 3) return []
  const terms = new Set<string>()
  for (let index = 0; index <= points.length - 3; index += 1) {
    terms.add(`${points[index]}${points[index + 1]}${points[index + 2]}`)
    if (terms.size >= 16_384) break
  }
  return [...terms]
}

function indexPrefixes(value: string): string[] {
  const result = new Set<string>()
  const words = value.match(/[\p{L}\p{N}_-]+/gu) ?? []
  for (const word of words) {
    for (
      let length = 1;
      length <= Math.min(12, [...word].length);
      length += 1
    ) {
      result.add([...word].slice(0, length).join(""))
    }
    if (result.size >= 16_384) break
  }
  return [...result]
}

function matchScore(
  document: IndexedDocument,
  query: string,
  start: number,
  end: number,
  options: ArtifactIndexedSearchOptions,
): number {
  let score = 100
  if (start === 0) score += 20
  if (wholeWord(document.text, start, end)) score += 15
  if (document.complete) score += 5
  if (options.caseSensitive && document.text.slice(start, end) === query) score += 10
  score -= Math.min(50, start / 100)
  return score
}

function wholeWord(text: string, start: number, end: number): boolean {
  const before = start > 0 ? text[start - 1] : undefined
  const after = end < text.length ? text[end] : undefined
  const expression = /[\p{L}\p{N}_]/u
  return (!before || !expression.test(before)) && (!after || !expression.test(after))
}

function preview(text: string, start: number, end: number): string {
  const from = Math.max(0, start - 80)
  const to = Math.min(text.length, end + 80)
  return `${from > 0 ? "…" : ""}${text.slice(from, to)}${to < text.length ? "…" : ""}`
}

function sameDocument(
  left: IndexedDocument,
  right: Omit<IndexedDocument, "revision" | "trigrams" | "prefixes">,
): boolean {
  return (
    left.id === right.id
    && left.lineNumber === right.lineNumber
    && left.text === right.text
    && left.startOffset === right.startOffset
    && left.endOffset === right.endOffset
    && left.complete === right.complete
  )
}

function emptyResult(
  query: string,
  snapshot: ArtifactSearchIndexSnapshot,
  elapsedMs: number,
): ArtifactIndexedSearchResult {
  return Object.freeze({
    query,
    matches: Object.freeze([]),
    candidateDocuments: 0,
    scannedDocuments: 0,
    scannedBytes: 0,
    indexedDocuments: snapshot.documents,
    indexedBytes: snapshot.bytes,
    truncated: false,
    cancelled: false,
    elapsedMs,
    strategy: "linear",
  })
}

function safeDocumentId(value: string): string {
  const selected = String(value || "").trim()
  if (
    !selected
    || /[\r\n\u0000]/.test(selected)
    || new TextEncoder().encode(selected).byteLength > 4096
  ) {
    throw new ArtifactSearchIndexError(
      "search_document_identity",
      "Artifact search document identity is invalid.",
    )
  }
  return selected
}

function safeOffset(value: number, label: string): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new ArtifactSearchIndexError(
      `search_document_${label}_offset`,
      `Artifact search document ${label} offset is invalid.`,
    )
  }
  return value
}

function safeLine(value: number): number {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new ArtifactSearchIndexError(
      "search_document_line",
      "Artifact search document line number is invalid.",
    )
  }
  return value
}

function boundedInteger(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const selected = value ?? fallback
  if (!Number.isSafeInteger(selected) || selected < minimum || selected > maximum) {
    throw new ArtifactSearchIndexError(
      "search_index_limit",
      `Artifact search index limit must be between ${minimum} and ${maximum}.`,
    )
  }
  return selected
}

function now(): number {
  return typeof performance === "undefined" ? Date.now() : performance.now()
}

function yieldThread(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}
