import type {
  FilterResult,
  GraphEntityKind,
  GraphNodeRecord,
  SearchMatch,
  TopologyFilterState,
  TopologyGraphModel,
} from "./contracts.ts"

export const DEFAULT_TOPOLOGY_FILTERS: TopologyFilterState = Object.freeze({
  query: "",
  status: "all",
  namespaces: Object.freeze([]),
  roles: Object.freeze([]),
  locations: Object.freeze([]),
  providers: Object.freeze([]),
  models: Object.freeze([]),
  privacyClasses: Object.freeze([]),
  capabilities: Object.freeze([]),
  changedOnly: false,
  openWorldOnly: false,
  policyViolationsOnly: false,
  includeRemoved: false,
  includePending: true,
  includeInferredEdges: true,
})

interface SearchDocument {
  id: string
  kind: GraphEntityKind
  primary: string
  secondary: string
  fields: Readonly<Record<string, string>>
  normalized: string
  tokens: readonly string[]
  sequence: number
}
interface QueryTerm {
  field?: string
  value: string
  negative: boolean
  quoted: boolean
}

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function normalizedText(value: unknown): string {
  return String(value ?? "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase()
    .replace(/[_/\\.:|-]+/g, " ")
    .replace(/[^\p{L}\p{N}\s-]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim()
}

function tokenize(value: string): readonly string[] {
  return freeze(
    [...new Set(normalizedText(value).split(" ").filter((token) => token.length > 0))],
  )
}

function editDistance(left: string, right: string, maximum = 3): number {
  if (left === right) return 0
  if (Math.abs(left.length - right.length) > maximum) return maximum + 1
  const previous = new Array(right.length + 1)
  const current = new Array(right.length + 1)
  for (let column = 0; column <= right.length; column += 1) previous[column] = column
  for (let row = 1; row <= left.length; row += 1) {
    current[0] = row
    let minimum = current[0]
    for (let column = 1; column <= right.length; column += 1) {
      const substitution = previous[column - 1] + (left[row - 1] === right[column - 1] ? 0 : 1)
      current[column] = Math.min(
        previous[column] + 1,
        current[column - 1] + 1,
        substitution,
      )
      minimum = Math.min(minimum, current[column])
    }
    if (minimum > maximum) return maximum + 1
    for (let column = 0; column <= right.length; column += 1) {
      previous[column] = current[column]
    }
  }
  return previous[right.length]
}

function parseQuery(query: string): readonly QueryTerm[] {
  const result: QueryTerm[] = []
  const pattern = /(-)?(?:(\w+):)?(?:"([^"]+)"|(\S+))/g
  let match: RegExpExecArray | null
  while ((match = pattern.exec(query))) {
    const value = normalizedText(match[3] ?? match[4] ?? "")
    if (!value) continue
    result.push(
      Object.freeze({
        field: match[2] ? normalizedText(match[2]) : undefined,
        value,
        negative: Boolean(match[1]),
        quoted: match[3] !== undefined,
      }),
    )
  }
  return freeze(result)
}

function document(
  id: string,
  kind: GraphEntityKind,
  primary: string,
  secondary: string,
  sequence: number,
  fields: Record<string, unknown>,
): SearchDocument {
  const normalizedFields: Record<string, string> = {}
  for (const [key, value] of Object.entries(fields)) {
    normalizedFields[normalizedText(key)] = normalizedText(
      Array.isArray(value) ? value.join(" ") : value,
    )
  }
  const normalized = normalizedText(
    [id, kind, primary, secondary, ...Object.values(normalizedFields)].join(" "),
  )
  return Object.freeze({
    id,
    kind,
    primary,
    secondary,
    fields: Object.freeze(normalizedFields),
    normalized,
    tokens: tokenize(normalized),
    sequence,
  })
}

function documentsForModel(model: TopologyGraphModel): readonly SearchDocument[] {
  const result: SearchDocument[] = []
  for (const node of model.nodes) {
    result.push(
      document(node.id, "node", node.title, node.subtitle, node.sequence, {
        id: node.id,
        state: node.state,
        role: node.role,
        namespace: node.namespace,
        location: node.location,
        provider: node.providerId,
        model: node.modelId,
        backend: node.backendId,
        privacy: node.privacyClass,
        capability: node.capabilities,
        worker: node.workerIds,
        route: node.routeIds,
        artifact: node.artifactIds,
      }),
    )
  }
  for (const edge of model.edges) {
    result.push(
      document(
        edge.id,
        "edge",
        `${edge.sourceId} → ${edge.targetId}`,
        edge.relation,
        edge.sequence,
        {
          id: edge.id,
          kind: edge.kind,
          source: edge.sourceId,
          target: edge.targetId,
          route: edge.routeIds,
          state: edge.missing ? "missing" : edge.pending ? "pending" : "committed",
        },
      ),
    )
  }
  for (const route of model.routes) {
    result.push(
      document(route.id, "route", `Route ${route.id}`, route.reasons.join(" · "), route.sequence, {
        node: route.nodeId,
        worker: route.selectedWorkerId,
        location: route.location,
        provider: route.providerId,
        model: route.modelId,
        backend: route.backendId,
        state: route.state,
        policy: route.policyId,
      }),
    )
  }
  for (const placement of model.placements) {
    result.push(
      document(
        placement.id,
        "placement",
        `Placement ${placement.id}`,
        `${placement.location} ${placement.privacyClass}`,
        placement.sequence,
        {
          node: placement.nodeId,
          route: placement.routeId,
          worker: placement.workerId,
          location: placement.location,
          provider: placement.providerId,
          model: placement.modelId,
          backend: placement.backendId,
          privacy: placement.privacyClass,
          violation: placement.violations,
        },
      ),
    )
  }
  for (const checkpoint of model.checkpoints) {
    result.push(
      document(
        checkpoint.id,
        "checkpoint",
        `Checkpoint ${checkpoint.id}`,
        checkpoint.phase,
        checkpoint.sequence,
        {
          parent: checkpoint.parentId,
          ancestry: checkpoint.ancestry,
          state: checkpoint.state,
          phase: checkpoint.phase,
          pending: checkpoint.pendingWrites,
          committed: checkpoint.committedWrites,
          graph: checkpoint.graphRevision,
          revision: checkpoint.commitRevision,
        },
      ),
    )
  }
  for (const branch of model.branches) {
    result.push(
      document(branch.id, "branch", `Branch ${branch.id}`, branch.strategy, branch.sequence, {
        checkpoint: branch.checkpointId,
        state: branch.state,
        outcome: branch.outcome,
        strategy: branch.strategy,
        conflict: branch.conflictCount,
        revision: branch.commitRevision,
      }),
    )
  }
  for (const change of model.changes) {
    result.push(
      document(
        change.id,
        "change",
        `Requirement change ${change.id}`,
        change.text,
        change.sequence,
        {
          state: change.state,
          affected: change.affectedNodeIds,
          superseded: change.supersededNodeIds,
          revise: change.needsRevisionNodeIds,
          replan: change.replanNodeId,
          route: change.routeIds,
        },
      ),
    )
  }
  return freeze(result)
}

function termScore(document: SearchDocument, term: QueryTerm): {
  matched: boolean
  score: number
  field?: string
} {
  const haystack = term.field ? document.fields[term.field] ?? "" : document.normalized
  if (!haystack) return { matched: false, score: 0 }
  const index = haystack.indexOf(term.value)
  if (index >= 0) {
    let score = term.quoted ? 180 : 120
    if (index === 0) score += 40
    if (haystack === term.value) score += 80
    if (term.field) score += 50
    return { matched: true, score, field: term.field }
  }
  if (term.quoted || term.value.length < 3) return { matched: false, score: 0 }
  const queryTokens = tokenize(term.value)
  let score = 0
  for (const queryToken of queryTokens) {
    let best = 0
    for (const candidate of document.tokens) {
      if (candidate.startsWith(queryToken)) {
        best = Math.max(best, 80 - Math.abs(candidate.length - queryToken.length))
        continue
      }
      const maximum = queryToken.length >= 8 ? 2 : 1
      const distance = editDistance(queryToken, candidate, maximum)
      if (distance <= maximum) best = Math.max(best, 45 - distance * 12)
    }
    if (best === 0) return { matched: false, score: 0 }
    score += best
  }
  return { matched: score > 0, score, field: term.field }
}

function matchRanges(primary: string, terms: readonly QueryTerm[]): readonly {
  start: number
  end: number
}[] {
  const normalized = normalizedText(primary)
  const result: { start: number; end: number }[] = []
  for (const term of terms.filter((value) => !value.negative)) {
    const start = normalized.indexOf(term.value)
    if (start >= 0) result.push({ start, end: start + term.value.length })
  }
  result.sort((left, right) => left.start - right.start || left.end - right.end)
  const merged: { start: number; end: number }[] = []
  for (const value of result) {
    const previous = merged.at(-1)
    if (previous && value.start <= previous.end) previous.end = Math.max(previous.end, value.end)
    else merged.push({ ...value })
  }
  return freeze(merged.map((value) => Object.freeze(value)))
}

export class TopologySearchIndex {
  #documents = new Map<string, SearchDocument>()
  #tokens = new Map<string, Set<string>>()
  #revision = -1

  get revision(): number {
    return this.#revision
  }

  rebuild(model: TopologyGraphModel): void {
    if (this.#revision === model.projectionRevision) return
    this.#documents.clear()
    this.#tokens.clear()
    for (const entry of documentsForModel(model)) {
      const key = `${entry.kind}:${entry.id}`
      this.#documents.set(key, entry)
      for (const token of entry.tokens) {
        const values = this.#tokens.get(token) ?? new Set<string>()
        values.add(key)
        this.#tokens.set(token, values)
      }
    }
    this.#revision = model.projectionRevision
  }

  search(query: string, maximum = 100): readonly SearchMatch[] {
    const terms = parseQuery(query)
    if (terms.length === 0) return freeze([])
    const positive = terms.filter((term) => !term.negative)
    const candidates = this.#candidateKeys(positive)
    const result: SearchMatch[] = []
    for (const key of candidates) {
      const entry = this.#documents.get(key)
      if (!entry) continue
      let score = 0
      const matchedFields = new Set<string>()
      let accepted = true
      for (const term of terms) {
        const match = termScore(entry, term)
        if (term.negative) {
          if (match.matched) {
            accepted = false
            break
          }
          continue
        }
        if (!match.matched) {
          accepted = false
          break
        }
        score += match.score
        if (match.field) matchedFields.add(match.field)
      }
      if (!accepted) continue
      if (entry.kind === "node") score += 25
      score += Math.min(25, Math.log2(Math.max(1, entry.sequence + 1)))
      result.push(
        Object.freeze({
          entityId: entry.id,
          kind: entry.kind,
          score,
          primary: entry.primary,
          secondary: entry.secondary,
          matchedFields: freeze(matchedFields),
          ranges: matchRanges(entry.primary, terms),
        }),
      )
    }
    return freeze(
      result
        .sort(
          (left, right) =>
            right.score - left.score ||
            kindRank(left.kind) - kindRank(right.kind) ||
            left.entityId.localeCompare(right.entityId),
        )
        .slice(0, Math.max(1, Math.floor(maximum))),
    )
  }

  suggestions(prefix: string, maximum = 12): readonly string[] {
    const normalized = normalizedText(prefix)
    if (!normalized) return freeze([])
    return freeze(
      [...this.#tokens.keys()]
        .filter((token) => token.startsWith(normalized))
        .sort(
          (left, right) =>
            (this.#tokens.get(right)?.size ?? 0) - (this.#tokens.get(left)?.size ?? 0) ||
            left.localeCompare(right),
        )
        .slice(0, maximum),
    )
  }

  #candidateKeys(terms: readonly QueryTerm[]): ReadonlySet<string> {
    if (terms.length === 0) return new Set(this.#documents.keys())
    const exactSets: Set<string>[] = []
    for (const term of terms) {
      if (term.field || term.quoted) continue
      const direct = this.#tokens.get(term.value)
      if (direct) exactSets.push(direct)
      else {
        const prefixes = [...this.#tokens.entries()]
          .filter(([token]) => token.startsWith(term.value))
          .flatMap(([, values]) => [...values])
        if (prefixes.length > 0) exactSets.push(new Set(prefixes))
      }
    }
    if (exactSets.length === 0) return new Set(this.#documents.keys())
    exactSets.sort((left, right) => left.size - right.size)
    const result = new Set(exactSets[0])
    for (const values of exactSets.slice(1)) {
      for (const key of [...result]) {
        if (!values.has(key)) result.delete(key)
      }
    }
    return result
  }
}

function kindRank(kind: GraphEntityKind): number {
  if (kind === "node") return 0
  if (kind === "route") return 1
  if (kind === "placement") return 2
  if (kind === "checkpoint") return 3
  if (kind === "branch") return 4
  if (kind === "change") return 5
  if (kind === "edge") return 6
  return 7
}

export function normalizeTopologyFilters(
  value: Partial<TopologyFilterState> = {},
): TopologyFilterState {
  const unique = (items: readonly string[] | undefined) =>
    freeze([...new Set((items ?? []).map((item) => String(item).trim()).filter(Boolean))].sort())
  return Object.freeze({
    query: String(value.query ?? "").slice(0, 1_024),
    status:
      value.status &&
      ["all", "active", "waiting", "recovering", "failed", "terminal", "changed", "policy-risk"].includes(
        value.status,
      )
        ? value.status
        : "all",
    namespaces: unique(value.namespaces),
    roles: unique(value.roles),
    locations: unique(value.locations) as TopologyFilterState["locations"],
    providers: unique(value.providers),
    models: unique(value.models),
    privacyClasses: unique(value.privacyClasses),
    capabilities: unique(value.capabilities),
    changedOnly: value.changedOnly === true,
    openWorldOnly: value.openWorldOnly === true,
    policyViolationsOnly: value.policyViolationsOnly === true,
    includeRemoved: value.includeRemoved === true,
    includePending: value.includePending !== false,
    includeInferredEdges: value.includeInferredEdges !== false,
  })
}

function matchesStatus(node: GraphNodeRecord, filter: TopologyFilterState["status"]): boolean {
  if (filter === "all") return true
  if (filter === "active") return ["ready", "queued", "leased", "running"].includes(node.state)
  if (filter === "waiting") return ["blocked", "waiting", "interrupted"].includes(node.state)
  if (filter === "recovering") return ["resuming", "recovering", "needs_revision"].includes(node.state)
  if (filter === "failed") return ["failed", "rejected", "conflicted"].includes(node.state)
  if (filter === "terminal") return node.terminal
  if (filter === "policy-risk") return node.policyViolation
  return (
    node.openWorldCreated ||
    node.openWorldRemoved ||
    node.openWorldReplaced ||
    node.affectedByChangeIds.length > 0
  )
}

function includesAll(haystack: readonly string[], needles: readonly string[]): boolean {
  if (needles.length === 0) return true
  const values = new Set(haystack.map(normalizedText))
  return needles.every((needle) => values.has(normalizedText(needle)))
}

function nodeMatches(
  node: GraphNodeRecord,
  filters: TopologyFilterState,
  searchNodeIds: ReadonlySet<string> | undefined,
): boolean {
  if (!filters.includeRemoved && node.removed) return false
  if (!filters.includePending && node.pending) return false
  if (searchNodeIds && !searchNodeIds.has(node.id)) return false
  if (!matchesStatus(node, filters.status)) return false
  if (filters.namespaces.length > 0 && !filters.namespaces.includes(node.namespace)) return false
  if (filters.roles.length > 0 && !filters.roles.includes(node.role)) return false
  if (filters.locations.length > 0 && !filters.locations.includes(node.location)) return false
  if (filters.providers.length > 0 && !filters.providers.includes(node.providerId ?? "")) return false
  if (filters.models.length > 0 && !filters.models.includes(node.modelId ?? "")) return false
  if (
    filters.privacyClasses.length > 0 &&
    !filters.privacyClasses.includes(node.privacyClass)
  ) return false
  if (!includesAll(node.capabilities, filters.capabilities)) return false
  const changed =
    node.openWorldCreated ||
    node.openWorldRemoved ||
    node.openWorldReplaced ||
    node.affectedByChangeIds.length > 0
  if (filters.changedOnly && !changed) return false
  if (
    filters.openWorldOnly &&
    !node.openWorldCreated &&
    !node.openWorldRemoved &&
    !node.openWorldReplaced
  ) return false
  if (filters.policyViolationsOnly && !node.policyViolation) return false
  return true
}

export function filterTopologyModel(
  model: TopologyGraphModel,
  input: Partial<TopologyFilterState>,
  searchIndex = new TopologySearchIndex(),
): FilterResult {
  const filters = normalizeTopologyFilters(input)
  searchIndex.rebuild(model)
  const searchMatches = filters.query.trim()
    ? searchIndex.search(filters.query, 2_000)
    : freeze<SearchMatch>([])
  const searchNodeIds =
    searchMatches.length > 0
      ? new Set(
          searchMatches.flatMap((match) => {
            if (match.kind === "node") return [match.entityId]
            if (match.kind === "edge") {
              const edge = model.edgeById.get(match.entityId)
              return edge ? [edge.sourceId, edge.targetId] : []
            }
            if (match.kind === "route") {
              const nodeId = model.routeById.get(match.entityId)?.nodeId
              return nodeId ? [nodeId] : []
            }
            if (match.kind === "placement") {
              const nodeId = model.placementById.get(match.entityId)?.nodeId
              return nodeId ? [nodeId] : []
            }
            if (match.kind === "change") {
              return model.changes.find((value) => value.id === match.entityId)?.affectedNodeIds ?? []
            }
            return []
          }),
        )
      : filters.query.trim()
        ? new Set<string>()
        : undefined
  const nodeIds = model.nodes
    .filter((node) => nodeMatches(node, filters, searchNodeIds))
    .map((node) => node.id)
  const visibleNodes = new Set(nodeIds)
  let orphanedVisibleEdgeCount = 0
  const edgeIds = model.edges
    .filter((edge) => {
      if (!filters.includeRemoved && edge.removed) return false
      if (!filters.includePending && edge.pending) return false
      if (!filters.includeInferredEdges && edge.inferred) return false
      const sourceVisible = visibleNodes.has(edge.sourceId)
      const targetVisible = visibleNodes.has(edge.targetId)
      if (sourceVisible !== targetVisible) orphanedVisibleEdgeCount += 1
      return sourceVisible && targetVisible
    })
    .map((edge) => edge.id)
  const routeIds = model.routes
    .filter((route) => !route.nodeId || visibleNodes.has(route.nodeId))
    .map((route) => route.id)
  const routeSet = new Set(routeIds)
  const placementIds = model.placements
    .filter(
      (placement) =>
        (!placement.nodeId || visibleNodes.has(placement.nodeId)) &&
        (!placement.routeId || routeSet.has(placement.routeId)),
    )
    .map((placement) => placement.id)
  const checkpointIds = model.checkpoints
    .filter((checkpoint) => {
      if (filters.status === "failed") {
        return !checkpoint.lineageValid || !checkpoint.visibilityValid
      }
      if (filters.status === "waiting") return checkpoint.pendingWrites > 0
      return true
    })
    .map((checkpoint) => checkpoint.id)
  const branchIds = model.branches
    .filter((branch) => {
      if (filters.status === "failed") {
        return ["rejected", "conflicted", "replan_required"].includes(branch.outcome)
      }
      if (filters.status === "changed") return branch.outcome === "rebased"
      return true
    })
    .map((branch) => branch.id)
  const changeIds = model.changes
    .filter((change) =>
      change.affectedNodeIds.some((nodeId) => visibleNodes.has(nodeId)),
    )
    .map((change) => change.id)
  return Object.freeze({
    nodeIds: freeze(nodeIds),
    edgeIds: freeze(edgeIds),
    routeIds: freeze(routeIds),
    placementIds: freeze(placementIds),
    checkpointIds: freeze(checkpointIds),
    branchIds: freeze(branchIds),
    changeIds: freeze(changeIds),
    hiddenNodeCount: Math.max(0, model.nodes.length - nodeIds.length),
    orphanedVisibleEdgeCount,
    searchMatches,
  })
}

export function topologyFilterFacets(model: TopologyGraphModel): Readonly<{
  namespaces: readonly string[]
  roles: readonly string[]
  locations: readonly string[]
  providers: readonly string[]
  models: readonly string[]
  privacyClasses: readonly string[]
  capabilities: readonly string[]
}> {
  const values = (selector: (node: GraphNodeRecord) => Iterable<string | undefined>) =>
    freeze(
      [...new Set(model.nodes.flatMap((node) => [...selector(node)].filter(Boolean) as string[]))].sort(),
    )
  return Object.freeze({
    namespaces: values((node) => [node.namespace]),
    roles: values((node) => [node.role]),
    locations: values((node) => [node.location]),
    providers: values((node) => [node.providerId]),
    models: values((node) => [node.modelId]),
    privacyClasses: values((node) => [node.privacyClass]),
    capabilities: values((node) => node.capabilities),
  })
}
