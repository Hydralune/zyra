import type {
  CanonicalProjectionState,
  CausalEventProjection,
  MemoryProjection,
  ProjectionSelector,
} from "../../state/contracts.ts"
import { projectionSelector } from "../../state/selectors.ts"
import {
  bounded,
  compareNumber,
  compareText,
  date,
  decimalFrom,
  elapsed,
  fingerprint,
  integerFrom,
  listFrom,
  mean,
  mergeRecords,
  ratio,
  record,
  searchScore,
  sortStable,
  sum,
  text,
  textFrom,
  truthFrom,
  unique,
  values,
} from "../session/value.ts"

export type MemoryLayer = "working" | "episodic" | "semantic" | "skill"
export type Veracity = "verified" | "supported" | "uncertain" | "contested" | "rejected"
export type CuratorPhase = "scheduled" | "candidate" | "accepted" | "committed" | "published" | "rejected" | "recovered" | "failed"

export interface MemoryEvidence {
  id: string
  kind: "event" | "artifact" | "tool" | "memory"
  summary: string
  sequence: number
  effective: boolean
  correlationId?: string
  provenanceScore: number
}

export interface MemoryRow {
  id: string
  taskId: string
  runId: string
  sessionId?: string
  layer: MemoryLayer
  namespace: string
  key: string
  title: string
  summary: string
  score: number
  searchScore: number
  combinedScore: number
  tokenCount: number
  revision: number
  sequence: number
  lifecycle: string
  status: string
  createdAt: string
  updatedAt: string
  ageMs: number
  freshness: number
  veracity: Veracity
  veracityScore: number
  provenanceScore: number
  sourceArtifactIds: readonly string[]
  sourceEventIds: readonly string[]
  sourceMemoryIds: readonly string[]
  evidence: readonly MemoryEvidence[]
  tags: readonly string[]
  skillName?: string
  procedureId?: string
  compactEpoch?: number
  staleReason?: string
  rejectedReason?: string
  retained: boolean
}

export interface MemoryLayerSummary {
  layer: MemoryLayer
  count: number
  tokens: number
  verified: number
  uncertain: number
  stale: number
  averageScore: number
  namespaces: readonly string[]
}

export interface CuratorDecision {
  id: string
  runId: string
  candidateId?: string
  memoryId?: string
  phase: CuratorPhase
  reason: string
  deterministic: boolean
  modelId?: string
  providerId?: string
  sourceEventId: string
  sequence: number
  createdAt: string
  correlationId: string
  artifactIds: readonly string[]
  previousPhase?: CuratorPhase
  transitionValid: boolean
}

export interface CuratorRun {
  id: string
  taskId: string
  phases: readonly CuratorDecision[]
  startedAt: string
  finishedAt?: string
  active: boolean
  failed: boolean
  candidateCount: number
  acceptedCount: number
  rejectedCount: number
  committedCount: number
  publishedCount: number
  recoveredCount: number
  durationMs: number
  modelId?: string
  providerId?: string
  reportArtifactIds: readonly string[]
  warnings: readonly string[]
}

export interface MemoryQuery {
  text?: string
  layers?: readonly MemoryLayer[]
  namespaces?: readonly string[]
  veracity?: readonly Veracity[]
  tags?: readonly string[]
  includeStale?: boolean
  minimumScore?: number
  limit?: number
}

export interface MemoryConsoleProjection {
  taskId: string
  query: Readonly<MemoryQuery>
  rows: readonly MemoryRow[]
  totalRows: number
  layerSummaries: readonly MemoryLayerSummary[]
  curatorRuns: readonly CuratorRun[]
  latestCuratorRun?: CuratorRun
  selectedMemoryIds: readonly string[]
  retrievalEventIds: readonly string[]
  compactMemoryIds: readonly string[]
  skillMemoryIds: readonly string[]
  provenanceWarnings: readonly string[]
  connected: boolean
  committedSequence: number
  revision: number
  fingerprint: string
}

export function selectMemoryConsole(
  taskId: string,
  query: MemoryQuery = {},
): ProjectionSelector<MemoryConsoleProjection> {
  const normalized = normalizeQuery(query)
  return projectionSelector(
    `memory-console:${taskId}:${fingerprint([normalized])}`,
    [
      `task:${taskId}`,
      "domain:memory",
      "domain:artifact",
      "domain:tool",
      "domain:event",
      "domain:session",
      "domain:command",
    ],
    (state) => buildMemoryConsoleProjection(state, taskId, normalized),
    sameMemoryConsole,
  )
}

export function buildMemoryConsoleProjection(
  state: CanonicalProjectionState,
  taskId: string,
  query: MemoryQuery = {},
): MemoryConsoleProjection {
  const normalized = normalizeQuery(query)
  const taskMemory = Object.values(state.memories)
    .filter((memory) => memory.taskId === taskId)
  const events = (state.causality.byTask[taskId] ?? [])
    .map((eventId) => state.causality.byEvent[eventId])
    .filter((event): event is CausalEventProjection => Boolean(event))
    .sort((left, right) => left.sequence - right.sequence || left.eventId.localeCompare(right.eventId))
  const allRows = taskMemory.map((memory) => buildMemoryRow(state, memory, events, normalized.text ?? ""))
  const filtered = allRows
    .filter((row) => filterMemory(row, normalized))
  const ranked = sortStable(filtered, compareMemoryRank)
  const rows = ranked.slice(0, normalized.limit)
  const layerSummaries = layerSummary(allRows)
  const curatorRuns = buildCuratorRuns(state, taskId, events)
  const retrievalEventIds = events
    .filter((event) => /memory.*retriev|retriev.*memory|memory_query|memory_search/i.test(event.eventType))
    .map((event) => event.eventId)
  const compactMemoryIds = unique(events
    .filter((event) => /compact/i.test(event.eventType))
    .flatMap((event) => memoryIdsFromEvent(state, event)))
  const skillMemoryIds = allRows
    .filter((row) => row.layer === "skill")
    .map((row) => row.id)
  const provenanceWarnings = buildProvenanceWarnings(state, allRows, events)
  const runtime = state.runtimes[taskId]
  const cursor = state.cursors[taskId]
  const resultFingerprint = fingerprint([
    state.revision,
    normalized,
    rows.map((row) => [row.id, row.revision, row.combinedScore]),
    curatorRuns.map((run) => [run.id, run.phases.length]),
  ])
  return Object.freeze({
    taskId,
    query: Object.freeze(normalized),
    rows: Object.freeze(rows),
    totalRows: ranked.length,
    layerSummaries: Object.freeze(layerSummaries),
    curatorRuns: Object.freeze(curatorRuns),
    latestCuratorRun: curatorRuns[0],
    selectedMemoryIds: Object.freeze(rows.map((row) => row.id)),
    retrievalEventIds: Object.freeze(retrievalEventIds),
    compactMemoryIds: Object.freeze(compactMemoryIds),
    skillMemoryIds: Object.freeze(skillMemoryIds),
    provenanceWarnings: Object.freeze(provenanceWarnings),
    connected: runtime?.connected ?? false,
    committedSequence: cursor?.committedSequence ?? runtime?.lastSequence ?? 0,
    revision: state.revision,
    fingerprint: resultFingerprint,
  })
}

export function buildMemoryRow(
  state: CanonicalProjectionState,
  memory: MemoryProjection,
  taskEvents: readonly CausalEventProjection[],
  query: string,
): MemoryRow {
  const attributes = mergeRecords(memory.attributes, memory.metadata)
  const layer = memoryLayer(memory)
  const sourceEventIds = memoryEventIds(state, memory, taskEvents)
  const sourceMemoryIds = listFrom(attributes, [
    "source_memory_ids",
    "parent_memory_ids",
    "derived_from_memory_ids",
  ])
  const evidence = memoryEvidence(
    state,
    memory,
    sourceEventIds,
    memory.sourceArtifactIds,
    sourceMemoryIds,
  )
  const provenanceScore = provenanceConfidence(memory, evidence)
  const veracityScore = veracityConfidence(memory, evidence, attributes)
  const veracity = veracityLabel(veracityScore, attributes)
  const title = memory.title || memory.key || memory.id
  const summary = memory.summary || textFrom(attributes, ["content_preview", "text", "summary"])
  const matched = searchScore(query, [
    memory.id,
    title,
    summary,
    memory.namespace ?? "",
    memory.key ?? "",
    layer,
    ...listFrom(attributes, ["tags", "labels"]),
    ...memory.sourceArtifactIds,
    ...sourceEventIds,
  ])
  const baseScore = bounded(memory.score ?? decimalFrom(attributes, ["score", "retrieval_score"], 0.5), 0, 1)
  const freshness = freshnessScore(memory.updatedAt, attributes)
  const combinedScore = memoryRankScore(baseScore, matched, provenanceScore, veracityScore, freshness)
  return Object.freeze({
    id: memory.id,
    taskId: memory.taskId,
    runId: memory.runId,
    sessionId: memory.sessionId,
    layer,
    namespace: memory.namespace ?? textFrom(attributes, ["namespace"], "default"),
    key: memory.key ?? textFrom(attributes, ["key"], memory.id),
    title,
    summary,
    score: baseScore,
    searchScore: matched,
    combinedScore,
    tokenCount: memory.tokenCount ?? integerFrom(attributes, ["token_count", "tokens"]),
    revision: memory.revision,
    sequence: memory.sequence,
    lifecycle: memory.lifecycle,
    status: memory.status,
    createdAt: memory.createdAt,
    updatedAt: memory.updatedAt,
    ageMs: elapsed(memory.updatedAt),
    freshness,
    veracity,
    veracityScore,
    provenanceScore,
    sourceArtifactIds: Object.freeze([...memory.sourceArtifactIds]),
    sourceEventIds: Object.freeze(sourceEventIds),
    sourceMemoryIds: Object.freeze(sourceMemoryIds),
    evidence: Object.freeze(evidence),
    tags: Object.freeze(listFrom(attributes, ["tags", "labels"])),
    skillName: textFrom(attributes, ["skill_name", "skill"]) || undefined,
    procedureId: textFrom(attributes, ["procedure_id", "procedure"]) || undefined,
    compactEpoch: integerFrom(attributes, ["compact_epoch"], -1) >= 0
      ? integerFrom(attributes, ["compact_epoch"])
      : undefined,
    staleReason: textFrom(attributes, ["stale_reason"]) || undefined,
    rejectedReason: textFrom(attributes, ["rejected_reason", "reject_reason"]) || undefined,
    retained: !memory.terminal && !["rejected", "deleted", "expired"].includes(memory.lifecycle),
  })
}

function memoryLayer(memory: MemoryProjection): MemoryLayer {
  const attributes = mergeRecords(memory.attributes, memory.metadata)
  const selected = text(
    memory.memoryKind ??
    attributes.layer ??
    attributes.memory_layer ??
    attributes.kind,
  ).toLowerCase()
  if (/skill|procedur|playbook/.test(selected)) return "skill"
  if (/semantic|fact|knowledge/.test(selected)) return "semantic"
  if (/episod|event|history|experience/.test(selected)) return "episodic"
  return "working"
}

function memoryEventIds(
  state: CanonicalProjectionState,
  memory: MemoryProjection,
  events: readonly CausalEventProjection[],
): string[] {
  const explicit = listFrom(mergeRecords(memory.attributes, memory.metadata), [
    "source_event_ids",
    "event_ids",
    "provenance_event_ids",
  ])
  const referenced = events
    .filter((event) =>
      event.entityRefs.includes(`memory:${memory.id}`) ||
      event.summary.includes(memory.id) ||
      event.mutationId === memory.lastEventId)
    .map((event) => event.eventId)
  return unique([
    memory.firstEventId,
    memory.lastEventId,
    ...explicit,
    ...referenced,
  ]).filter((eventId) => Boolean(state.causality.byEvent[eventId]))
}

function memoryEvidence(
  state: CanonicalProjectionState,
  memory: MemoryProjection,
  eventIds: readonly string[],
  artifactIds: readonly string[],
  memoryIds: readonly string[],
): MemoryEvidence[] {
  const evidence: MemoryEvidence[] = []
  for (const eventId of eventIds) {
    const event = state.causality.byEvent[eventId]
    if (!event) continue
    evidence.push({
      id: eventId,
      kind: "event",
      summary: event.summary || event.eventType,
      sequence: event.sequence,
      effective: event.effective,
      correlationId: event.correlationId,
      provenanceScore: event.effective ? 1 : 0.25,
    })
  }
  for (const artifactId of artifactIds) {
    const artifact = state.artifacts[artifactId]
    if (!artifact) continue
    evidence.push({
      id: artifactId,
      kind: "artifact",
      summary: artifact.title || artifact.mediaType || artifact.id,
      sequence: artifact.sequence,
      effective: artifact.effective && !artifact.deleted,
      correlationId: artifact.correlationId,
      provenanceScore: artifact.digest ? 1 : 0.5,
    })
  }
  for (const memoryId of memoryIds) {
    const source = state.memories[memoryId]
    if (!source) continue
    evidence.push({
      id: memoryId,
      kind: "memory",
      summary: source.summary || source.title || source.id,
      sequence: source.sequence,
      effective: source.effective,
      correlationId: source.correlationId,
      provenanceScore: source.score ?? 0.5,
    })
  }
  const toolIds = unique(eventIds.flatMap((eventId) => {
    const event = state.causality.byEvent[eventId]
    return event?.toolCallId ? [event.toolCallId] : []
  }))
  for (const toolId of toolIds) {
    const tool = state.tools[toolId]
    if (!tool) continue
    evidence.push({
      id: toolId,
      kind: "tool",
      summary: tool.summary || tool.toolName || tool.id,
      sequence: tool.sequence,
      effective: tool.effective && !tool.errorCode,
      correlationId: tool.correlationId,
      provenanceScore: tool.resultDigest && !tool.errorCode ? 1 : 0.35,
    })
  }
  if (!evidence.length) {
    evidence.push({
      id: memory.id,
      kind: "memory",
      summary: "Memory has no surviving provenance reference.",
      sequence: memory.sequence,
      effective: false,
      correlationId: memory.correlationId,
      provenanceScore: 0,
    })
  }
  return sortStable(evidence, (left, right) =>
    compareNumber(left.sequence, right.sequence) ||
    compareText(left.id, right.id))
}

function provenanceConfidence(
  memory: MemoryProjection,
  evidence: readonly MemoryEvidence[],
): number {
  const effective = evidence.filter((item) => item.effective)
  const average = mean(effective.map((item) => item.provenanceScore))
  const diversity = new Set(effective.map((item) => item.kind)).size
  const diversityBonus = Math.min(0.2, diversity * 0.05)
  const explicitBonus = memory.sourceArtifactIds.length ? 0.1 : 0
  return bounded(average + diversityBonus + explicitBonus, 0, 1)
}

function veracityConfidence(
  memory: MemoryProjection,
  evidence: readonly MemoryEvidence[],
  attributes: Record<string, unknown>,
): number {
  const explicit = decimalFrom(attributes, [
    "veracity_score",
    "truth_score",
    "confidence",
  ], -1)
  if (explicit >= 0) return bounded(explicit > 1 ? explicit / 100 : explicit, 0, 1)
  const effective = evidence.filter((item) => item.effective)
  const failed = evidence.filter((item) => !item.effective)
  const evidenceWeight = ratio(effective.length, Math.max(1, evidence.length))
  const sourceWeight = mean(effective.map((item) => item.provenanceScore))
  const contradictionPenalty =
    truthFrom(attributes, ["contested", "contradicted", "conflict"]) ? 0.45 : 0
  const rejectionPenalty =
    memory.lifecycle === "rejected" || truthFrom(attributes, ["rejected"]) ? 1 : 0
  const failurePenalty = Math.min(0.3, failed.length * 0.05)
  return bounded(
    evidenceWeight * 0.45 +
    sourceWeight * 0.45 +
    (memory.score ?? 0.5) * 0.1 -
    contradictionPenalty -
    rejectionPenalty -
    failurePenalty,
    0,
    1,
  )
}

function veracityLabel(
  score: number,
  attributes: Record<string, unknown>,
): Veracity {
  if (truthFrom(attributes, ["rejected"])) return "rejected"
  if (truthFrom(attributes, ["contested", "contradicted", "conflict"])) return "contested"
  if (score >= 0.85) return "verified"
  if (score >= 0.65) return "supported"
  return "uncertain"
}

function freshnessScore(updatedAt: string, attributes: Record<string, unknown>): number {
  const halfLifeDays = bounded(decimalFrom(attributes, ["half_life_days"], 30), 0.1, 3650)
  const ageDays = elapsed(updatedAt) / 86_400_000
  return bounded(2 ** (-ageDays / halfLifeDays), 0, 1)
}

function memoryRankScore(
  baseScore: number,
  matched: number,
  provenance: number,
  veracity: number,
  freshness: number,
): number {
  const textScore = bounded(matched / 60, 0, 1)
  return bounded(
    baseScore * 0.25 +
    textScore * 0.25 +
    provenance * 0.2 +
    veracity * 0.2 +
    freshness * 0.1,
    0,
    1,
  )
}

function filterMemory(row: MemoryRow, query: Required<MemoryQuery>): boolean {
  if (query.text && row.searchScore <= 0) return false
  if (query.layers.length && !query.layers.includes(row.layer)) return false
  if (query.namespaces.length && !query.namespaces.includes(row.namespace)) return false
  if (query.veracity.length && !query.veracity.includes(row.veracity)) return false
  if (query.tags.length && !query.tags.every((tag) => row.tags.includes(tag))) return false
  if (!query.includeStale && (row.status === "stale" || Boolean(row.staleReason))) return false
  if (row.combinedScore < query.minimumScore) return false
  return true
}

function compareMemoryRank(left: MemoryRow, right: MemoryRow): number {
  return (
    compareNumber(right.combinedScore, left.combinedScore) ||
    compareNumber(right.veracityScore, left.veracityScore) ||
    compareNumber(right.provenanceScore, left.provenanceScore) ||
    compareNumber(right.sequence, left.sequence) ||
    compareText(left.id, right.id)
  )
}

function layerSummary(rows: readonly MemoryRow[]): MemoryLayerSummary[] {
  const order: MemoryLayer[] = ["working", "episodic", "semantic", "skill"]
  return order.map((layer) => {
    const selected = rows.filter((row) => row.layer === layer)
    return Object.freeze({
      layer,
      count: selected.length,
      tokens: sum(selected.map((row) => row.tokenCount)),
      verified: selected.filter((row) => row.veracity === "verified").length,
      uncertain: selected.filter((row) => row.veracity === "uncertain" || row.veracity === "contested").length,
      stale: selected.filter((row) => row.status === "stale" || Boolean(row.staleReason)).length,
      averageScore: mean(selected.map((row) => row.combinedScore)),
      namespaces: Object.freeze(unique(selected.map((row) => row.namespace)).sort()),
    })
  })
}

function buildCuratorRuns(
  state: CanonicalProjectionState,
  taskId: string,
  events: readonly CausalEventProjection[],
): CuratorRun[] {
  const decisions = events
    .filter((event) => /memory_curator|curator/i.test(event.eventType))
    .map((event) => curatorDecision(state, event))
  const groups = new Map<string, CuratorDecision[]>()
  for (const decision of decisions) {
    const entries = groups.get(decision.runId) ?? []
    entries.push(decision)
    groups.set(decision.runId, entries)
  }
  const runs = [...groups.entries()].map(([id, entries]) => {
    const phases = sortStable(entries, (left, right) =>
      compareNumber(left.sequence, right.sequence) ||
      compareText(left.id, right.id))
    const first = phases[0]
    const last = phases.at(-1)
    const terminal = Boolean(last && ["published", "rejected", "recovered", "failed"].includes(last.phase))
    const warnings: string[] = []
    if (phases.some((phase) => !phase.transitionValid)) warnings.push("invalid curator phase transition")
    if (phases.some((phase) => !phase.deterministic && !phase.modelId)) {
      warnings.push("non-deterministic decision lacks model provenance")
    }
    const reportArtifactIds = unique(phases.flatMap((phase) => phase.artifactIds))
    return Object.freeze({
      id,
      taskId,
      phases: Object.freeze(phases),
      startedAt: first?.createdAt ?? "",
      finishedAt: terminal ? last?.createdAt : undefined,
      active: !terminal,
      failed: last?.phase === "failed",
      candidateCount: phases.filter((phase) => phase.phase === "candidate").length,
      acceptedCount: phases.filter((phase) => phase.phase === "accepted").length,
      rejectedCount: phases.filter((phase) => phase.phase === "rejected").length,
      committedCount: phases.filter((phase) => phase.phase === "committed").length,
      publishedCount: phases.filter((phase) => phase.phase === "published").length,
      recoveredCount: phases.filter((phase) => phase.phase === "recovered").length,
      durationMs: elapsed(first?.createdAt, last?.createdAt),
      modelId: [...phases].reverse().find((phase) => phase.modelId)?.modelId,
      providerId: [...phases].reverse().find((phase) => phase.providerId)?.providerId,
      reportArtifactIds: Object.freeze(reportArtifactIds),
      warnings: Object.freeze(warnings),
    })
  })
  return sortStable(runs, (left, right) =>
    right.startedAt.localeCompare(left.startedAt) ||
    left.id.localeCompare(right.id))
}

function curatorDecision(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
): CuratorDecision {
  const mutation = state.mutations[event.mutationId]
  const phase = curatorPhase(event.eventType)
  const previousEvents = (state.causality.byCorrelation[event.correlationId] ?? [])
    .map((eventId) => state.causality.byEvent[eventId])
    .filter((candidate): candidate is CausalEventProjection =>
      Boolean(candidate && candidate.sequence < event.sequence && /curator/i.test(candidate.eventType)))
    .sort((left, right) => right.sequence - left.sequence)
  const previousPhase = previousEvents[0]
    ? curatorPhase(previousEvents[0].eventType)
    : undefined
  const transitionValid = validCuratorTransition(previousPhase, phase)
  const metadata = mergeRecords(
    mutation ? { operation: mutation.operation, path: mutation.path } : {},
  )
  const runId = textFrom(metadata, ["curator_run_id"], event.correlationId)
  return Object.freeze({
    id: event.eventId,
    runId,
    candidateId: entityId(event.entityRefs, "candidate") || undefined,
    memoryId: entityId(event.entityRefs, "memory") || undefined,
    phase,
    reason: event.summary,
    deterministic: !/llm|model|suggest/i.test(event.eventType),
    modelId: entityId(event.entityRefs, "model") || undefined,
    providerId: entityId(event.entityRefs, "provider") || undefined,
    sourceEventId: event.eventId,
    sequence: event.sequence,
    createdAt: event.createdAt,
    correlationId: event.correlationId,
    artifactIds: Object.freeze([...event.artifactIds]),
    previousPhase,
    transitionValid,
  })
}

function curatorPhase(eventType: string): CuratorPhase {
  const selected = eventType.toLowerCase()
  if (selected.includes("scheduled")) return "scheduled"
  if (selected.includes("candidate")) return "candidate"
  if (selected.includes("accepted")) return "accepted"
  if (selected.includes("committed")) return "committed"
  if (selected.includes("published") || selected.includes("index")) return "published"
  if (selected.includes("rejected")) return "rejected"
  if (selected.includes("recovered")) return "recovered"
  if (selected.includes("failed") || selected.includes("error")) return "failed"
  return "candidate"
}

function validCuratorTransition(
  previous: CuratorPhase | undefined,
  next: CuratorPhase,
): boolean {
  if (!previous) return next === "scheduled" || next === "candidate"
  const allowed: Record<CuratorPhase, readonly CuratorPhase[]> = {
    scheduled: ["candidate", "failed", "recovered"],
    candidate: ["candidate", "accepted", "rejected", "failed"],
    accepted: ["candidate", "committed", "rejected", "failed"],
    committed: ["published", "recovered", "failed"],
    published: ["recovered"],
    rejected: ["candidate", "recovered"],
    recovered: ["candidate", "committed", "published", "failed"],
    failed: ["recovered"],
  }
  return allowed[previous].includes(next)
}

function entityId(refs: readonly string[], domain: string): string {
  const prefix = `${domain}:`
  const ref = refs.find((entry) => entry.startsWith(prefix))
  return ref?.slice(prefix.length) ?? ""
}

function memoryIdsFromEvent(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
): string[] {
  return event.entityRefs
    .filter((ref) => ref.startsWith("memory:"))
    .map((ref) => ref.slice("memory:".length))
    .filter((id) => Boolean(state.memories[id]))
}

function buildProvenanceWarnings(
  state: CanonicalProjectionState,
  rows: readonly MemoryRow[],
  events: readonly CausalEventProjection[],
): string[] {
  const warnings: string[] = []
  for (const row of rows) {
    if (row.provenanceScore === 0) warnings.push(`${row.id}: no surviving provenance`)
    if (row.sourceArtifactIds.some((id) => !state.artifacts[id])) {
      warnings.push(`${row.id}: source artifact is missing`)
    }
    if (row.sourceEventIds.some((id) => !state.causality.byEvent[id])) {
      warnings.push(`${row.id}: source event is missing`)
    }
    if (row.veracity === "contested") warnings.push(`${row.id}: evidence is contested`)
    if (row.veracity === "rejected" && row.retained) warnings.push(`${row.id}: rejected memory remains retained`)
  }
  const retrievalsWithoutMemory = events
    .filter((event) => /retriev/i.test(event.eventType))
    .filter((event) => memoryIdsFromEvent(state, event).length === 0)
  for (const event of retrievalsWithoutMemory) {
    warnings.push(`${event.eventId}: retrieval has no canonical memory reference`)
  }
  return unique(warnings).sort()
}

function normalizeQuery(query: MemoryQuery): Required<MemoryQuery> {
  const layers = unique((query.layers ?? []).filter((layer): layer is MemoryLayer =>
    ["working", "episodic", "semantic", "skill"].includes(layer)))
  const veracity = unique((query.veracity ?? []).filter((item): item is Veracity =>
    ["verified", "supported", "uncertain", "contested", "rejected"].includes(item)))
  return {
    text: text(query.text),
    layers: Object.freeze(layers),
    namespaces: Object.freeze(unique((query.namespaces ?? []).map((item) => text(item)).filter(Boolean)).sort()),
    veracity: Object.freeze(veracity),
    tags: Object.freeze(unique((query.tags ?? []).map((item) => text(item)).filter(Boolean)).sort()),
    includeStale: query.includeStale === true,
    minimumScore: bounded(query.minimumScore ?? 0, 0, 1),
    limit: bounded(Math.floor(query.limit ?? 100), 1, 500),
  }
}

function sameMemoryConsole(
  left: MemoryConsoleProjection,
  right: MemoryConsoleProjection,
): boolean {
  return left === right || left.fingerprint === right.fingerprint
}
