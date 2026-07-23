import type { JsonObject } from "../../../events/ingress/index.ts"
import type {
  ProjectionRecord,
  RequirementChangeView,
  RouteDecisionView,
  TopologyMutationView,
  TopologyNodeView,
  TopologyProjectionContext,
} from "./contracts.ts"
import { evidenceForEntity, evidenceWithGraph, mergeEvidence } from "./evidence.ts"
import {
  compactAttributes,
  findRecords,
  firstBoolean,
  firstInteger,
  firstString,
  firstStringArray,
  namedRecords,
  normalizeEntityState,
  normalizeToken,
  recordCandidates,
  recordLooksLike,
  stableKey,
  uniqueStrings,
  valueForAliases,
} from "./record-reader.ts"

const CHANGE_NAMES = [
  "requirement_change",
  "change_request",
  "requirement_update",
  "impact_analysis",
  "replan",
  "replan_decision",
]

interface ChangeSource {
  record: ProjectionRecord
  source: JsonObject
  changeId: string
  graphId?: string
  graphRevision: number
  commitRevision: number
}

export function projectRequirementChanges(
  context: TopologyProjectionContext,
  nodes: readonly TopologyNodeView[],
  routes: readonly RouteDecisionView[],
  mutations: readonly TopologyMutationView[],
): RequirementChangeView[] {
  const sources: ChangeSource[] = []
  for (const record of context.records) {
    sources.push(...changeSourcesForRecord(record))
  }
  sources.sort(compareSource)
  const byId = new Map<string, RequirementChangeView>()
  for (const source of sources) {
    const view = changeView(source, context, nodes, routes, mutations)
    const existing = byId.get(view.changeId)
    if (!existing) {
      byId.set(view.changeId, view)
      continue
    }
    const newer = compareChangeVersion(existing, view) <= 0 ? view : existing
    const older = newer === view ? existing : view
    byId.set(view.changeId, mergeChange(older, newer))
  }
  const changes = [...byId.values()].sort(compareChange)
  inferChangeRelationships(changes, nodes, routes, mutations, context)
  return changes
}

function changeSourcesForRecord(record: ProjectionRecord): ChangeSource[] {
  const output: ChangeSource[] = []
  for (const source of namedRecords(record, CHANGE_NAMES)) {
    const candidate = changeSource(record, source)
    if (candidate) output.push(candidate)
  }
  if (
    ["task", "recovery", "scheduler", "node"].includes(record.domain) &&
    hasChangeSignal(record.attributes)
  ) {
    const candidate = changeSource(record, record.attributes)
    if (candidate) output.push(candidate)
  }
  const nested = findRecords(
    recordCandidates(record),
    (candidate, path) =>
      recordLooksLike(candidate, [
        "change_id",
        "affected_node_ids",
        "superseded_node_ids",
        "needs_revision_node_ids",
        "replan_node_id",
        "requirement_text",
      ]) &&
      path.some((part) => /requirement|change|impact|replan|revision/i.test(part)),
    4_000,
  )
  for (const source of nested) {
    const candidate = changeSource(record, source)
    if (candidate) output.push(candidate)
  }
  return dedupeSources(output)
}

function changeSource(
  record: ProjectionRecord,
  source: JsonObject,
): ChangeSource | undefined {
  const all = [source, ...recordCandidates(record)]
  const explicitId = firstString(
    all,
    "change_id",
    "requirement_change_id",
    "change_request_id",
  )
  if (!explicitId && !hasChangeSignal(source)) return undefined
  const graphRevision =
    firstInteger(
      all,
      "graph_revision",
      "committed_revision",
      "topology_revision",
    ) ?? 0
  return {
    record,
    source,
    changeId: explicitId ?? `change:${record.eventId}`,
    graphId: firstString(all, "graph_id", "graphId"),
    graphRevision,
    commitRevision:
      firstInteger(all, "commit_revision", "committed_revision") ??
      graphRevision,
  }
}

function changeView(
  source: ChangeSource,
  context: TopologyProjectionContext,
  nodes: readonly TopologyNodeView[],
  routes: readonly RouteDecisionView[],
  mutations: readonly TopologyMutationView[],
): RequirementChangeView {
  const { record } = source
  const all = [source.source, ...recordCandidates(record)]
  const affectedNodeIds = uniqueStrings([
    ...firstStringArray(
      all,
      "affected_node_ids",
      "affected_nodes",
      "impacted_node_ids",
      "impacted_nodes",
    ),
    firstString(all, "affected_node_id", "node_id"),
  ])
  const supersededNodeIds = uniqueStrings([
    ...firstStringArray(
      all,
      "superseded_node_ids",
      "superseded_nodes",
      "invalidated_node_ids",
    ),
    ...nodes
      .filter((node) => node.affectedByChangeIds.includes(source.changeId))
      .filter((node) => node.state === "superseded" || Boolean(node.supersededBy))
      .map((node) => node.id),
  ])
  const needsRevisionNodeIds = uniqueStrings([
    ...firstStringArray(
      all,
      "needs_revision_node_ids",
      "revision_node_ids",
      "nodes_needing_revision",
    ),
    ...nodes
      .filter((node) => node.affectedByChangeIds.includes(source.changeId))
      .filter((node) => node.state === "needs_revision")
      .map((node) => node.id),
  ])
  const routeIds = uniqueStrings([
    ...firstStringArray(all, "route_ids", "affected_route_ids"),
    firstString(all, "route_id"),
    ...routes
      .filter(
        (route) =>
          affectedNodeIds.includes(route.nodeId ?? "") ||
          route.evidence.eventIds.includes(record.eventId),
      )
      .map((route) => route.routeId),
  ])
  const relatedMutationIds = mutations
    .filter(
      (mutation) =>
        mutation.kind === "requirement_changed" ||
        affectedNodeIds.includes(mutation.entityId) ||
        mutation.evidence.eventIds.includes(record.eventId),
    )
    .map((mutation) => mutation.id)
  const evidence = evidenceWithGraph(
    context.evidence.merge(
      evidenceForEntity(
        context.evidence,
        record,
        "requirement_change",
        source.changeId,
        source.graphId,
        source.graphRevision,
      ),
      context.evidence.forEventIds(
        context.evidence.relatedEventIds([record.eventId], 2_000),
      ),
    ),
    source.graphId,
    source.graphRevision,
    [
      `change:${source.changeId}`,
      ...affectedNodeIds.map((id) => `node:${id}`),
      ...routeIds.map((id) => `route:${id}`),
      ...relatedMutationIds.map((id) => `mutation:${id}`),
    ],
  )
  const replanNodeId = firstString(
    all,
    "replan_node_id",
    "replacement_node_id",
    "new_plan_node_id",
  )
  const localReplan =
    firstBoolean(all, "local_replan", "localized_replan") ??
    Boolean(replanNodeId || affectedNodeIds.length > 0)
  const state = normalizeEntityState(
    firstString(all, "status", "state", "phase"),
    record.lifecycle,
  )
  return {
    id: source.changeId,
    changeId: source.changeId,
    sourceEventId: record.eventId,
    taskId: record.taskId,
    runId: record.runId,
    kind: "requirement_change",
    sequence: record.sequence,
    revision:
      firstInteger(all, "revision", "change_revision", "version") ??
      record.revision,
    graphRevision: source.graphRevision,
    commitRevision: source.commitRevision,
    state,
    title:
      firstString(all, "title", "name") ??
      `Requirement change ${source.changeId}`,
    summary:
      firstString(all, "summary", "reason", "description") ?? record.summary,
    namespace:
      firstString(all, "namespace", "checkpoint_ns") ?? "requirements",
    subgraphId: firstString(all, "subgraph_id", "graph_id"),
    branchId: firstString(all, "branch_id"),
    checkpointId:
      firstString(all, "checkpoint_id") ?? record.checkpointId,
    effective: record.effective,
    terminal:
      record.terminal ||
      ["completed", "failed", "cancelled", "rejected"].includes(state),
    pending: ["planned", "queued", "waiting", "needs_revision"].includes(state),
    removed: record.removed,
    evidence,
    attributes: Object.freeze(
      compactAttributes([source.source], context.sensitiveFieldDrops),
    ),
    text:
      firstString(
        all,
        "requirement_text",
        "change_text",
        "new_requirement",
        "text",
        "description",
      ) ?? record.summary,
    affectedNodeIds: Object.freeze(affectedNodeIds),
    supersededNodeIds: Object.freeze(supersededNodeIds),
    needsRevisionNodeIds: Object.freeze(needsRevisionNodeIds),
    replanNodeId,
    decisionId: firstString(all, "decision_id"),
    resourceDecisionId: firstString(all, "resource_decision_id"),
    routeIds: Object.freeze(routeIds),
    localReplan,
    faultClassified:
      firstBoolean(all, "fault_classified", "classified_as_fault") ??
      Boolean(firstString(all, "fault_class", "fault_id")),
    causalClosureEventIds: Object.freeze(
      context.evidence.relatedEventIds([record.eventId], 2_000),
    ),
  }
}

function inferChangeRelationships(
  changes: RequirementChangeView[],
  nodes: readonly TopologyNodeView[],
  routes: readonly RouteDecisionView[],
  mutations: readonly TopologyMutationView[],
  context: TopologyProjectionContext,
): void {
  for (let index = 0; index < changes.length; index += 1) {
    const change = changes[index]!
    const causalIds = new Set(change.causalClosureEventIds)
    const mutationNodes = mutations
      .filter(
        (mutation) =>
          mutation.kind === "requirement_changed" ||
          mutation.evidence.eventIds.some((eventId) => causalIds.has(eventId)),
      )
      .map((mutation) => mutation.entityId)
    const causalNodes = nodes
      .filter((node) =>
        node.evidence.eventIds.some((eventId) => causalIds.has(eventId)),
      )
      .map((node) => node.id)
    const affectedNodeIds = uniqueStrings([
      ...change.affectedNodeIds,
      ...mutationNodes,
      ...causalNodes,
    ])
    const routeIds = uniqueStrings([
      ...change.routeIds,
      ...routes
        .filter(
          (route) =>
            affectedNodeIds.includes(route.nodeId ?? "") ||
            route.evidence.eventIds.some((eventId) => causalIds.has(eventId)),
        )
        .map((route) => route.routeId),
    ])
    const evidence = context.evidence.merge(
      change.evidence,
      ...nodes
        .filter((node) => affectedNodeIds.includes(node.id))
        .map((node) => node.evidence),
      ...routes
        .filter((route) => routeIds.includes(route.routeId))
        .map((route) => route.evidence),
    )
    changes[index] = {
      ...change,
      affectedNodeIds: Object.freeze(affectedNodeIds),
      routeIds: Object.freeze(routeIds),
      evidence,
    }
  }
}

function mergeChange(
  older: RequirementChangeView,
  newer: RequirementChangeView,
): RequirementChangeView {
  return {
    ...newer,
    affectedNodeIds: Object.freeze(
      uniqueStrings([...older.affectedNodeIds, ...newer.affectedNodeIds]),
    ),
    supersededNodeIds: Object.freeze(
      uniqueStrings([
        ...older.supersededNodeIds,
        ...newer.supersededNodeIds,
      ]),
    ),
    needsRevisionNodeIds: Object.freeze(
      uniqueStrings([
        ...older.needsRevisionNodeIds,
        ...newer.needsRevisionNodeIds,
      ]),
    ),
    routeIds: Object.freeze(
      uniqueStrings([...older.routeIds, ...newer.routeIds]),
    ),
    causalClosureEventIds: Object.freeze(
      uniqueStrings([
        ...older.causalClosureEventIds,
        ...newer.causalClosureEventIds,
      ]),
    ),
    localReplan: older.localReplan || newer.localReplan,
    faultClassified: older.faultClassified || newer.faultClassified,
    evidence: mergeEvidence(older.evidence, newer.evidence),
  }
}

function hasChangeSignal(source: JsonObject): boolean {
  const eventType = firstString([source], "event_type", "type")
  if (/requirement.*chang|change.*requirement|local.*replan/i.test(eventType ?? "")) {
    return true
  }
  const state = normalizeToken(firstString([source], "state", "status"))
  return (
    ["needs_revision", "superseded"].includes(state) ||
    [
      "change_id",
      "requirement_change_id",
      "affected_node_ids",
      "superseded_node_ids",
      "needs_revision_node_ids",
      "replan_node_id",
      "requirement_text",
    ].some((key) => valueForAliases(source, [key]) !== undefined)
  )
}

function dedupeSources(values: readonly ChangeSource[]): ChangeSource[] {
  const output: ChangeSource[] = []
  const seen = new Set<string>()
  for (const value of values) {
    const key = [
      value.record.eventId,
      value.changeId,
      value.graphRevision,
      value.commitRevision,
      stableKey(value.source),
    ].join("\0")
    if (seen.has(key)) continue
    seen.add(key)
    output.push(value)
  }
  return output
}

function compareSource(left: ChangeSource, right: ChangeSource): number {
  return (
    left.record.sequence - right.record.sequence ||
    left.graphRevision - right.graphRevision ||
    left.commitRevision - right.commitRevision ||
    left.changeId.localeCompare(right.changeId)
  )
}

function compareChangeVersion(
  left: RequirementChangeView,
  right: RequirementChangeView,
): number {
  return (
    left.graphRevision - right.graphRevision ||
    left.commitRevision - right.commitRevision ||
    left.sequence - right.sequence ||
    left.revision - right.revision
  )
}

function compareChange(
  left: RequirementChangeView,
  right: RequirementChangeView,
): number {
  return (
    left.sequence - right.sequence ||
    left.revision - right.revision ||
    left.changeId.localeCompare(right.changeId)
  )
}
