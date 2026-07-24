import {
  TraceCompleteness,
  TraceNodeKind,
  TraceSemanticKind,
  type CausalTraceProjection,
  type TraceFilter,
  type TraceFilteredProjection,
  type TraceHiddenGap,
  type TraceNode,
  type TraceSearchMatch,
} from "../contracts.ts"

type SearchField =
  | "any"
  | "title"
  | "summary"
  | "kind"
  | "semantic"
  | "tag"
  | "event"
  | "span"
  | "worker"
  | "tool"
  | "artifact"
  | "mutation"
  | "checkpoint"
  | "provider"
  | "failure"
  | "recovery"
  | "completeness"

interface QueryTerm {
  raw: string
  value: string
  field: SearchField
  negative: boolean
  phrase: boolean
  required: boolean
}

interface IndexedDocument {
  key: string
  fields: Readonly<Record<SearchField, string>>
  tokens: ReadonlySet<string>
  tokenPositions: Readonly<Record<string, readonly number[]>>
  ordinal: number
}

export interface TraceSearchResult {
  query: string
  terms: readonly QueryTerm[]
  matches: readonly TraceSearchMatch[]
  elapsedMs: number
  scannedDocuments: number
  candidateDocuments: number
  truncated: boolean
}

export interface TraceSearchAudit {
  closed: boolean
  documentCount: number
  tokenCount: number
  postingCount: number
  queryCount: number
  cacheHits: number
  lastQuery?: string
  lastCandidateCount: number
  lastMatchCount: number
}

const FIELD_PREFIX: Readonly<Record<string, SearchField>> = Object.freeze({
  title: "title",
  summary: "summary",
  kind: "kind",
  semantic: "semantic",
  semantics: "semantic",
  tag: "tag",
  event: "event",
  span: "span",
  worker: "worker",
  tool: "tool",
  artifact: "artifact",
  mutation: "mutation",
  checkpoint: "checkpoint",
  provider: "provider",
  failure: "failure",
  recovery: "recovery",
  completeness: "completeness",
  status: "completeness",
})

const FIELD_WEIGHT: Readonly<Record<SearchField, number>> = Object.freeze({
  any: 1,
  title: 5,
  summary: 2,
  kind: 4,
  semantic: 4,
  tag: 3,
  event: 6,
  span: 6,
  worker: 6,
  tool: 6,
  artifact: 6,
  mutation: 6,
  checkpoint: 6,
  provider: 6,
  failure: 6,
  recovery: 6,
  completeness: 4,
})

function normalizeText(value: string): string {
  return value.normalize("NFKC").toLocaleLowerCase().replace(/\s+/g, " ").trim()
}

function tokenize(value: string): readonly string[] {
  const normalized = normalizeText(value)
  if (!normalized) return Object.freeze([])
  const tokens = normalized
    .split(/[^\p{L}\p{N}._:/@+-]+/u)
    .map((token) => token.trim())
    .filter(Boolean)
  const expanded: string[] = []
  for (const token of tokens) {
    expanded.push(token)
    if (token.includes(".")) expanded.push(...token.split(".").filter(Boolean))
    if (token.includes(":")) expanded.push(...token.split(":").filter(Boolean))
    if (token.includes("-")) expanded.push(...token.split("-").filter(Boolean))
  }
  return Object.freeze([...new Set(expanded)])
}

function scanQuery(query: string): readonly string[] {
  const output: string[] = []
  let current = ""
  let quoted = false
  let escaped = false
  for (const character of query.trim()) {
    if (escaped) {
      current += character
      escaped = false
      continue
    }
    if (character === "\\") {
      escaped = true
      continue
    }
    if (character === '"') {
      quoted = !quoted
      current += character
      continue
    }
    if (/\s/.test(character) && !quoted) {
      if (current) output.push(current)
      current = ""
      continue
    }
    current += character
  }
  if (current) output.push(current)
  return Object.freeze(output)
}

function parseTerm(rawInput: string): QueryTerm | undefined {
  let raw = rawInput.trim()
  if (!raw) return undefined
  let negative = false
  let required = false
  if (raw.startsWith("-")) {
    negative = true
    raw = raw.slice(1)
  } else if (raw.startsWith("+")) {
    required = true
    raw = raw.slice(1)
  }
  let field: SearchField = "any"
  const separator = raw.indexOf(":")
  if (separator > 0) {
    const candidate = FIELD_PREFIX[raw.slice(0, separator).toLowerCase()]
    if (candidate) {
      field = candidate
      raw = raw.slice(separator + 1)
    }
  }
  const phrase = raw.length >= 2 && raw.startsWith('"') && raw.endsWith('"')
  const value = normalizeText(phrase ? raw.slice(1, -1) : raw)
  if (!value) return undefined
  return Object.freeze({ raw: rawInput, value, field, negative, phrase, required })
}

export function parseTraceSearchQuery(query: string): readonly QueryTerm[] {
  return Object.freeze(
    scanQuery(query)
      .map(parseTerm)
      .filter((term): term is QueryTerm => Boolean(term)),
  )
}

function nodeFields(node: TraceNode): Readonly<Record<SearchField, string>> {
  const artifacts = node.refs.artifactIds.join(" ")
  const any = [
    node.title,
    node.summary,
    node.key,
    node.identity.kind,
    node.identity.id,
    node.semantics.join(" "),
    node.tags.join(" "),
    node.eventIds.join(" "),
    node.refs.spanId,
    node.refs.workerId,
    node.refs.toolCallId,
    artifacts,
    node.refs.mutationId,
    node.refs.checkpointId,
    node.refs.providerId,
    node.refs.failureId,
    node.refs.recoveryId,
    node.completeness,
  ].filter(Boolean).join(" ")
  return Object.freeze({
    any: normalizeText(any),
    title: normalizeText(node.title),
    summary: normalizeText(node.summary),
    kind: normalizeText(`${node.identity.kind} ${node.identity.id}`),
    semantic: normalizeText(node.semantics.join(" ")),
    tag: normalizeText(node.tags.join(" ")),
    event: normalizeText(node.eventIds.join(" ")),
    span: normalizeText(node.refs.spanId ?? ""),
    worker: normalizeText(node.refs.workerId ?? ""),
    tool: normalizeText(node.refs.toolCallId ?? ""),
    artifact: normalizeText(artifacts),
    mutation: normalizeText(node.refs.mutationId ?? ""),
    checkpoint: normalizeText(node.refs.checkpointId ?? ""),
    provider: normalizeText(node.refs.providerId ?? ""),
    failure: normalizeText(node.refs.failureId ?? ""),
    recovery: normalizeText(node.refs.recoveryId ?? ""),
    completeness: normalizeText(node.completeness),
  })
}

function documentForNode(node: TraceNode, ordinal: number): IndexedDocument {
  const fields = nodeFields(node)
  const allTokens = tokenize(fields.any)
  const positions: Record<string, number[]> = {}
  const sourceWords = fields.any.split(" ")
  for (let index = 0; index < sourceWords.length; index += 1) {
    for (const token of tokenize(sourceWords[index] ?? "")) {
      const values = positions[token]
      if (values) values.push(index)
      else positions[token] = [index]
    }
  }
  return Object.freeze({
    key: node.key,
    fields,
    tokens: new Set(allTokens),
    tokenPositions: Object.freeze(
      Object.fromEntries(Object.entries(positions).map(([token, values]) => [token, Object.freeze(values)])),
    ),
    ordinal,
  })
}

function tokenCandidates(
  term: QueryTerm,
  postings: ReadonlyMap<string, ReadonlySet<string>>,
): ReadonlySet<string> | undefined {
  if (term.phrase || term.field !== "any") return undefined
  const tokens = tokenize(term.value)
  if (!tokens.length) return undefined
  let candidate: Set<string> | undefined
  for (const token of tokens) {
    const values = postings.get(token)
    if (!values) return new Set()
    if (!candidate) candidate = new Set(values)
    else {
      for (const key of candidate) if (!values.has(key)) candidate.delete(key)
    }
  }
  return candidate
}

function intersects(left: ReadonlySet<string>, right: ReadonlySet<string>): Set<string> {
  const output = new Set<string>()
  const [small, large] = left.size <= right.size ? [left, right] : [right, left]
  for (const value of small) if (large.has(value)) output.add(value)
  return output
}

function termMatch(document: IndexedDocument, term: QueryTerm): {
  matched: boolean
  score: number
  fields: readonly string[]
  ranges: readonly { field: string; start: number; end: number }[]
} {
  const fieldNames: SearchField[] = term.field === "any"
    ? ["title", "summary", "kind", "semantic", "tag", "event", "span", "worker", "tool", "artifact", "mutation", "checkpoint", "provider", "failure", "recovery", "completeness"]
    : [term.field]
  let score = 0
  const fields: string[] = []
  const ranges: Array<{ field: string; start: number; end: number }> = []
  for (const field of fieldNames) {
    const text = document.fields[field]
    if (!text) continue
    const index = text.indexOf(term.value)
    const exactToken = !term.phrase && tokenize(term.value).every((token) =>
      new Set(tokenize(text)).has(token),
    )
    if (index < 0 && !exactToken) continue
    fields.push(field)
    const weight = FIELD_WEIGHT[field]
    score += weight * (index === 0 ? 2 : 1) * (term.phrase ? 1.5 : 1)
    if (index >= 0) ranges.push({ field, start: index, end: index + term.value.length })
  }
  return Object.freeze({
    matched: fields.length > 0,
    score,
    fields: Object.freeze(fields),
    ranges: Object.freeze(ranges),
  })
}

function querySignature(query: string, limit: number): string {
  return `${normalizeText(query)}|${limit}`
}

export class TraceSearchIndex {
  readonly #documents = new Map<string, IndexedDocument>()
  readonly #postings = new Map<string, Set<string>>()
  readonly #now: () => number
  readonly #cache = new Map<string, TraceSearchResult>()
  #closed = false
  #queryCount = 0
  #cacheHits = 0
  #lastQuery?: string
  #lastCandidateCount = 0
  #lastMatchCount = 0

  constructor(nodes: readonly TraceNode[], options: { now?: () => number } = {}) {
    this.#now = options.now ?? (() => Date.now())
    nodes.forEach((node, ordinal) => this.add(node, ordinal))
  }

  add(node: TraceNode, ordinal = this.#documents.size): void {
    if (this.#closed) throw new Error("Trace search index is closed.")
    this.remove(node.key)
    const document = documentForNode(node, ordinal)
    this.#documents.set(node.key, document)
    for (const token of document.tokens) {
      const posting = this.#postings.get(token)
      if (posting) posting.add(node.key)
      else this.#postings.set(token, new Set([node.key]))
    }
    this.#cache.clear()
  }

  remove(key: string): boolean {
    const existing = this.#documents.get(key)
    if (!existing) return false
    this.#documents.delete(key)
    for (const token of existing.tokens) {
      const posting = this.#postings.get(token)
      if (!posting) continue
      posting.delete(key)
      if (!posting.size) this.#postings.delete(token)
    }
    this.#cache.clear()
    return true
  }

  search(query: string, limit = 500): TraceSearchResult {
    if (this.#closed) throw new Error("Trace search index is closed.")
    const boundedLimit = Math.max(1, Math.min(10_000, Math.trunc(limit)))
    const signature = querySignature(query, boundedLimit)
    const cached = this.#cache.get(signature)
    if (cached) {
      this.#cacheHits += 1
      return cached
    }
    const started = this.#now()
    const terms = parseTraceSearchQuery(query)
    let candidates: Set<string> | undefined
    for (const term of terms) {
      if (term.negative) continue
      const posting = tokenCandidates(term, this.#postings)
      if (!posting) continue
      candidates = candidates ? intersects(candidates, posting) : new Set(posting)
      if (!candidates.size) break
    }
    if (!candidates) candidates = new Set(this.#documents.keys())
    const matches: TraceSearchMatch[] = []
    for (const key of candidates) {
      const document = this.#documents.get(key)
      if (!document) continue
      let score = 0
      let rejected = false
      const matchedTerms: string[] = []
      const matchedFields = new Set<string>()
      const ranges: Array<{ field: string; start: number; end: number }> = []
      for (const term of terms) {
        const match = termMatch(document, term)
        if (term.negative && match.matched) {
          rejected = true
          break
        }
        if (!term.negative && (term.required || terms.every((value) => value.negative || value.required)) && !match.matched) {
          rejected = true
          break
        }
        if (!term.negative && match.matched) {
          matchedTerms.push(term.raw)
          match.fields.forEach((field) => matchedFields.add(field))
          ranges.push(...match.ranges)
          score += match.score
        }
      }
      if (rejected) continue
      const positiveCount = terms.filter((term) => !term.negative).length
      if (positiveCount && !matchedTerms.length) continue
      const node = this.#documents.get(key)
      score += node ? Math.max(0, 1 - node.ordinal / Math.max(1, this.#documents.size)) : 0
      matches.push(Object.freeze({
        nodeKey: key,
        score,
        matchedTerms: Object.freeze(matchedTerms),
        matchedFields: Object.freeze([...matchedFields]),
        ranges: Object.freeze(ranges),
      }))
    }
    matches.sort((left, right) =>
      right.score - left.score ||
      (this.#documents.get(left.nodeKey)?.ordinal ?? 0) -
        (this.#documents.get(right.nodeKey)?.ordinal ?? 0) ||
      left.nodeKey.localeCompare(right.nodeKey),
    )
    const result: TraceSearchResult = Object.freeze({
      query,
      terms,
      matches: Object.freeze(matches.slice(0, boundedLimit)),
      elapsedMs: Math.max(0, this.#now() - started),
      scannedDocuments: candidates.size,
      candidateDocuments: candidates.size,
      truncated: matches.length > boundedLimit,
    })
    this.#queryCount += 1
    this.#lastQuery = query
    this.#lastCandidateCount = candidates.size
    this.#lastMatchCount = result.matches.length
    this.#cache.set(signature, result)
    while (this.#cache.size > 32) this.#cache.delete(this.#cache.keys().next().value!)
    return result
  }

  close(): void {
    this.#closed = true
    this.#documents.clear()
    this.#postings.clear()
    this.#cache.clear()
  }

  audit(): TraceSearchAudit {
    let postingCount = 0
    for (const values of this.#postings.values()) postingCount += values.size
    return Object.freeze({
      closed: this.#closed,
      documentCount: this.#documents.size,
      tokenCount: this.#postings.size,
      postingCount,
      queryCount: this.#queryCount,
      cacheHits: this.#cacheHits,
      lastQuery: this.#lastQuery,
      lastCandidateCount: this.#lastCandidateCount,
      lastMatchCount: this.#lastMatchCount,
    })
  }
}

function hasFailure(node: TraceNode): boolean {
  return node.semantics.includes(TraceSemanticKind.FAULT) ||
    Boolean(node.refs.failureId) ||
    node.tags.some((tag) => /fail|error|crash|timeout/.test(tag))
}

function nodeMatchesFilter(node: TraceNode, filter: TraceFilter): boolean {
  if (filter.semantics?.length && !filter.semantics.some((semantic) => node.semantics.includes(semantic))) {
    return false
  }
  if (filter.nodeKinds?.length && !filter.nodeKinds.includes(node.identity.kind)) return false
  if (filter.workerIds?.length && (!node.refs.workerId || !filter.workerIds.includes(node.refs.workerId))) {
    return false
  }
  if (filter.providerIds?.length && (!node.refs.providerId || !filter.providerIds.includes(node.refs.providerId))) {
    return false
  }
  if (filter.completeness?.length && !filter.completeness.includes(node.completeness)) return false
  if (filter.fromSequence !== undefined && node.endSequence < filter.fromSequence) return false
  if (filter.toSequence !== undefined && node.sequence > filter.toSequence) return false
  if (filter.criticalOnly && !node.critical) return false
  if (filter.failuresOnly && !hasFailure(node)) return false
  if (filter.retriesOnly && node.retryCount <= 0) return false
  if (filter.includeNonEffective === false && !node.effective) return false
  if (filter.includeIssueNodes === false && node.identity.kind === TraceNodeKind.ISSUE) return false
  return true
}

function activeFilterCount(filter: TraceFilter): number {
  let count = 0
  if (filter.search?.trim()) count += 1
  if (filter.semantics?.length) count += 1
  if (filter.nodeKinds?.length) count += 1
  if (filter.workerIds?.length) count += 1
  if (filter.providerIds?.length) count += 1
  if (filter.completeness?.length) count += 1
  if (filter.fromSequence !== undefined) count += 1
  if (filter.toSequence !== undefined) count += 1
  if (filter.criticalOnly) count += 1
  if (filter.failuresOnly) count += 1
  if (filter.retriesOnly) count += 1
  if (filter.includeNonEffective === false) count += 1
  if (filter.includeIssueNodes === false) count += 1
  return count
}

function hiddenGap(
  ordinal: number,
  hidden: readonly TraceNode[],
  beforeKey: string | undefined,
  afterKey: string | undefined,
  projection: CausalTraceProjection,
): TraceHiddenGap {
  const semantics: Record<string, number> = {}
  const hiddenKeys = new Set(hidden.map((node) => node.key))
  for (const node of hidden) {
    for (const semantic of node.semantics) semantics[semantic] = (semantics[semantic] ?? 0) + 1
  }
  const causalBridge = projection.edges.some((edge) =>
    hiddenKeys.has(edge.sourceKey) !== hiddenKeys.has(edge.targetKey) &&
    (edge.sourceKey === beforeKey || edge.targetKey === afterKey ||
      hiddenKeys.has(edge.sourceKey) || hiddenKeys.has(edge.targetKey)),
  )
  return Object.freeze({
    id: `trace-gap:${ordinal}:${hidden[0]?.key ?? "empty"}`,
    beforeKey,
    afterKey,
    hiddenNodeKeys: Object.freeze(hidden.map((node) => node.key)),
    hiddenEventIds: Object.freeze([...new Set(hidden.flatMap((node) => node.eventIds))]),
    count: hidden.length,
    startSequence: Math.min(...hidden.map((node) => node.sequence)),
    endSequence: Math.max(...hidden.map((node) => node.endSequence)),
    semantics: Object.freeze(semantics),
    containsCritical: hidden.some((node) => node.critical),
    containsFailure: hidden.some(hasFailure),
    containsRecovery: hidden.some((node) => node.semantics.includes(TraceSemanticKind.RECOVERY)),
    causalBridge,
  })
}

function buildGaps(
  projection: CausalTraceProjection,
  visibleKeys: ReadonlySet<string>,
): readonly TraceHiddenGap[] {
  const gaps: TraceHiddenGap[] = []
  let hidden: TraceNode[] = []
  let beforeKey: string | undefined
  const flush = (afterKey?: string) => {
    if (!hidden.length) return
    gaps.push(hiddenGap(gaps.length, hidden, beforeKey, afterKey, projection))
    hidden = []
  }
  for (const node of projection.nodes) {
    if (visibleKeys.has(node.key)) {
      flush(node.key)
      beforeKey = node.key
    } else hidden.push(node)
  }
  flush(undefined)
  return Object.freeze(gaps)
}

function edgeVisible(
  sourceVisible: boolean,
  targetVisible: boolean,
  sourceAncestors: readonly string[],
  targetAncestors: readonly string[],
  visibleKeys: ReadonlySet<string>,
): boolean {
  if (sourceVisible && targetVisible) return true
  if (sourceVisible && targetAncestors.some((key) => visibleKeys.has(key))) return true
  if (targetVisible && sourceAncestors.some((key) => visibleKeys.has(key))) return true
  return false
}

export function filterCausalTrace(
  projection: CausalTraceProjection,
  filter: TraceFilter = {},
  searchIndex?: TraceSearchIndex,
): TraceFilteredProjection {
  const query = filter.search?.trim() ?? ""
  const search = query
    ? (searchIndex ?? new TraceSearchIndex(projection.nodes)).search(query, projection.nodes.length)
    : undefined
  const matchedKeys = search ? new Set(search.matches.map((match) => match.nodeKey)) : undefined
  const searchMatchByKey = new Map(search?.matches.map((match) => [match.nodeKey, match]) ?? [])
  const nodes = Object.freeze(projection.nodes.filter((node) =>
    (!matchedKeys || matchedKeys.has(node.key)) && nodeMatchesFilter(node, filter),
  ))
  const visibleKeys = new Set(nodes.map((node) => node.key))
  const visibleEdgeIds = Object.freeze(projection.edges
    .filter((edge) => edgeVisible(
      visibleKeys.has(edge.sourceKey),
      visibleKeys.has(edge.targetKey),
      projection.hierarchy.ancestorsByKey[edge.sourceKey] ?? [],
      projection.hierarchy.ancestorsByKey[edge.targetKey] ?? [],
      visibleKeys,
    ))
    .map((edge) => edge.id))
  const hiddenNodes = projection.nodes.filter((node) => !visibleKeys.has(node.key))
  return Object.freeze({
    nodes,
    matches: Object.freeze(nodes
      .map((node) => searchMatchByKey.get(node.key))
      .filter((match): match is TraceSearchMatch => Boolean(match))),
    gaps: buildGaps(projection, visibleKeys),
    visibleNodeKeys: Object.freeze(nodes.map((node) => node.key)),
    visibleEdgeIds,
    hiddenNodeCount: hiddenNodes.length,
    hiddenEventCount: new Set(hiddenNodes.flatMap((node) => node.eventIds)).size,
    activeFilterCount: activeFilterCount(filter),
  })
}

export function defaultTraceFilter(): TraceFilter {
  return Object.freeze({
    search: "",
    semantics: Object.freeze([]),
    nodeKinds: Object.freeze([]),
    workerIds: Object.freeze([]),
    providerIds: Object.freeze([]),
    completeness: Object.freeze([
      TraceCompleteness.COMPLETE,
      TraceCompleteness.PARTIAL,
      TraceCompleteness.LATE,
      TraceCompleteness.ORPHAN,
      TraceCompleteness.QUARANTINED,
    ]),
    criticalOnly: false,
    failuresOnly: false,
    retriesOnly: false,
    includeNonEffective: true,
    includeIssueNodes: true,
  })
}

export function availableTraceSemantics(
  projection: CausalTraceProjection,
): readonly { semantic: string; count: number }[] {
  const values = Object.entries(projection.indexes.nodeKeysBySemantic)
    .map(([semantic, keys]) => ({ semantic, count: keys.length }))
    .sort((left, right) => right.count - left.count || left.semantic.localeCompare(right.semantic))
  return Object.freeze(values)
}

export function availableTraceWorkers(
  projection: CausalTraceProjection,
): readonly { workerId: string; count: number }[] {
  const values = Object.entries(projection.indexes.nodeKeysByWorker)
    .map(([workerId, keys]) => ({ workerId, count: keys.length }))
    .sort((left, right) => right.count - left.count || left.workerId.localeCompare(right.workerId))
  return Object.freeze(values)
}

export function traceAttentionFilter(projection: CausalTraceProjection): TraceFilter {
  const completeness = [
    TraceCompleteness.PARTIAL,
    TraceCompleteness.LATE,
    TraceCompleteness.ORPHAN,
    TraceCompleteness.QUARANTINED,
  ] as const
  const hasIntegrityIssue = projection.nodes.some((node) => completeness.includes(
    node.completeness as (typeof completeness)[number],
  ))
  return Object.freeze({
    ...defaultTraceFilter(),
    completeness: hasIntegrityIssue ? completeness : undefined,
    semantics: hasIntegrityIssue
      ? undefined
      : Object.freeze([
          TraceSemanticKind.FAULT,
          TraceSemanticKind.RECOVERY,
          TraceSemanticKind.PROVIDER_RETRY,
          TraceSemanticKind.MCP_RECONNECT,
        ]),
  })
}
