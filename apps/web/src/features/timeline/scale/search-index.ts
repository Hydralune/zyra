import type {
  TimelineEventFacts,
  TimelineRow,
} from "../projection/index.ts"
import type {
  TimelineSearchDocument,
  TimelineSearchMatch,
  TimelineSearchQuery,
  TimelineSearchResult,
} from "./contracts.ts"

const TOKEN_PATTERN = /[\p{L}\p{N}_./:@#-]+/gu
const MAX_PREFIX_LENGTH = 24
const MAX_TRIGRAM_SOURCE = 240

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function normalize(value: unknown): string {
  return String(value ?? "")
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/\s+/g, " ")
    .trim()
}

function tokens(value: string): readonly string[] {
  return freeze(
    new Set(
      normalize(value)
        .match(TOKEN_PATTERN)
        ?.map((token) => token.replace(/^[-./:@#]+|[-./:@#]+$/g, ""))
        .filter(Boolean) ?? [],
    ),
  )
}

function prefixes(values: readonly string[]): readonly string[] {
  const result = new Set<string>()
  for (const token of values) {
    const length = Math.min(token.length, MAX_PREFIX_LENGTH)
    for (let index = 2; index <= length; index += 1) {
      result.add(token.slice(0, index))
    }
  }
  return freeze(result)
}

function trigrams(value: string): readonly string[] {
  const compact = normalize(value).replace(/\s+/g, " ").slice(0, MAX_TRIGRAM_SOURCE)
  if (compact.length < 3) return compact ? Object.freeze([compact]) : Object.freeze([])
  const result = new Set<string>()
  for (let index = 0; index <= compact.length - 3; index += 1) {
    result.add(compact.slice(index, index + 3))
  }
  return freeze(result)
}

function evidenceText(row: TimelineRow): string {
  return row.evidence
    .flatMap((evidence) => [
      evidence.kind,
      evidence.id,
      evidence.label,
      evidence.status ?? "",
      ...Object.entries(evidence.metadata).flatMap(([key, value]) => [
        key,
        String(value),
      ]),
    ])
    .join(" ")
}

function drilldownText(row: TimelineRow): string {
  return row.drilldowns
    .flatMap((target) => [
      target.kind,
      target.entityId,
      target.label,
      target.workerId ?? "",
      target.nodeId ?? "",
      target.failureId ?? "",
      target.recoveryId ?? "",
    ])
    .join(" ")
}

function factsText(facts: readonly TimelineEventFacts[]): string {
  return facts
    .flatMap((fact) => [
      fact.title,
      fact.summary,
      fact.event.eventId,
      fact.event.eventType,
      fact.worker?.workerId ?? "",
      fact.failureId ?? "",
      fact.recoveryId ?? "",
      fact.permissionId ?? "",
      fact.routeId ?? "",
      fact.placementId ?? "",
      ...Object.entries(fact.safeAttributes).flatMap(([key, value]) => [
        key,
        typeof value === "string" || typeof value === "number"
          ? String(value)
          : "",
      ]),
    ])
    .join(" ")
}

function rowText(
  row: TimelineRow,
  facts: readonly TimelineEventFacts[],
): string {
  return normalize(
    [
      row.title,
      row.summary,
      row.workerId ?? "",
      row.nodeId ?? "",
      row.leaseId ?? "",
      row.routeId ?? "",
      row.placementId ?? "",
      row.toolCallId ?? "",
      row.permissionId ?? "",
      row.failureId ?? "",
      row.recoveryId ?? "",
      row.correlationId,
      row.causationId ?? "",
      row.phase,
      row.rowKind,
      ...row.tags,
      ...row.eventIds,
      evidenceText(row),
      drilldownText(row),
      factsText(facts),
    ].join(" "),
  )
}

function buildDocument(
  row: TimelineRow,
  index: number,
  facts: readonly TimelineEventFacts[],
): TimelineSearchDocument {
  const normalized = rowText(row, facts)
  const documentTokens = tokens(normalized)
  return Object.freeze({
    rowKey: row.key,
    index,
    normalized,
    tokens: documentTokens,
    prefixes: prefixes(documentTokens),
    trigrams: trigrams(normalized),
    workerId: row.workerId,
    phase: row.phase,
    kind: row.rowKind,
    sequence: row.sequence,
    critical: row.critical,
    effective: row.effective,
  })
}

function addPosting(
  index: Map<string, Set<string>>,
  term: string,
  rowKey: string,
): void {
  const posting = index.get(term) ?? new Set<string>()
  posting.add(rowKey)
  index.set(term, posting)
}

function removePosting(
  index: Map<string, Set<string>>,
  term: string,
  rowKey: string,
): void {
  const posting = index.get(term)
  if (!posting) return
  posting.delete(rowKey)
  if (!posting.size) index.delete(term)
}

function intersection(
  postings: readonly ReadonlySet<string>[],
): Set<string> {
  if (!postings.length) return new Set()
  const ordered = [...postings].sort((left, right) => left.size - right.size)
  const result = new Set(ordered[0])
  for (const posting of ordered.slice(1)) {
    for (const key of result) {
      if (!posting.has(key)) result.delete(key)
    }
  }
  return result
}

function union(postings: readonly ReadonlySet<string>[]): Set<string> {
  const result = new Set<string>()
  for (const posting of postings) {
    for (const key of posting) result.add(key)
  }
  return result
}

function makeHighlights(
  normalized: string,
  queryTokens: readonly string[],
): TimelineSearchMatch["highlights"] {
  const matches: { start: number; end: number; text: string }[] = []
  for (const token of queryTokens) {
    let offset = 0
    while (offset < normalized.length && matches.length < 20) {
      const start = normalized.indexOf(token, offset)
      if (start < 0) break
      matches.push(
        Object.freeze({
          start,
          end: start + token.length,
          text: normalized.slice(start, start + token.length),
        }),
      )
      offset = start + Math.max(1, token.length)
    }
  }
  return freeze(
    matches
      .sort((left, right) => left.start - right.start || left.end - right.end)
      .filter(
        (match, index, all) =>
          index === 0 ||
          match.start >= all[index - 1].end ||
          match.text !== all[index - 1].text,
      ),
  )
}

function boundedLimit(value: number | undefined): number {
  if (!Number.isFinite(value)) return 200
  return Math.max(1, Math.min(5000, Math.floor(value as number)))
}

export class TimelineSearchIndex {
  #documents = new Map<string, TimelineSearchDocument>()
  #tokenPostings = new Map<string, Set<string>>()
  #prefixPostings = new Map<string, Set<string>>()
  #trigramPostings = new Map<string, Set<string>>()
  #sourceSignatures = new Map<string, string>()
  #revision = 0

  get revision(): number {
    return this.#revision
  }

  get size(): number {
    return this.#documents.size
  }

  documents(): readonly TimelineSearchDocument[] {
    return freeze(
      [...this.#documents.values()].sort((left, right) => left.index - right.index),
    )
  }

  rebuild(
    rows: readonly TimelineRow[],
    events: readonly TimelineEventFacts[] = [],
  ): {
    added: number
    updated: number
    removed: number
    unchanged: number
  } {
    const factsByEvent = new Map(events.map((fact) => [fact.event.eventId, fact]))
    const retained = new Set<string>()
    let added = 0
    let updated = 0
    let unchanged = 0
    rows.forEach((row, index) => {
      retained.add(row.key)
      const signature = [
        index,
        row.eventIds.join(","),
        row.title,
        row.summary,
        row.tags.join(","),
        row.evidence.map((evidence) => `${evidence.kind}:${evidence.id}:${evidence.status ?? ""}`).join(","),
      ].join("|")
      if (this.#sourceSignatures.get(row.key) === signature) {
        unchanged += 1
        return
      }
      const existing = this.#documents.get(row.key)
      if (existing) {
        this.#unindex(existing)
        updated += 1
      } else {
        added += 1
      }
      const facts = row.eventIds
        .map((eventId) => factsByEvent.get(eventId))
        .filter((fact): fact is TimelineEventFacts => Boolean(fact))
      const document = buildDocument(row, index, facts)
      this.#documents.set(row.key, document)
      this.#sourceSignatures.set(row.key, signature)
      this.#index(document)
    })
    let removed = 0
    for (const [rowKey, document] of this.#documents) {
      if (retained.has(rowKey)) continue
      this.#unindex(document)
      this.#documents.delete(rowKey)
      this.#sourceSignatures.delete(rowKey)
      removed += 1
    }
    if (added || updated || removed) this.#revision += 1
    return { added, updated, removed, unchanged }
  }

  search(query: TimelineSearchQuery): TimelineSearchResult {
    const started = performance.now()
    const queryText = normalize(query.text)
    const queryTokens = tokens(queryText)
    const queryTrigrams = trigrams(queryText)
    const tokenPostings = queryTokens.map((token) => {
      const exact = this.#tokenPostings.get(token)
      const prefix = this.#prefixPostings.get(token)
      return union([exact ?? new Set(), prefix ?? new Set()])
    })
    let candidates =
      tokenPostings.length > 0
        ? intersection(tokenPostings)
        : new Set(this.#documents.keys())
    if (!candidates.size && queryTrigrams.length) {
      const trigramPostings = queryTrigrams
        .map((trigram) => this.#trigramPostings.get(trigram))
        .filter((posting): posting is Set<string> => Boolean(posting))
      candidates = union(trigramPostings)
    }
    const workerIds = query.workerIds?.length
      ? new Set(query.workerIds)
      : undefined
    const phases = query.phases?.length ? new Set(query.phases) : undefined
    const kinds = query.kinds?.length ? new Set(query.kinds) : undefined
    const matches: TimelineSearchMatch[] = []
    let scannedDocuments = 0
    for (const rowKey of candidates) {
      const document = this.#documents.get(rowKey)
      if (!document) continue
      scannedDocuments += 1
      if (workerIds && (!document.workerId || !workerIds.has(document.workerId))) {
        continue
      }
      if (phases && !phases.has(document.phase)) continue
      if (kinds && !kinds.has(document.kind)) continue
      if (query.criticalOnly && !document.critical) continue
      if (query.effectiveOnly && !document.effective) continue
      const matchedTokens = queryTokens.filter(
        (token) =>
          document.tokens.includes(token) ||
          document.prefixes.includes(token) ||
          document.normalized.includes(token),
      )
      if (queryTokens.length && matchedTokens.length !== queryTokens.length) {
        continue
      }
      const exactPhrase = queryText && document.normalized.includes(queryText)
      const fields: string[] = []
      if (document.workerId && queryText.includes(normalize(document.workerId))) {
        fields.push("worker")
      }
      if (queryText.includes(document.phase)) fields.push("phase")
      if (queryText.includes(document.kind)) fields.push("kind")
      fields.push("content")
      const score =
        matchedTokens.reduce((total, token) => {
          const exact = document.tokens.includes(token) ? 12 : 0
          const prefixed = document.prefixes.includes(token) ? 6 : 0
          const frequency = document.normalized.split(token).length - 1
          return total + exact + prefixed + Math.min(8, frequency * 2)
        }, 0) +
        (exactPhrase ? 20 : 0) +
        (document.critical ? 4 : 0) +
        (document.effective ? 2 : 0)
      matches.push(
        Object.freeze({
          rowKey,
          index: document.index,
          score,
          matchedTokens: freeze(matchedTokens),
          matchedFields: freeze(new Set(fields)),
          highlights: makeHighlights(document.normalized, queryTokens),
        }),
      )
    }
    matches.sort(
      (left, right) =>
        right.score - left.score ||
        left.index - right.index ||
        left.rowKey.localeCompare(right.rowKey),
    )
    const totalMatches = matches.length
    return Object.freeze({
      query: Object.freeze({ ...query, text: queryText }),
      matches: freeze(matches.slice(0, boundedLimit(query.limit))),
      totalMatches,
      scannedDocuments,
      candidateDocuments: candidates.size,
      elapsedMs: Math.max(0, performance.now() - started),
    })
  }

  #index(document: TimelineSearchDocument): void {
    for (const token of document.tokens) {
      addPosting(this.#tokenPostings, token, document.rowKey)
    }
    for (const prefix of document.prefixes) {
      addPosting(this.#prefixPostings, prefix, document.rowKey)
    }
    for (const trigram of document.trigrams) {
      addPosting(this.#trigramPostings, trigram, document.rowKey)
    }
  }

  #unindex(document: TimelineSearchDocument): void {
    for (const token of document.tokens) {
      removePosting(this.#tokenPostings, token, document.rowKey)
    }
    for (const prefix of document.prefixes) {
      removePosting(this.#prefixPostings, prefix, document.rowKey)
    }
    for (const trigram of document.trigrams) {
      removePosting(this.#trigramPostings, trigram, document.rowKey)
    }
  }
}

export function searchTimelineRows(
  rows: readonly TimelineRow[],
  query: TimelineSearchQuery,
  events: readonly TimelineEventFacts[] = [],
): TimelineSearchResult {
  const index = new TimelineSearchIndex()
  index.rebuild(rows, events)
  return index.search(query)
}
