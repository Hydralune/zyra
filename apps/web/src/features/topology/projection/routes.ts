import type { JsonObject, JsonValue } from "../../../events/ingress/index.ts"
import type {
  ProjectionRecord,
  ResourceFitView,
  RouteCandidateView,
  RouteDecisionView,
  TopologyNodeView,
  TopologyProjectionContext,
} from "./contracts.ts"
import { evidenceForEntity, evidenceWithGraph, mergeEvidence } from "./evidence.ts"
import {
  compactAttributes,
  findRecords,
  firstBoolean,
  firstInteger,
  firstNumber,
  firstString,
  firstStringArray,
  hashKey,
  isJsonObject,
  mergedStringArrays,
  namedRecords,
  normalizeEntityState,
  normalizeLocation,
  normalizeToken,
  objectForAliases,
  recordCandidates,
  recordLooksLike,
  resourceVector,
  uniqueStrings,
  valueForAliases,
  vectorFits,
  vectorSubtract,
} from "./record-reader.ts"

const ROUTE_NAMES = [
  "decision",
  "route",
  "route_decision",
  "topology_route",
  "scheduler_plan",
  "admission_plan",
  "provider_route",
  "layered_route_decision",
  "resource_decision",
]
const CANDIDATE_NAMES = [
  "route_candidates",
  "candidates",
  "capacity_observations",
  "alternatives",
  "recipients",
]

interface RouteCandidateSource {
  record: ProjectionRecord
  source: JsonObject
  container: JsonObject
  routeId: string
  decisionId?: string
  nodeId?: string
  graphId?: string
  graphRevision: number
  commitRevision: number
}

export function projectRoutes(
  context: TopologyProjectionContext,
  nodes: readonly TopologyNodeView[],
): RouteDecisionView[] {
  const sources: RouteCandidateSource[] = []
  for (const record of context.records) {
    sources.push(...routeSources(record))
  }
  sources.sort(compareRouteSource)
  const byId = new Map<string, RouteDecisionView>()
  for (const source of sources) {
    const route = routeView(source, context, nodes)
    const existing = byId.get(route.id)
    if (!existing) {
      byId.set(route.id, route)
      continue
    }
    if (compareRouteVersion(existing, route) <= 0) {
      byId.set(route.id, mergeRoute(existing, route))
    } else {
      byId.set(route.id, mergeRoute(route, existing))
    }
  }
  const values = [...byId.values()].sort(compareRoute)
  markRouteChanges(values)
  return values
}

function routeSources(record: ProjectionRecord): RouteCandidateSource[] {
  const output: RouteCandidateSource[] = []
  const named = namedRecords(record, ROUTE_NAMES)
  for (const source of named) {
    const candidate = sourceFromRecord(record, source)
    if (candidate) output.push(candidate)
  }
  if (record.domain === "scheduler") {
    const source =
      named.find((item) => routeIdentity(item)) ?? record.attributes
    const candidate = sourceFromRecord(record, source)
    if (candidate) output.push(candidate)
  }
  const nested = findRecords(
    recordCandidates(record),
    (candidate, path) => {
      const routeLike = recordLooksLike(candidate, [
        "route_id",
        "selected_worker",
        "selected_worker_id",
        "selected_manifest_id",
        "selected_provider_id",
        "route_candidates",
        "candidates",
      ])
      if (!routeLike) return false
      if (path.some((part) => /candidate|alternative|recipient/i.test(part))) {
        return false
      }
      return (
        "route_id" in candidate ||
        "decision_id" in candidate ||
        "selected_worker" in candidate
      )
    },
    4_000,
  )
  for (const source of nested) {
    const candidate = sourceFromRecord(record, source)
    if (candidate) output.push(candidate)
  }
  return dedupeRouteSources(output)
}

function sourceFromRecord(
  record: ProjectionRecord,
  source: JsonObject,
): RouteCandidateSource | undefined {
  const all = [source, ...recordCandidates(record)]
  const decisionId = firstString(
    [source],
    "decision_id",
    "route_decision_id",
    "resource_decision_id",
    "plan_id",
  )
  const nodeId =
    firstString([source], "node_id", "graph_node_id", "target_node_id") ??
    record.nodeId
  const routeId =
    routeIdentity(source) ??
    firstString(all, "route_id", "backend_route_id") ??
    decisionId ??
    (record.domain === "scheduler" ? record.entityId : undefined)
  if (!routeId) return undefined
  return {
    record,
    source,
    container: record.attributes,
    routeId,
    decisionId,
    nodeId,
    graphId: firstString(all, "graph_id", "graphId"),
    graphRevision:
      firstInteger(
        all,
        "graph_revision",
        "committed_revision",
        "topology_revision",
      ) ?? 0,
    commitRevision:
      firstInteger(all, "commit_revision", "committed_revision") ?? 0,
  }
}

function routeView(
  source: RouteCandidateSource,
  context: TopologyProjectionContext,
  nodes: readonly TopologyNodeView[],
): RouteDecisionView {
  const { record } = source
  const all = [source.source, source.container, ...recordCandidates(record)]
  const candidateRecords = extractCandidateRecords(source.source, source.container)
  const selectedWorkerId = firstString(
    all,
    "selected_worker_id",
    "selected_worker",
    "selected",
    "worker_id",
  )
  const selectedManifestId = firstString(
    all,
    "selected_manifest_id",
    "manifest_id",
  )
  const selectedProviderId = firstString(
    all,
    "selected_provider_id",
    "provider_id",
  )
  const selectedModelId = firstString(
    all,
    "selected_model_id",
    "model_id",
  )
  const selectedBackendId = firstString(
    all,
    "selected_backend_id",
    "selected_backend",
    "backend_id",
    "backend",
  )
  const requested = requestedResourceVector(all)
  const requiredCapabilities = mergedStringArrays(
    all,
    "required_capabilities",
    "required",
    "capability_refs",
  )
  const candidates = candidateRecords
    .map((candidate, index) =>
      candidateView(
        candidate,
        index,
        source,
        context,
        selectedWorkerId,
        selectedManifestId,
        selectedProviderId,
        selectedModelId,
        requested,
        requiredCapabilities,
      ),
    )
    .sort(compareCandidate)
  normalizeCandidateRanks(candidates)
  ensureSelectedCandidate(
    candidates,
    source,
    context,
    selectedWorkerId,
    selectedManifestId,
    selectedProviderId,
    selectedModelId,
    selectedBackendId,
    requested,
    requiredCapabilities,
  )
  const selectedCandidate =
    candidates.find((candidate) => candidate.selected) ??
    candidates.find((candidate) => candidate.accepted)
  const routeType =
    firstString(all, "route_type", "purpose", "decision_type", "action") ??
    "topology_route"
  const previousRouteId = firstString(
    all,
    "previous_route_id",
    "prior_route_id",
    "replaced_route_id",
  )
  const topologyMutation = firstBoolean(
    all,
    "topology_mutation",
    "open_world_mutation",
  )
  const selectedLocation = normalizeLocation(
    firstString(all, "selected_location", "location"),
    selectedCandidate?.location,
  )
  const evidence = evidenceWithGraph(
    evidenceForEntity(
      context.evidence,
      record,
      "route",
      source.routeId,
      source.graphId,
      source.graphRevision,
    ),
    source.graphId,
    source.graphRevision,
    [
      `route:${source.routeId}`,
      source.nodeId ? `node:${source.nodeId}` : "",
      selectedWorkerId ? `worker:${selectedWorkerId}` : "",
    ],
  )
  const state = normalizeEntityState(
    firstString(all, "phase", "status", "state"),
    firstBoolean(all, "accepted") === false ? "rejected" : undefined,
    record.lifecycle,
  )
  const accepted =
    firstBoolean(all, "accepted", "dispatchable", "committed") ??
    Boolean(selectedWorkerId || selectedCandidate)
  const node = source.nodeId
    ? nodes.find((candidate) => candidate.id === source.nodeId)
    : undefined
  const reasons = uniqueStrings([
    ...firstStringArray(all, "reasons", "rationale"),
    ...candidates.filter((candidate) => candidate.selected).flatMap(
      (candidate) => candidate.reasons,
    ),
  ])
  return {
    id: source.routeId,
    routeId: source.routeId,
    taskId: record.taskId,
    runId: record.runId,
    kind: "route",
    sequence: record.sequence,
    revision:
      firstInteger(all, "revision", "route_revision", "catalog_revision") ??
      record.revision,
    graphRevision: source.graphRevision,
    commitRevision: source.commitRevision,
    state,
    title:
      firstString(all, "title", "name", "summary") ??
      `Route ${source.routeId}`,
    summary:
      firstString(all, "summary", "rationale", "reason") ??
      record.summary,
    namespace:
      node?.namespace ??
      firstString(all, "namespace", "checkpoint_ns") ??
      "root",
    subgraphId:
      node?.subgraphId ?? firstString(all, "subgraph_id", "graph_id"),
    branchId: firstString(all, "branch_id"),
    checkpointId:
      firstString(all, "checkpoint_id") ?? record.checkpointId,
    effective: record.effective && accepted,
    terminal: record.terminal || ["completed", "failed", "rejected"].includes(state),
    pending: ["planned", "queued", "waiting"].includes(state),
    removed: record.removed,
    evidence,
    attributes: Object.freeze(
      compactAttributes(
        [source.source, source.container],
        context.sensitiveFieldDrops,
      ),
    ),
    routeType,
    nodeId: source.nodeId,
    selectedWorkerId: selectedCandidate?.workerId ?? selectedWorkerId,
    selectedCandidateId: selectedCandidate?.id,
    selectedProviderId:
      selectedCandidate?.providerId ?? selectedProviderId,
    selectedModelId: selectedCandidate?.modelId ?? selectedModelId,
    selectedBackendId:
      selectedCandidate?.backendId ?? selectedBackendId,
    selectedLocation,
    previousRouteId,
    topK:
      firstInteger(all, "top_k", "topK") ??
      Math.max(1, candidates.length),
    candidateCount:
      firstInteger(all, "candidate_count", "available_recipient_count") ??
      candidates.length,
    candidates: Object.freeze(candidates),
    requiredCapabilities: Object.freeze(requiredCapabilities),
    requiredTools: Object.freeze(
      mergedStringArrays(all, "required_tools", "tool_ids"),
    ),
    reasons: Object.freeze(reasons),
    accepted,
    routeHealth:
      firstString(all, "route_health", "scheduler_route_health", "health") ??
      selectedCandidate?.health ??
      "unknown",
    routeChanged: Boolean(previousRouteId),
    fixedCandidateSelection:
      candidates.length > 0 && topologyMutation !== true,
    topologyMutation: topologyMutation === true,
    policyId: firstString(all, "policy_id", "policy_rule"),
    decisionId: source.decisionId,
    resourceDecisionId: firstString(all, "resource_decision_id"),
    planDigest: firstString(all, "plan_digest", "request_digest"),
  }
}

function candidateView(
  candidate: JsonObject,
  index: number,
  route: RouteCandidateSource,
  context: TopologyProjectionContext,
  selectedWorkerId: string | undefined,
  selectedManifestId: string | undefined,
  selectedProviderId: string | undefined,
  selectedModelId: string | undefined,
  requested: ReturnType<typeof resourceVector>,
  requiredCapabilities: readonly string[],
): RouteCandidateView {
  const sources = [candidate]
  const workerId =
    firstString(
      sources,
      "worker_id",
      "worker_name",
      "runtime_worker",
      "recipient_id",
      "id",
    ) ?? `candidate-${index + 1}`
  const manifestId = firstString(sources, "manifest_id", "worker_manifest_id")
  const providerId = firstString(sources, "provider_id")
  const modelId = firstString(sources, "model_id")
  const selected =
    firstBoolean(sources, "selected") === true ||
    workerId === selectedWorkerId ||
    (manifestId !== undefined && manifestId === selectedManifestId) ||
    (providerId !== undefined &&
      providerId === selectedProviderId &&
      modelId !== undefined &&
      modelId === selectedModelId)
  const accepted =
    firstBoolean(sources, "accepted", "eligible", "dispatchable") ??
    selected
  const capabilities = mergedStringArrays(
    sources,
    "capabilities",
    "capability_refs",
    "selected_capabilities",
  )
  const missingCapabilities = requiredCapabilities.filter(
    (capability) => !capabilities.includes(capability),
  )
  const resourceFit = candidateResourceFit(candidate, requested)
  const candidateId =
    firstString(sources, "candidate_id", "recipient_id") ??
    `${route.routeId}:candidate:${workerId}`
  return {
    id: candidateId,
    routeId: route.routeId,
    workerId,
    workerName:
      firstString(sources, "worker_name", "runtime_worker", "display_name") ??
      workerId,
    manifestId,
    backendId: firstString(
      sources,
      "backend_id",
      "backend",
      "selected_backend",
    ),
    providerId,
    modelId,
    location: normalizeLocation(
      firstString(sources, "location", "selected_location"),
      firstString(sources, "backend"),
    ),
    rank: firstInteger(sources, "rank", "position") ?? index + 1,
    score: firstNumber(sources, "score", "weight", "priority") ?? 0,
    accepted:
      accepted &&
      missingCapabilities.length === 0 &&
      (resourceFit?.fits ?? true),
    selected,
    health:
      firstString(sources, "route_health", "health", "status") ?? "unknown",
    reasons: Object.freeze(
      uniqueStrings([
        ...firstStringArray(sources, "reasons"),
        firstString(sources, "reason", "rationale"),
      ]),
    ),
    capabilities: Object.freeze(capabilities),
    requiredCapabilities: Object.freeze([...requiredCapabilities]),
    missingCapabilities: Object.freeze(missingCapabilities),
    resourceFit,
    privacyClass: firstString(
      sources,
      "privacy_class",
      "privacy_level",
      "privacy_mode",
    ),
    costEstimate: firstNumber(
      sources,
      "cost_estimate",
      "estimated_cost",
      "cost_per_1k_tokens",
    ),
    latencyEstimateMs: firstNumber(
      sources,
      "latency_estimate_ms",
      "estimated_latency_ms",
      "latency_ms",
    ),
    evidence: evidenceWithGraph(
      context.evidence.forRecord(route.record),
      route.graphId,
      route.graphRevision,
      [`candidate:${candidateId}`, `worker:${workerId}`],
    ),
    attributes: Object.freeze(
      compactAttributes([candidate], context.sensitiveFieldDrops),
    ),
  }
}

function ensureSelectedCandidate(
  candidates: RouteCandidateView[],
  route: RouteCandidateSource,
  context: TopologyProjectionContext,
  selectedWorkerId: string | undefined,
  selectedManifestId: string | undefined,
  selectedProviderId: string | undefined,
  selectedModelId: string | undefined,
  selectedBackendId: string | undefined,
  requested: ReturnType<typeof resourceVector>,
  requiredCapabilities: readonly string[],
): void {
  if (candidates.some((candidate) => candidate.selected)) return
  if (
    !selectedWorkerId &&
    !selectedManifestId &&
    !selectedProviderId &&
    !selectedModelId
  ) {
    return
  }
  const workerId =
    selectedWorkerId ??
    selectedManifestId ??
    [selectedProviderId, selectedModelId].filter(Boolean).join("/") ??
    "selected"
  const id = `${route.routeId}:candidate:${workerId}`
  candidates.push({
    id,
    routeId: route.routeId,
    workerId,
    workerName: workerId,
    manifestId: selectedManifestId,
    backendId: selectedBackendId,
    providerId: selectedProviderId,
    modelId: selectedModelId,
    location: normalizeLocation(
      firstString([route.source], "selected_location"),
      selectedBackendId,
    ),
    rank: 1,
    score: firstNumber([route.source], "score") ?? 0,
    accepted: true,
    selected: true,
    health:
      firstString([route.source], "route_health", "health") ?? "unknown",
    reasons: Object.freeze(
      firstStringArray([route.source], "reasons", "rationale"),
    ),
    capabilities: Object.freeze([]),
    requiredCapabilities: Object.freeze([...requiredCapabilities]),
    missingCapabilities: Object.freeze([]),
    resourceFit: {
      requested,
      capacity: requested,
      allocated: resourceVector(undefined),
      available: requested,
      fits: true,
      missing: Object.freeze([]),
      utilization: Object.freeze({}),
    },
    evidence: evidenceWithGraph(
      context.evidence.forRecord(route.record),
      route.graphId,
      route.graphRevision,
      [`candidate:${id}`, `worker:${workerId}`],
    ),
    attributes: Object.freeze({ synthetic_selected_candidate: true }),
  })
  normalizeCandidateRanks(candidates)
}

function candidateResourceFit(
  candidate: JsonObject,
  requested: ReturnType<typeof resourceVector>,
): ResourceFitView | undefined {
  const capacityObject = objectForAliases(candidate, [
    "capacity",
    "resource_capacity",
  ])
  const allocatedObject = objectForAliases(candidate, [
    "allocated",
    "resource_allocated",
  ])
  const availableObject = objectForAliases(candidate, [
    "available",
    "resource_available",
  ])
  if (!capacityObject && !allocatedObject && !availableObject) return undefined
  const capacity = resourceVector(capacityObject)
  const allocated = resourceVector(allocatedObject)
  const available = availableObject
    ? resourceVector(availableObject)
    : vectorSubtract(capacity, allocated)
  const fit = vectorFits(available, requested)
  return Object.freeze({
    requested,
    capacity,
    allocated,
    available,
    fits: fit.fits,
    missing: Object.freeze(fit.missing),
    utilization: Object.freeze(fit.utilization),
  })
}

function requestedResourceVector(
  sources: readonly JsonObject[],
): ReturnType<typeof resourceVector> {
  for (const source of sources) {
    const requirement = objectForAliases(source, [
      "requirement",
      "requirements",
      "request",
    ])
    const resources = requirement
      ? objectForAliases(requirement, ["resources", "resource_request"])
      : undefined
    if (resources) return resourceVector(resources)
    const direct = objectForAliases(source, [
      "requested_resources",
      "resources",
      "resource_request",
    ])
    if (direct) return resourceVector(direct)
  }
  return resourceVector(undefined)
}

function extractCandidateRecords(
  source: JsonObject,
  container: JsonObject,
): JsonObject[] {
  const output: JsonObject[] = []
  for (const root of [source, container]) {
    for (const name of CANDIDATE_NAMES) {
      const value = valueForAliases(root, [name])
      if (Array.isArray(value)) {
        output.push(...value.filter(isJsonObject))
      } else if (isJsonObject(value)) {
        output.push(...Object.values(value).filter(isJsonObject))
      }
    }
  }
  const nested = findRecords(
    [source, container],
    (candidate, path) => {
      if (!path.some((part) => /candidate|alternative|recipient|capacity/i.test(part))) {
        return false
      }
      return recordLooksLike(candidate, [
        "worker_id",
        "worker_name",
        "runtime_worker",
        "provider_id",
        "model_id",
        "score",
      ])
    },
    2_000,
  )
  const byKey = new Map<string, JsonObject>()
  for (const candidate of [...output, ...nested]) {
    const key =
      firstString(
        [candidate],
        "candidate_id",
        "worker_id",
        "worker_name",
        "runtime_worker",
        "recipient_id",
      ) ?? hashKey(candidate)
    const existing = byKey.get(key)
    byKey.set(key, existing ? { ...existing, ...candidate } : candidate)
  }
  return [...byKey.values()]
}

function normalizeCandidateRanks(candidates: RouteCandidateView[]): void {
  candidates.sort(compareCandidate)
  for (let index = 0; index < candidates.length; index += 1) {
    candidates[index] = { ...candidates[index]!, rank: index + 1 }
  }
}

function markRouteChanges(routes: RouteDecisionView[]): void {
  const byNode = new Map<string, RouteDecisionView[]>()
  for (const route of routes) {
    const key = route.nodeId ?? `task:${route.taskId}:${route.routeType}`
    const values = byNode.get(key) ?? []
    values.push(route)
    byNode.set(key, values)
  }
  for (const values of byNode.values()) {
    values.sort(compareRoute)
    let previous: RouteDecisionView | undefined
    for (const route of values) {
      const index = routes.indexOf(route)
      const changed =
        Boolean(route.previousRouteId) ||
        (previous !== undefined &&
          (previous.selectedWorkerId !== route.selectedWorkerId ||
            previous.selectedProviderId !== route.selectedProviderId ||
            previous.selectedModelId !== route.selectedModelId ||
            previous.selectedLocation !== route.selectedLocation))
      if (index >= 0 && changed !== route.routeChanged) {
        routes[index] = {
          ...route,
          routeChanged: changed,
          previousRouteId: route.previousRouteId ?? previous?.routeId,
        }
      }
      previous = routes[index] ?? route
    }
  }
}

function mergeRoute(
  older: RouteDecisionView,
  newer: RouteDecisionView,
): RouteDecisionView {
  const candidates = mergeCandidates(older.candidates, newer.candidates)
  return {
    ...newer,
    candidates: Object.freeze(candidates),
    candidateCount: Math.max(
      newer.candidateCount,
      older.candidateCount,
      candidates.length,
    ),
    requiredCapabilities: Object.freeze(
      uniqueStrings([
        ...older.requiredCapabilities,
        ...newer.requiredCapabilities,
      ]),
    ),
    requiredTools: Object.freeze(
      uniqueStrings([...older.requiredTools, ...newer.requiredTools]),
    ),
    reasons: Object.freeze(
      uniqueStrings([...older.reasons, ...newer.reasons]),
    ),
    routeChanged: older.routeChanged || newer.routeChanged,
    topologyMutation: older.topologyMutation || newer.topologyMutation,
    fixedCandidateSelection:
      older.fixedCandidateSelection || newer.fixedCandidateSelection,
    evidence: mergeEvidence(older.evidence, newer.evidence),
  }
}

function mergeCandidates(
  older: readonly RouteCandidateView[],
  newer: readonly RouteCandidateView[],
): RouteCandidateView[] {
  const byId = new Map<string, RouteCandidateView>()
  for (const candidate of [...older, ...newer]) {
    const existing = byId.get(candidate.id)
    if (!existing) {
      byId.set(candidate.id, candidate)
      continue
    }
    byId.set(candidate.id, {
      ...existing,
      ...candidate,
      selected: existing.selected || candidate.selected,
      accepted: existing.accepted || candidate.accepted,
      reasons: Object.freeze(
        uniqueStrings([...existing.reasons, ...candidate.reasons]),
      ),
      capabilities: Object.freeze(
        uniqueStrings([...existing.capabilities, ...candidate.capabilities]),
      ),
      requiredCapabilities: Object.freeze(
        uniqueStrings([
          ...existing.requiredCapabilities,
          ...candidate.requiredCapabilities,
        ]),
      ),
      missingCapabilities: Object.freeze(
        uniqueStrings([
          ...existing.missingCapabilities,
          ...candidate.missingCapabilities,
        ]),
      ),
      evidence: mergeEvidence(existing.evidence, candidate.evidence),
    })
  }
  const output = [...byId.values()]
  normalizeCandidateRanks(output)
  return output
}

function routeIdentity(source: JsonObject): string | undefined {
  return firstString(
    [source],
    "route_id",
    "routeId",
    "backend_route_id",
    "route_decision_id",
  )
}

function dedupeRouteSources(
  sources: readonly RouteCandidateSource[],
): RouteCandidateSource[] {
  const output: RouteCandidateSource[] = []
  const seen = new Set<string>()
  for (const source of sources) {
    const key = [
      source.record.eventId,
      source.routeId,
      source.graphRevision,
      source.commitRevision,
      hashKey(source.source),
    ].join("\0")
    if (seen.has(key)) continue
    seen.add(key)
    output.push(source)
  }
  return output
}

function compareRouteSource(
  left: RouteCandidateSource,
  right: RouteCandidateSource,
): number {
  return (
    left.record.sequence - right.record.sequence ||
    left.graphRevision - right.graphRevision ||
    left.commitRevision - right.commitRevision ||
    left.routeId.localeCompare(right.routeId)
  )
}

function compareRouteVersion(
  left: RouteDecisionView,
  right: RouteDecisionView,
): number {
  return (
    left.graphRevision - right.graphRevision ||
    left.commitRevision - right.commitRevision ||
    left.sequence - right.sequence ||
    left.revision - right.revision
  )
}

function compareRoute(
  left: RouteDecisionView,
  right: RouteDecisionView,
): number {
  return (
    left.sequence - right.sequence ||
    left.revision - right.revision ||
    left.routeId.localeCompare(right.routeId)
  )
}

function compareCandidate(
  left: RouteCandidateView,
  right: RouteCandidateView,
): number {
  if (left.selected !== right.selected) return left.selected ? -1 : 1
  if (left.accepted !== right.accepted) return left.accepted ? -1 : 1
  return (
    right.score - left.score ||
    left.rank - right.rank ||
    left.workerId.localeCompare(right.workerId) ||
    left.id.localeCompare(right.id)
  )
}
