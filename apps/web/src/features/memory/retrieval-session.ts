import type { MemoryRow, MemoryLayer, Veracity } from "./projection.ts"
import {
  bounded,
  compareNumber,
  compareText,
  fingerprint,
  mean,
  ratio,
  sortStable,
  sum,
  text,
  unique,
} from "../session/value.ts"

export interface RetrievalConstraint {
  query: string
  layers: readonly MemoryLayer[]
  namespaces: readonly string[]
  tags: readonly string[]
  minimumVeracity: Veracity
  minimumProvenance: number
  maximumTokens: number
  maximumItems: number
  includeStale: boolean
  includeRejected: boolean
  requireArtifactEvidence: boolean
  requireEventEvidence: boolean
  diversityWeight: number
  freshnessWeight: number
  relevanceWeight: number
  veracityWeight: number
  provenanceWeight: number
}

export interface RetrievalCandidate {
  memory: MemoryRow
  eligible: boolean
  reasons: readonly string[]
  relevance: number
  veracity: number
  provenance: number
  freshness: number
  diversity: number
  utility: number
  tokenCost: number
  marginalUtility: number
  selected: boolean
  selectionOrder?: number
}

export interface RetrievalPlan {
  id: string
  constraint: RetrievalConstraint
  candidates: readonly RetrievalCandidate[]
  selected: readonly RetrievalCandidate[]
  rejected: readonly RetrievalCandidate[]
  tokenCount: number
  tokenBudget: number
  tokenUtilization: number
  layerCounts: Readonly<Record<MemoryLayer, number>>
  namespaceCounts: Readonly<Record<string, number>>
  evidenceEventIds: readonly string[]
  evidenceArtifactIds: readonly string[]
  warnings: readonly string[]
  contextFingerprint: string
}

export interface CuratorCandidate {
  id: string
  memoryIds: readonly string[]
  action: "retain" | "merge" | "promote" | "archive" | "reject" | "verify"
  layer: MemoryLayer
  score: number
  deterministicReasons: readonly string[]
  requiresReview: boolean
  evidenceEventIds: readonly string[]
  evidenceArtifactIds: readonly string[]
  proposedTitle?: string
  proposedSummary?: string
}

export interface CuratorBatch {
  id: string
  candidates: readonly CuratorCandidate[]
  retainCount: number
  mergeCount: number
  promoteCount: number
  archiveCount: number
  rejectCount: number
  verifyCount: number
  automaticCount: number
  reviewCount: number
  sourceMemoryIds: readonly string[]
  warnings: readonly string[]
}

export class MemoryRetrievalSession {
  readonly #rows = new Map<string, MemoryRow>()
  readonly #plans = new Map<string, RetrievalPlan>()
  #enabled = true
  #disabledReason = "Memory retrieval session is disabled."
  #revision = 0

  replace(rows: readonly MemoryRow[]): void {
    this.#assertEnabled()
    this.#rows.clear()
    for (const row of rows) this.#rows.set(row.id, row)
    this.#revision += 1
  }

  upsert(row: MemoryRow): void {
    this.#assertEnabled()
    const existing = this.#rows.get(row.id)
    if (
      existing &&
      existing.revision === row.revision &&
      existing.combinedScore === row.combinedScore
    ) return
    this.#rows.set(row.id, row)
    this.#revision += 1
  }

  remove(memoryId: string): boolean {
    this.#assertEnabled()
    const removed = this.#rows.delete(memoryId)
    if (removed) this.#revision += 1
    return removed
  }

  plan(input: Partial<RetrievalConstraint>): RetrievalPlan {
    this.#assertEnabled()
    const constraint = normalizeConstraint(input)
    const candidates = [...this.#rows.values()].map((memory) =>
      evaluateCandidate(memory, constraint))
    const eligible = candidates.filter((candidate) => candidate.eligible)
    const selected = selectCandidates(eligible, constraint)
    const selectedIds = new Map(selected.map((candidate, index) => [
      candidate.memory.id,
      index + 1,
    ]))
    const settled = candidates.map((candidate) => Object.freeze({
      ...candidate,
      selected: selectedIds.has(candidate.memory.id),
      selectionOrder: selectedIds.get(candidate.memory.id),
    }))
    const ordered = sortStable(settled, (left, right) =>
      Number(right.selected) - Number(left.selected) ||
      compareNumber(left.selectionOrder, right.selectionOrder) ||
      compareNumber(right.utility, left.utility) ||
      compareText(left.memory.id, right.memory.id))
    const selectedRows = ordered.filter((candidate) => candidate.selected)
    const rejectedRows = ordered.filter((candidate) => !candidate.selected)
    const warnings = retrievalWarnings(selectedRows, rejectedRows, constraint)
    const layerCounts = countLayers(selectedRows)
    const namespaceCounts = countNamespaces(selectedRows)
    const eventIds = unique(selectedRows.flatMap((candidate) =>
      candidate.memory.sourceEventIds))
    const artifactIds = unique(selectedRows.flatMap((candidate) =>
      candidate.memory.sourceArtifactIds))
    const tokenCount = sum(selectedRows.map((candidate) => candidate.tokenCost))
    const contextFingerprint = fingerprint([
      this.#revision,
      constraint,
      selectedRows.map((candidate) => [
        candidate.memory.id,
        candidate.memory.revision,
        candidate.tokenCost,
        candidate.utility,
      ]),
    ])
    const plan: RetrievalPlan = Object.freeze({
      id: fingerprint([constraint, contextFingerprint]),
      constraint,
      candidates: Object.freeze(ordered),
      selected: Object.freeze(selectedRows),
      rejected: Object.freeze(rejectedRows),
      tokenCount,
      tokenBudget: constraint.maximumTokens,
      tokenUtilization: ratio(tokenCount, constraint.maximumTokens),
      layerCounts: Object.freeze(layerCounts),
      namespaceCounts: Object.freeze(namespaceCounts),
      evidenceEventIds: Object.freeze(eventIds),
      evidenceArtifactIds: Object.freeze(artifactIds),
      warnings: Object.freeze(warnings),
      contextFingerprint,
    })
    this.#plans.set(plan.id, plan)
    return plan
  }

  get(planId: string): RetrievalPlan | undefined {
    this.#assertEnabled()
    return this.#plans.get(planId)
  }

  listPlans(): readonly RetrievalPlan[] {
    this.#assertEnabled()
    return Object.freeze([...this.#plans.values()])
  }

  latest(): RetrievalPlan | undefined {
    this.#assertEnabled()
    return [...this.#plans.values()].at(-1)
  }

  compare(leftId: string, rightId: string): Readonly<Record<string, unknown>> {
    this.#assertEnabled()
    const left = this.#plans.get(leftId)
    const right = this.#plans.get(rightId)
    if (!left || !right) throw new Error("Both retrieval plans must exist.")
    const leftIds = new Set(left.selected.map((candidate) => candidate.memory.id))
    const rightIds = new Set(right.selected.map((candidate) => candidate.memory.id))
    return Object.freeze({
      leftId,
      rightId,
      addedMemoryIds: Object.freeze([...rightIds].filter((id) => !leftIds.has(id))),
      removedMemoryIds: Object.freeze([...leftIds].filter((id) => !rightIds.has(id))),
      retainedMemoryIds: Object.freeze([...rightIds].filter((id) => leftIds.has(id))),
      tokenDelta: right.tokenCount - left.tokenCount,
      utilizationDelta: right.tokenUtilization - left.tokenUtilization,
      evidenceEventDelta: right.evidenceEventIds.length - left.evidenceEventIds.length,
      evidenceArtifactDelta: right.evidenceArtifactIds.length - left.evidenceArtifactIds.length,
      contextChanged: left.contextFingerprint !== right.contextFingerprint,
    })
  }

  buildContextEnvelope(planId: string): Readonly<Record<string, unknown>> {
    this.#assertEnabled()
    const plan = this.#plans.get(planId)
    if (!plan) throw new Error("Retrieval plan does not exist.")
    return Object.freeze({
      schema: "zyra.memory-context-selection/v1",
      plan_id: plan.id,
      context_fingerprint: plan.contextFingerprint,
      query: plan.constraint.query,
      token_count: plan.tokenCount,
      token_budget: plan.tokenBudget,
      memory_ids: Object.freeze(plan.selected.map((candidate) => candidate.memory.id)),
      layers: Object.freeze(plan.layerCounts),
      namespaces: Object.freeze(plan.namespaceCounts),
      evidence_event_ids: plan.evidenceEventIds,
      evidence_artifact_ids: plan.evidenceArtifactIds,
      selections: Object.freeze(plan.selected.map((candidate) => Object.freeze({
        memory_id: candidate.memory.id,
        layer: candidate.memory.layer,
        namespace: candidate.memory.namespace,
        token_count: candidate.tokenCost,
        utility: candidate.utility,
        veracity: candidate.memory.veracity,
        provenance_score: candidate.memory.provenanceScore,
        source_event_ids: candidate.memory.sourceEventIds,
        source_artifact_ids: candidate.memory.sourceArtifactIds,
      }))),
    })
  }

  disable(reason = "Memory retrieval session is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Memory retrieval session is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  clear(): void {
    this.#assertEnabled()
    this.#rows.clear()
    this.#plans.clear()
    this.#revision += 1
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

export class MemoryCuratorPlanner {
  #enabled = true
  #disabledReason = "Memory curator planner is disabled."

  plan(rows: readonly MemoryRow[]): CuratorBatch {
    this.#assertEnabled()
    const candidates: CuratorCandidate[] = []
    const clustered = clusterSimilarRows(rows)
    const clusteredIds = new Set<string>()
    for (const cluster of clustered) {
      for (const row of cluster) clusteredIds.add(row.id)
      candidates.push(mergeCandidate(cluster))
    }
    for (const row of rows) {
      if (clusteredIds.has(row.id)) continue
      candidates.push(singleCandidate(row))
    }
    const ordered = sortStable(candidates, (left, right) =>
      curatorActionRank(left.action) - curatorActionRank(right.action) ||
      compareNumber(right.score, left.score) ||
      compareText(left.id, right.id))
    const warnings: string[] = []
    if (ordered.some((candidate) => candidate.action === "reject" && !candidate.evidenceEventIds.length)) {
      warnings.push("rejection candidate lacks event evidence")
    }
    if (ordered.some((candidate) => candidate.action === "merge" && candidate.memoryIds.length < 2)) {
      warnings.push("merge candidate contains fewer than two memories")
    }
    if (ordered.some((candidate) => candidate.requiresReview && !candidate.deterministicReasons.length)) {
      warnings.push("review candidate lacks deterministic explanation")
    }
    const sourceMemoryIds = unique(ordered.flatMap((candidate) => candidate.memoryIds))
    return Object.freeze({
      id: fingerprint([
        rows.map((row) => [row.id, row.revision, row.veracity, row.status]),
        ordered.map((candidate) => [candidate.id, candidate.action, candidate.score]),
      ]),
      candidates: Object.freeze(ordered),
      retainCount: ordered.filter((candidate) => candidate.action === "retain").length,
      mergeCount: ordered.filter((candidate) => candidate.action === "merge").length,
      promoteCount: ordered.filter((candidate) => candidate.action === "promote").length,
      archiveCount: ordered.filter((candidate) => candidate.action === "archive").length,
      rejectCount: ordered.filter((candidate) => candidate.action === "reject").length,
      verifyCount: ordered.filter((candidate) => candidate.action === "verify").length,
      automaticCount: ordered.filter((candidate) => !candidate.requiresReview).length,
      reviewCount: ordered.filter((candidate) => candidate.requiresReview).length,
      sourceMemoryIds: Object.freeze(sourceMemoryIds),
      warnings: Object.freeze(warnings),
    })
  }

  apply(
    batch: CuratorBatch,
    acceptedIds: readonly string[],
    rejectedIds: readonly string[],
  ): Readonly<Record<string, unknown>> {
    this.#assertEnabled()
    const accepted = new Set(acceptedIds)
    const rejected = new Set(rejectedIds)
    const overlap = [...accepted].filter((id) => rejected.has(id))
    if (overlap.length) throw new Error("Curator candidate cannot be accepted and rejected.")
    const known = new Set(batch.candidates.map((candidate) => candidate.id))
    const unknown = [...accepted, ...rejected].filter((id) => !known.has(id))
    if (unknown.length) throw new Error(`Unknown curator candidate: ${unknown[0]}`)
    const decisions = batch.candidates.map((candidate) => Object.freeze({
      candidate_id: candidate.id,
      action: candidate.action,
      memory_ids: candidate.memoryIds,
      decision: accepted.has(candidate.id)
        ? "accept"
        : rejected.has(candidate.id)
          ? "reject"
          : candidate.requiresReview
            ? "pending"
            : "automatic",
      deterministic_reasons: candidate.deterministicReasons,
      evidence_event_ids: candidate.evidenceEventIds,
      evidence_artifact_ids: candidate.evidenceArtifactIds,
    }))
    return Object.freeze({
      schema: "zyra.memory-curator-decision/v1",
      batch_id: batch.id,
      decisions: Object.freeze(decisions),
      accepted_count: decisions.filter((decision) =>
        decision.decision === "accept" || decision.decision === "automatic").length,
      rejected_count: decisions.filter((decision) => decision.decision === "reject").length,
      pending_count: decisions.filter((decision) => decision.decision === "pending").length,
      source_memory_ids: batch.sourceMemoryIds,
    })
  }

  disable(reason = "Memory curator planner is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Memory curator planner is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

function normalizeConstraint(input: Partial<RetrievalConstraint>): RetrievalConstraint {
  const layers = unique((input.layers ?? []).filter((layer): layer is MemoryLayer =>
    ["working", "episodic", "semantic", "skill"].includes(layer)))
  const weights = [
    bounded(input.diversityWeight ?? 0.1, 0, 1),
    bounded(input.freshnessWeight ?? 0.1, 0, 1),
    bounded(input.relevanceWeight ?? 0.3, 0, 1),
    bounded(input.veracityWeight ?? 0.25, 0, 1),
    bounded(input.provenanceWeight ?? 0.25, 0, 1),
  ]
  const weightTotal = sum(weights) || 1
  return Object.freeze({
    query: text(input.query),
    layers: Object.freeze(layers),
    namespaces: Object.freeze(unique((input.namespaces ?? []).map((item) => text(item)).filter(Boolean))),
    tags: Object.freeze(unique((input.tags ?? []).map((item) => text(item)).filter(Boolean))),
    minimumVeracity: input.minimumVeracity ?? "uncertain",
    minimumProvenance: bounded(input.minimumProvenance ?? 0, 0, 1),
    maximumTokens: bounded(Math.floor(input.maximumTokens ?? 8000), 1, 1000000),
    maximumItems: bounded(Math.floor(input.maximumItems ?? 50), 1, 500),
    includeStale: input.includeStale === true,
    includeRejected: input.includeRejected === true,
    requireArtifactEvidence: input.requireArtifactEvidence === true,
    requireEventEvidence: input.requireEventEvidence === true,
    diversityWeight: weights[0]! / weightTotal,
    freshnessWeight: weights[1]! / weightTotal,
    relevanceWeight: weights[2]! / weightTotal,
    veracityWeight: weights[3]! / weightTotal,
    provenanceWeight: weights[4]! / weightTotal,
  })
}

function evaluateCandidate(
  memory: MemoryRow,
  constraint: RetrievalConstraint,
): RetrievalCandidate {
  const reasons: string[] = []
  if (constraint.layers.length && !constraint.layers.includes(memory.layer)) reasons.push("layer excluded")
  if (constraint.namespaces.length && !constraint.namespaces.includes(memory.namespace)) reasons.push("namespace excluded")
  if (constraint.tags.length && !constraint.tags.every((tag) => memory.tags.includes(tag))) reasons.push("required tag absent")
  if (!constraint.includeStale && (memory.status === "stale" || memory.staleReason)) reasons.push("memory stale")
  if (!constraint.includeRejected && memory.veracity === "rejected") reasons.push("memory rejected")
  if (veracityRank(memory.veracity) < veracityRank(constraint.minimumVeracity)) reasons.push("veracity below threshold")
  if (memory.provenanceScore < constraint.minimumProvenance) reasons.push("provenance below threshold")
  if (constraint.requireArtifactEvidence && !memory.sourceArtifactIds.length) reasons.push("artifact evidence required")
  if (constraint.requireEventEvidence && !memory.sourceEventIds.length) reasons.push("event evidence required")
  const relevance = bounded(memory.searchScore / 60, 0, 1)
  const veracity = memory.veracityScore
  const provenance = memory.provenanceScore
  const freshness = memory.freshness
  const diversity = diversityPotential(memory)
  const utility = bounded(
    relevance * constraint.relevanceWeight +
    veracity * constraint.veracityWeight +
    provenance * constraint.provenanceWeight +
    freshness * constraint.freshnessWeight +
    diversity * constraint.diversityWeight,
    0,
    1,
  )
  const tokenCost = Math.max(1, memory.tokenCount)
  return Object.freeze({
    memory,
    eligible: reasons.length === 0,
    reasons: Object.freeze(reasons),
    relevance,
    veracity,
    provenance,
    freshness,
    diversity,
    utility,
    tokenCost,
    marginalUtility: utility / Math.sqrt(tokenCost),
    selected: false,
  })
}

function selectCandidates(
  candidates: readonly RetrievalCandidate[],
  constraint: RetrievalConstraint,
): RetrievalCandidate[] {
  const selected: RetrievalCandidate[] = []
  const remaining = [...candidates]
  let tokens = 0
  while (remaining.length && selected.length < constraint.maximumItems) {
    const namespaces = new Set(selected.map((candidate) => candidate.memory.namespace))
    const layers = new Set(selected.map((candidate) => candidate.memory.layer))
    const ranked = remaining
      .map((candidate) => ({
        candidate,
        score:
          candidate.marginalUtility +
          (namespaces.has(candidate.memory.namespace) ? 0 : constraint.diversityWeight * 0.2) +
          (layers.has(candidate.memory.layer) ? 0 : constraint.diversityWeight * 0.2),
      }))
      .sort((left, right) =>
        right.score - left.score ||
        left.candidate.memory.id.localeCompare(right.candidate.memory.id))
    const next = ranked[0]?.candidate
    if (!next) break
    remaining.splice(remaining.findIndex((candidate) => candidate.memory.id === next.memory.id), 1)
    if (tokens + next.tokenCost > constraint.maximumTokens) continue
    selected.push(next)
    tokens += next.tokenCost
  }
  return selected
}

function retrievalWarnings(
  selected: readonly RetrievalCandidate[],
  rejected: readonly RetrievalCandidate[],
  constraint: RetrievalConstraint,
): string[] {
  const warnings: string[] = []
  if (!selected.length) warnings.push("retrieval selected no memory")
  if (sum(selected.map((candidate) => candidate.tokenCost)) >= constraint.maximumTokens * 0.95) {
    warnings.push("retrieval nearly exhausts token budget")
  }
  if (!selected.some((candidate) => candidate.memory.layer === "working")) {
    warnings.push("retrieval has no working memory")
  }
  if (!selected.some((candidate) => candidate.memory.layer === "episodic")) {
    warnings.push("retrieval has no episodic memory")
  }
  if (selected.some((candidate) => candidate.memory.veracity === "contested")) {
    warnings.push("retrieval includes contested memory")
  }
  if (selected.some((candidate) => !candidate.memory.sourceEventIds.length)) {
    warnings.push("retrieval includes memory without event provenance")
  }
  if (rejected.some((candidate) => candidate.reasons.includes("veracity below threshold"))) {
    warnings.push("low-veracity memory was excluded")
  }
  return warnings
}

function countLayers(
  candidates: readonly RetrievalCandidate[],
): Record<MemoryLayer, number> {
  const result: Record<MemoryLayer, number> = {
    working: 0,
    episodic: 0,
    semantic: 0,
    skill: 0,
  }
  for (const candidate of candidates) result[candidate.memory.layer] += 1
  return result
}

function countNamespaces(
  candidates: readonly RetrievalCandidate[],
): Record<string, number> {
  const result: Record<string, number> = {}
  for (const candidate of candidates) {
    result[candidate.memory.namespace] = (result[candidate.memory.namespace] ?? 0) + 1
  }
  return result
}

function diversityPotential(memory: MemoryRow): number {
  const evidenceKinds = new Set(memory.evidence.map((entry) => entry.kind)).size
  const sources = memory.sourceArtifactIds.length + memory.sourceEventIds.length + memory.sourceMemoryIds.length
  const tags = memory.tags.length
  return bounded(evidenceKinds * 0.2 + Math.min(0.4, sources * 0.05) + Math.min(0.2, tags * 0.04), 0, 1)
}

function clusterSimilarRows(rows: readonly MemoryRow[]): MemoryRow[][] {
  const clusters: MemoryRow[][] = []
  const assigned = new Set<string>()
  for (const row of rows) {
    if (assigned.has(row.id)) continue
    const related = rows.filter((candidate) => {
      if (candidate.id === row.id || assigned.has(candidate.id)) return false
      if (candidate.layer !== row.layer || candidate.namespace !== row.namespace) return false
      const sharedTags = candidate.tags.filter((tag) => row.tags.includes(tag)).length
      const sharedEvents = candidate.sourceEventIds.filter((id) => row.sourceEventIds.includes(id)).length
      const sharedArtifacts = candidate.sourceArtifactIds.filter((id) => row.sourceArtifactIds.includes(id)).length
      const sameKey = candidate.key === row.key
      return sameKey || sharedTags >= 2 || sharedEvents > 0 || sharedArtifacts > 0
    })
    if (!related.length) continue
    const cluster = [row, ...related]
    for (const entry of cluster) assigned.add(entry.id)
    clusters.push(cluster)
  }
  return clusters
}

function mergeCandidate(rows: readonly MemoryRow[]): CuratorCandidate {
  const ordered = [...rows].sort((left, right) =>
    right.veracityScore - left.veracityScore ||
    right.provenanceScore - left.provenanceScore ||
    right.sequence - left.sequence)
  const leader = ordered[0]!
  const reasons = [
    `${rows.length} memories share layer and namespace`,
    `leader ${leader.id} has the strongest evidence`,
  ]
  if (new Set(rows.map((row) => row.key)).size === 1) reasons.push("memory keys are identical")
  if (rows.some((row) => row.veracity === "contested")) reasons.push("cluster contains contested memory")
  return Object.freeze({
    id: fingerprint(["merge", rows.map((row) => row.id).sort()]),
    memoryIds: Object.freeze(rows.map((row) => row.id)),
    action: "merge",
    layer: leader.layer,
    score: mean(rows.map((row) => row.combinedScore)),
    deterministicReasons: Object.freeze(reasons),
    requiresReview: rows.some((row) => row.veracity === "contested" || row.veracity === "uncertain"),
    evidenceEventIds: Object.freeze(unique(rows.flatMap((row) => row.sourceEventIds))),
    evidenceArtifactIds: Object.freeze(unique(rows.flatMap((row) => row.sourceArtifactIds))),
    proposedTitle: leader.title,
    proposedSummary: leader.summary,
  })
}

function singleCandidate(row: MemoryRow): CuratorCandidate {
  const reasons: string[] = []
  let action: CuratorCandidate["action"] = "retain"
  let requiresReview = false
  if (row.veracity === "rejected") {
    action = "reject"
    reasons.push("memory is rejected by veracity policy")
  } else if (row.veracity === "contested" || row.veracity === "uncertain") {
    action = "verify"
    requiresReview = true
    reasons.push("memory requires evidence review")
  } else if (row.status === "stale" || row.staleReason) {
    action = "archive"
    reasons.push("memory is stale")
  } else if (row.layer === "working" && row.veracity === "verified" && row.provenanceScore >= 0.8) {
    action = "promote"
    reasons.push("verified working memory is eligible for promotion")
  } else {
    reasons.push("memory remains useful in its current layer")
  }
  if (!row.sourceEventIds.length) {
    reasons.push("event provenance is absent")
    requiresReview = true
  }
  return Object.freeze({
    id: fingerprint([action, row.id, row.revision]),
    memoryIds: Object.freeze([row.id]),
    action,
    layer: row.layer,
    score: row.combinedScore,
    deterministicReasons: Object.freeze(reasons),
    requiresReview,
    evidenceEventIds: Object.freeze([...row.sourceEventIds]),
    evidenceArtifactIds: Object.freeze([...row.sourceArtifactIds]),
    proposedTitle: row.title,
    proposedSummary: row.summary,
  })
}

function veracityRank(veracity: Veracity): number {
  return ["rejected", "contested", "uncertain", "supported", "verified"].indexOf(veracity)
}

function curatorActionRank(action: CuratorCandidate["action"]): number {
  return ["verify", "reject", "merge", "promote", "archive", "retain"].indexOf(action)
}
