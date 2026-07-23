import type { JsonObject } from "../../../events/ingress/index.ts"
import type {
  ModelSplitSegment,
  PlacementLocation,
  PlacementView,
  ProjectionRecord,
  ResourceFitView,
  RouteDecisionView,
  SlaAssessment,
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
  hashKey,
  isJsonObject,
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

const PLACEMENT_NAMES = [
  "placement",
  "resource_decision",
  "provider_route",
  "dispatch",
  "binding",
  "lease",
  "model_split",
]

interface PlacementSource {
  record: ProjectionRecord
  source: JsonObject
  placementId: string
  routeId?: string
  nodeId?: string
  workerId?: string
  graphId?: string
  graphRevision: number
  commitRevision: number
}

export function projectPlacements(
  context: TopologyProjectionContext,
  routes: readonly RouteDecisionView[],
  nodes: readonly TopologyNodeView[],
): PlacementView[] {
  const sources: PlacementSource[] = []
  for (const record of context.records) {
    sources.push(...placementSources(record))
  }
  sources.push(...routePlacementSources(context.records, routes))
  sources.sort(compareSource)
  const byId = new Map<string, PlacementView>()
  for (const source of sources) {
    const view = placementView(source, context, routes, nodes)
    const existing = byId.get(view.id)
    if (!existing) {
      byId.set(view.id, view)
      continue
    }
    if (comparePlacementVersion(existing, view) <= 0) {
      byId.set(view.id, mergePlacement(existing, view))
    } else {
      byId.set(view.id, mergePlacement(view, existing))
    }
  }
  const values = [...byId.values()].sort(comparePlacement)
  markPlacementChanges(values)
  return values
}

function placementSources(record: ProjectionRecord): PlacementSource[] {
  const output: PlacementSource[] = []
  const named = namedRecords(record, PLACEMENT_NAMES)
  for (const source of named) {
    const candidate = sourceFromRecord(record, source)
    if (candidate) output.push(candidate)
  }
  if (
    ["scheduler", "worker"].includes(record.domain) &&
    hasPlacementSignal(record.attributes)
  ) {
    const candidate = sourceFromRecord(record, record.attributes)
    if (candidate) output.push(candidate)
  }
  const nested = findRecords(
    recordCandidates(record),
    (candidate, path) => {
      if (
        !recordLooksLike(candidate, [
          "selected_location",
          "location",
          "selected_backend",
          "backend_id",
          "provider_id",
          "model_id",
          "resource_capacity",
          "model_split",
        ])
      ) {
        return false
      }
      return path.some((part) =>
        /placement|resource|route|dispatch|binding|lease|provider|model/i.test(
          part,
        ),
      )
    },
    4_000,
  )
  for (const source of nested) {
    const candidate = sourceFromRecord(record, source)
    if (candidate) output.push(candidate)
  }
  return dedupeSources(output)
}

function routePlacementSources(
  records: readonly ProjectionRecord[],
  routes: readonly RouteDecisionView[],
): PlacementSource[] {
  const output: PlacementSource[] = []
  for (const route of routes) {
    const record =
      records.find((candidate) =>
        route.evidence.eventIds.includes(candidate.eventId),
      ) ??
      records.find(
        (candidate) =>
          candidate.domain === "scheduler" &&
          candidate.entityId === route.routeId,
      )
    if (!record) continue
    const selected = route.candidates.find((candidate) => candidate.selected)
    const source: JsonObject = {
      placement_id: `${route.routeId}:placement`,
      route_id: route.routeId,
      node_id: route.nodeId ?? null,
      selected_worker: route.selectedWorkerId ?? null,
      selected_backend: route.selectedBackendId ?? null,
      selected_provider_id: route.selectedProviderId ?? null,
      selected_model_id: route.selectedModelId ?? null,
      selected_location: route.selectedLocation,
      privacy_class: selected?.privacyClass ?? null,
      cost_estimate: selected?.costEstimate ?? null,
      latency_estimate_ms: selected?.latencyEstimateMs ?? null,
    }
    output.push({
      record,
      source,
      placementId: `${route.routeId}:placement`,
      routeId: route.routeId,
      nodeId: route.nodeId,
      workerId: route.selectedWorkerId,
      graphId: route.evidence.graphIds.at(-1),
      graphRevision: route.graphRevision,
      commitRevision: route.commitRevision,
    })
  }
  return output
}

function sourceFromRecord(
  record: ProjectionRecord,
  source: JsonObject,
): PlacementSource | undefined {
  const all = [source, ...recordCandidates(record)]
  const routeId = firstString(
    all,
    "route_id",
    "backend_route_id",
    "provider_route_id",
  )
  const nodeId =
    firstString(all, "node_id", "graph_node_id") ?? record.nodeId
  const workerId =
    firstString(
      all,
      "selected_worker_id",
      "selected_worker",
      "worker_id",
    ) ?? record.workerId
  const providerId = firstString(
    all,
    "selected_provider_id",
    "provider_id",
  )
  const modelId = firstString(all, "selected_model_id", "model_id")
  const backendId = firstString(
    all,
    "selected_backend_id",
    "selected_backend",
    "backend_id",
    "backend",
  )
  const location = normalizeLocation(
    firstString(all, "selected_location", "location"),
    backendId,
  )
  if (
    !routeId &&
    !workerId &&
    !providerId &&
    !modelId &&
    !backendId &&
    location === "unknown"
  ) {
    return undefined
  }
  const placementId =
    firstString(
      [source],
      "placement_id",
      "resource_decision_id",
      "binding_id",
      "lease_id",
      "dispatch_id",
    ) ??
    [
      routeId ?? record.entityId,
      workerId,
      providerId,
      modelId,
      location,
    ]
      .filter(Boolean)
      .join(":")
  return {
    record,
    source,
    placementId,
    routeId,
    nodeId,
    workerId,
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

function placementView(
  source: PlacementSource,
  context: TopologyProjectionContext,
  routes: readonly RouteDecisionView[],
  nodes: readonly TopologyNodeView[],
): PlacementView {
  const { record } = source
  const all = [source.source, ...recordCandidates(record)]
  const route = source.routeId
    ? routes.find((candidate) => candidate.routeId === source.routeId)
    : undefined
  const node = source.nodeId
    ? nodes.find((candidate) => candidate.id === source.nodeId)
    : undefined
  const backendId =
    firstString(
      all,
      "selected_backend_id",
      "selected_backend",
      "backend_id",
      "backend",
    ) ?? route?.selectedBackendId
  const providerId =
    firstString(all, "selected_provider_id", "provider_id") ??
    route?.selectedProviderId
  const modelId =
    firstString(all, "selected_model_id", "model_id", "primary") ??
    route?.selectedModelId
  const location = normalizeLocation(
    firstString(all, "selected_location", "location"),
    backendId,
    route?.selectedLocation,
  )
  const privacyClass =
    firstString(
      all,
      "privacy_class",
      "privacy_level",
      "privacy_mode",
      "selected_privacy",
    ) ?? "project"
  const modelSplit = modelSplitSegments(
    source.placementId,
    source.source,
    providerId,
    modelId,
    backendId,
    location,
    context,
  )
  const modelSplitStrategy =
    firstString(
      all,
      "model_split_strategy",
      "strategy",
      "split_strategy",
    ) ??
    (modelSplit.length > 1 ? "multi_stage" : "single_worker")
  const resourceFit = placementResourceFit(all)
  const costEstimate = firstNumber(
    all,
    "cost_estimate",
    "estimated_cost",
    "cost_usd_estimate",
    "cost_per_1k_tokens",
  )
  const latencyEstimateMs = firstNumber(
    all,
    "latency_estimate_ms",
    "estimated_latency_ms",
    "latency_ms",
  )
  const sla = assessSla(
    all,
    privacyClass,
    location,
    resourceFit,
    costEstimate,
    latencyEstimateMs,
  )
  const evidence = evidenceWithGraph(
    evidenceForEntity(
      context.evidence,
      record,
      "placement",
      source.placementId,
      source.graphId,
      source.graphRevision,
    ),
    source.graphId,
    source.graphRevision,
    [
      `placement:${source.placementId}`,
      source.routeId ? `route:${source.routeId}` : "",
      source.nodeId ? `node:${source.nodeId}` : "",
      source.workerId ? `worker:${source.workerId}` : "",
    ],
  )
  const state = normalizeEntityState(
    firstString(all, "phase", "status", "state"),
    route?.state,
    record.lifecycle,
  )
  return {
    id: source.placementId,
    placementId: source.placementId,
    taskId: record.taskId,
    runId: record.runId,
    kind: "placement",
    sequence: record.sequence,
    revision:
      firstInteger(all, "revision", "placement_revision", "version") ??
      record.revision,
    graphRevision: source.graphRevision,
    commitRevision: source.commitRevision,
    state,
    title:
      firstString(all, "title", "name", "display_name") ??
      `${location} placement`,
    summary:
      firstString(all, "summary", "reason", "rationale") ??
      route?.summary ??
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
    effective: record.effective && (route?.accepted ?? true),
    terminal: record.terminal || ["completed", "failed", "rejected"].includes(state),
    pending: ["planned", "queued", "waiting"].includes(state),
    removed: record.removed,
    evidence,
    attributes: Object.freeze(
      compactAttributes([source.source], context.sensitiveFieldDrops),
    ),
    routeId: source.routeId,
    nodeId: source.nodeId,
    workerId: source.workerId ?? route?.selectedWorkerId,
    backendId,
    providerId,
    modelId,
    location,
    privacyClass,
    modelSplitStrategy,
    modelSplit: Object.freeze(modelSplit),
    resourceFit,
    costEstimate,
    latencyEstimateMs,
    sla,
    previousPlacementId: firstString(
      all,
      "previous_placement_id",
      "prior_placement_id",
    ),
    changed: Boolean(
      firstString(all, "previous_placement_id", "prior_placement_id"),
    ),
  }
}

function modelSplitSegments(
  placementId: string,
  source: JsonObject,
  providerId: string | undefined,
  modelId: string | undefined,
  backendId: string | undefined,
  location: PlacementLocation,
  context: TopologyProjectionContext,
): ModelSplitSegment[] {
  const split = objectForAliases(source, [
    "model_split",
    "split",
    "modelSplit",
  ])
  const output: ModelSplitSegment[] = []
  if (split) {
    const primary = firstString([split], "primary", "primary_model")
    const secondary = firstString([split], "secondary", "secondary_model")
    if (primary) {
      output.push({
        id: `${placementId}:primary`,
        role: "primary",
        providerId,
        modelId: primary,
        backendId:
          firstString([split], "backend", "primary_backend") ?? backendId,
        location: normalizeLocation(
          firstString([split], "location", "primary_location"),
          backendId,
          location,
        ),
        handoff: firstString([split], "handoff"),
        primary: true,
        attributes: Object.freeze(
          compactAttributes([split], context.sensitiveFieldDrops),
        ),
      })
    }
    if (secondary) {
      output.push({
        id: `${placementId}:secondary`,
        role: "secondary",
        providerId: firstString([split], "secondary_provider_id") ?? providerId,
        modelId: secondary,
        backendId:
          firstString([split], "secondary_backend") ?? backendId,
        location: normalizeLocation(
          firstString([split], "secondary_location"),
          secondary,
        ),
        handoff: firstString([split], "handoff"),
        primary: false,
        attributes: Object.freeze(
          compactAttributes([split], context.sensitiveFieldDrops),
        ),
      })
    }
    const stages = valueForAliases(split, ["stages", "segments"])
    if (Array.isArray(stages)) {
      for (let index = 0; index < stages.length; index += 1) {
        const stage = stages[index]
        if (!isJsonObject(stage)) continue
        output.push({
          id:
            firstString([stage], "id", "segment_id") ??
            `${placementId}:stage:${index + 1}`,
          role:
            firstString([stage], "role", "purpose", "stage") ??
            `stage_${index + 1}`,
          providerId: firstString([stage], "provider_id"),
          modelId: firstString([stage], "model_id", "model"),
          backendId: firstString([stage], "backend_id", "backend"),
          location: normalizeLocation(
            firstString([stage], "location"),
            firstString([stage], "backend"),
          ),
          handoff: firstString([stage], "handoff", "reason"),
          primary:
            firstBoolean([stage], "primary") ?? output.length === 0,
          attributes: Object.freeze(
            compactAttributes([stage], context.sensitiveFieldDrops),
          ),
        })
      }
    }
  }
  if (output.length === 0) {
    output.push({
      id: `${placementId}:primary`,
      role: "primary",
      providerId,
      modelId,
      backendId,
      location,
      primary: true,
      attributes: Object.freeze({}),
    })
  }
  return dedupeSegments(output)
}

function placementResourceFit(
  sources: readonly JsonObject[],
): ResourceFitView | undefined {
  let requestedObject: JsonObject | undefined
  let capacityObject: JsonObject | undefined
  let allocatedObject: JsonObject | undefined
  let availableObject: JsonObject | undefined
  for (const source of sources) {
    requestedObject ??= objectForAliases(source, [
      "requested_resources",
      "resource_request",
      "resources",
    ])
    capacityObject ??= objectForAliases(source, [
      "capacity",
      "resource_capacity",
    ])
    allocatedObject ??= objectForAliases(source, [
      "allocated",
      "resource_allocated",
    ])
    availableObject ??= objectForAliases(source, [
      "available",
      "resource_available",
    ])
  }
  if (
    !requestedObject &&
    !capacityObject &&
    !allocatedObject &&
    !availableObject
  ) {
    return undefined
  }
  const requested = resourceVector(requestedObject)
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

function assessSla(
  sources: readonly JsonObject[],
  privacyClass: string,
  location: PlacementLocation,
  resourceFit: ResourceFitView | undefined,
  costEstimate: number | undefined,
  latencyEstimateMs: number | undefined,
): SlaAssessment {
  const budgetLimit = firstNumber(
    sources,
    "budget_limit",
    "cost_limit",
    "maximum_cost",
    "max_cost_usd",
  )
  const latencyLimitMs = firstNumber(
    sources,
    "latency_limit_ms",
    "maximum_latency_ms",
    "latency_sla_ms",
  )
  const requiredLocation = normalizeLocation(
    firstString(
      sources,
      "required_location",
      "location_constraint",
      "privacy_location",
    ),
  )
  const localOnly =
    firstBoolean(sources, "local_only", "device_only") === true ||
    ["secret", "sensitive", "private", "local_only"].includes(
      normalizeToken(privacyClass),
    )
  const edgeOnly =
    firstBoolean(sources, "edge_only") === true ||
    normalizeToken(privacyClass) === "edge_only"
  const costSatisfied =
    budgetLimit === undefined || costEstimate === undefined
      ? undefined
      : costEstimate <= budgetLimit
  const latencySatisfied =
    latencyLimitMs === undefined || latencyEstimateMs === undefined
      ? undefined
      : latencyEstimateMs <= latencyLimitMs
  let privacySatisfied = true
  if (requiredLocation !== "unknown" && requiredLocation !== location) {
    privacySatisfied = false
  }
  if (localOnly && !["device", "local"].includes(location)) {
    privacySatisfied = false
  }
  if (edgeOnly && location !== "edge") privacySatisfied = false
  const resourceSatisfied = resourceFit?.fits
  const violations: string[] = []
  if (costSatisfied === false) violations.push("cost_budget_exceeded")
  if (latencySatisfied === false) violations.push("latency_sla_exceeded")
  if (!privacySatisfied) violations.push("privacy_location_violated")
  if (resourceSatisfied === false) {
    violations.push(
      ...resourceFit!.missing.map((resource) => `resource_missing:${resource}`),
    )
  }
  return Object.freeze({
    budgetLimit,
    costEstimate,
    latencyLimitMs,
    latencyEstimateMs,
    costSatisfied,
    latencySatisfied,
    privacySatisfied,
    resourceSatisfied,
    satisfied: violations.length === 0,
    violations: Object.freeze(violations),
  })
}

function mergePlacement(
  older: PlacementView,
  newer: PlacementView,
): PlacementView {
  return {
    ...newer,
    modelSplit: Object.freeze(
      dedupeSegments([...older.modelSplit, ...newer.modelSplit]),
    ),
    changed: older.changed || newer.changed,
    previousPlacementId:
      newer.previousPlacementId ?? older.previousPlacementId,
    evidence: mergeEvidence(older.evidence, newer.evidence),
    sla: Object.freeze({
      ...older.sla,
      ...newer.sla,
      satisfied: older.sla.satisfied && newer.sla.satisfied,
      violations: Object.freeze(
        uniqueStrings([...older.sla.violations, ...newer.sla.violations]),
      ),
    }),
  }
}

function markPlacementChanges(placements: PlacementView[]): void {
  const byNode = new Map<string, PlacementView[]>()
  for (const placement of placements) {
    const key = placement.nodeId ?? `task:${placement.taskId}`
    const values = byNode.get(key) ?? []
    values.push(placement)
    byNode.set(key, values)
  }
  for (const values of byNode.values()) {
    values.sort(comparePlacement)
    let previous: PlacementView | undefined
    for (const placement of values) {
      const changed =
        Boolean(placement.previousPlacementId) ||
        (previous !== undefined &&
          (previous.location !== placement.location ||
            previous.workerId !== placement.workerId ||
            previous.backendId !== placement.backendId ||
            previous.providerId !== placement.providerId ||
            previous.modelId !== placement.modelId))
      if (changed !== placement.changed) {
        const index = placements.indexOf(placement)
        if (index >= 0) {
          placements[index] = {
            ...placement,
            changed,
            previousPlacementId:
              placement.previousPlacementId ?? previous?.placementId,
          }
        }
      }
      previous = placement
    }
  }
}

function hasPlacementSignal(source: JsonObject): boolean {
  return [
    "placement_id",
    "selected_location",
    "selected_backend",
    "provider_id",
    "model_id",
    "resource_decision",
    "model_split",
  ].some((key) => valueForAliases(source, [key]) !== undefined)
}

function dedupeSources(values: readonly PlacementSource[]): PlacementSource[] {
  const output: PlacementSource[] = []
  const seen = new Set<string>()
  for (const value of values) {
    const key = [
      value.record.eventId,
      value.placementId,
      value.graphRevision,
      value.commitRevision,
      hashKey(value.source),
    ].join("\0")
    if (seen.has(key)) continue
    seen.add(key)
    output.push(value)
  }
  return output
}

function dedupeSegments(
  values: readonly ModelSplitSegment[],
): ModelSplitSegment[] {
  const byId = new Map<string, ModelSplitSegment>()
  for (const value of values) {
    const key = value.id || `${value.role}:${value.providerId}:${value.modelId}`
    const existing = byId.get(key)
    byId.set(key, existing ? { ...existing, ...value } : value)
  }
  return [...byId.values()].sort(
    (left, right) =>
      Number(right.primary) - Number(left.primary) ||
      left.role.localeCompare(right.role) ||
      left.id.localeCompare(right.id),
  )
}

function compareSource(
  left: PlacementSource,
  right: PlacementSource,
): number {
  return (
    left.record.sequence - right.record.sequence ||
    left.graphRevision - right.graphRevision ||
    left.commitRevision - right.commitRevision ||
    left.placementId.localeCompare(right.placementId)
  )
}

function comparePlacementVersion(
  left: PlacementView,
  right: PlacementView,
): number {
  return (
    left.graphRevision - right.graphRevision ||
    left.commitRevision - right.commitRevision ||
    left.sequence - right.sequence ||
    left.revision - right.revision
  )
}

function comparePlacement(
  left: PlacementView,
  right: PlacementView,
): number {
  return (
    left.sequence - right.sequence ||
    left.revision - right.revision ||
    left.placementId.localeCompare(right.placementId)
  )
}
