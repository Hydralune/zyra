import type { JsonObject } from "../../../events/ingress/index.ts"
import type {
  NamespaceView,
  ProjectionRecord,
  TopologyEvidenceResolver,
  TopologyMutationKind,
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
  hashKey,
  isJsonObject,
  mergedStringArrays,
  namedRecords,
  normalizeEntityState,
  normalizeKey,
  normalizeToken,
  recordCandidates,
  recordLooksLike,
  uniqueNumbers,
  uniqueStrings,
  valueForAliases,
} from "./record-reader.ts"

const NODE_RECORD_NAMES = [
  "node",
  "plan_node",
  "planNode",
  "topology_node",
  "graph_node",
  "replacement_node",
  "before_node",
  "after_node",
]
const SNAPSHOT_NAMES = [
  "snapshot",
  "graph_snapshot",
  "graphState",
  "graph_state",
]
const MUTATION_NAMES = [
  "mutation",
  "mutations",
  "graph_mutation",
  "graph_mutations",
  "delta",
  "branch_delta",
]

interface NodeCandidate {
  record: ProjectionRecord
  source: JsonObject
  nodeId: string
  graphId?: string
  graphRevision: number
  commitRevision: number
  mutationKind: TopologyMutationKind
  openWorld: boolean
  removed: boolean
  replaced: boolean
}

export function projectNodes(context: TopologyProjectionContext): {
  nodes: TopologyNodeView[]
  mutations: TopologyMutationView[]
  namespaces: NamespaceView[]
} {
  const candidates: NodeCandidate[] = []
  const mutations: TopologyMutationView[] = []
  for (const record of context.records) {
    candidates.push(...nodeCandidates(record))
    mutations.push(...mutationViews(record, context.evidence))
  }
  candidates.sort(compareNodeCandidate)
  const nodes = mergeNodeCandidates(candidates, context)
  attachWorkerRelationships(nodes, context.records)
  attachRouteRelationships(nodes, context.records)
  attachChangeRelationships(nodes, context.records)
  finalizeMissingDependencies(nodes)
  const namespaces = buildNamespaces(nodes, context)
  mutations.sort(compareMutation)
  return { nodes, mutations: dedupeMutations(mutations), namespaces }
}

function nodeCandidates(record: ProjectionRecord): NodeCandidate[] {
  const output: NodeCandidate[] = []
  if (record.domain === "node") {
    const sources = [
      ...namedRecords(record, NODE_RECORD_NAMES),
      record.attributes,
    ]
    const source =
      sources.find((candidate) => nodeIdentity(candidate) === record.entityId) ??
      sources[0] ??
      record.attributes
    output.push(candidateFrom(record, source, record.entityId))
  }
  for (const snapshot of namedRecords(record, SNAPSHOT_NAMES)) {
    const graphId = firstString([snapshot], "graph_id", "graphId")
    const graphRevision =
      firstInteger([snapshot], "revision", "graph_revision") ?? 0
    const commitRevision =
      firstInteger([snapshot], "commit_revision", "committed_revision") ??
      graphRevision
    const nodes = valueForAliases(snapshot, ["nodes"])
    if (!Array.isArray(nodes)) continue
    for (const value of nodes) {
      if (!isJsonObject(value)) continue
      const nodeId = nodeIdentity(value)
      if (!nodeId) continue
      output.push({
        ...candidateFrom(record, value, nodeId),
        graphId,
        graphRevision,
        commitRevision,
      })
    }
  }
  const nested = findRecords(
    recordCandidates(record),
    (candidate, path) => {
      if (!nodeIdentity(candidate)) return false
      const tail = normalizeToken(path.at(-1))
      if (tail.includes("candidate") || tail.includes("worker")) return false
      return recordLooksLike(
        candidate,
        ["role", "capabilities", "dependencies", "depends_on", "state", "status"],
        ["node_id"],
      )
    },
    2_000,
  )
  for (const source of nested) {
    const nodeId = nodeIdentity(source)
    if (!nodeId) continue
    output.push(candidateFrom(record, source, nodeId))
  }
  for (const mutation of mutationRecords(record)) {
    const kind = normalizeMutationKind(
      firstString([mutation], "kind", "operation", "mutation_kind"),
    )
    if (!kind.includes("node")) continue
    const value =
      (valueForAliases(mutation, ["value", "node", "replacement"]) as
        | JsonObject
        | undefined) ?? mutation
    const entityId =
      firstString([mutation], "entity_id", "node_id", "target_id") ??
      nodeIdentity(value)
    if (!entityId) continue
    const candidate = candidateFrom(record, value, entityId)
    candidate.mutationKind = kind
    candidate.openWorld = isOpenWorldMutation(kind)
    candidate.removed = kind === "remove_node"
    candidate.replaced = kind === "replace_node"
    candidate.graphId =
      firstString([mutation, ...recordCandidates(record)], "graph_id") ??
      candidate.graphId
    candidate.graphRevision =
      firstInteger(
        [mutation, ...recordCandidates(record)],
        "committed_revision",
        "graph_revision",
      ) ?? candidate.graphRevision
    candidate.commitRevision =
      firstInteger(
        [mutation, ...recordCandidates(record)],
        "commit_revision",
        "committed_revision",
      ) ?? candidate.commitRevision
    output.push(candidate)
  }
  return dedupeCandidates(output)
}

function candidateFrom(
  record: ProjectionRecord,
  source: JsonObject,
  nodeId: string,
): NodeCandidate {
  const all = [source, ...recordCandidates(record)]
  const mutationKind = normalizeMutationKind(
    firstString(all, "mutation_kind", "operation", "kind"),
  )
  return {
    record,
    source,
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
    mutationKind,
    openWorld: isOpenWorldMutation(mutationKind),
    removed:
      record.removed ||
      mutationKind === "remove_node" ||
      firstBoolean(all, "removed", "deleted", "tombstoned") === true,
    replaced:
      mutationKind === "replace_node" ||
      firstBoolean(all, "replaced") === true,
  }
}

function mergeNodeCandidates(
  candidates: readonly NodeCandidate[],
  context: TopologyProjectionContext,
): TopologyNodeView[] {
  const byId = new Map<string, TopologyNodeView>()
  for (const candidate of candidates) {
    const existing = byId.get(candidate.nodeId)
    const next = nodeView(candidate, existing, context)
    if (!existing || compareNodeVersion(existing, next) <= 0) {
      byId.set(candidate.nodeId, mergeNode(existing, next))
    } else {
      byId.set(candidate.nodeId, mergeNode(next, existing))
    }
  }
  const values = [...byId.values()]
    .filter((node) => context.options.includeRemoved || !node.removed)
    .sort(compareNode)
  return values
}

function nodeView(
  candidate: NodeCandidate,
  existing: TopologyNodeView | undefined,
  context: TopologyProjectionContext,
): TopologyNodeView {
  const { record, source } = candidate
  const all = [source, ...recordCandidates(record)]
  const metadata = isJsonObject(valueForAliases(source, ["metadata"]))
    ? (valueForAliases(source, ["metadata"]) as JsonObject)
    : {}
  const labels = isJsonObject(valueForAliases(source, ["labels"]))
    ? (valueForAliases(source, ["labels"]) as JsonObject)
    : {}
  const namespace =
    firstString(
      [source, metadata, labels],
      "namespace",
      "checkpoint_ns",
      "graph_namespace",
      "scope",
    ) ??
    existing?.namespace ??
    namespaceFromNodeId(candidate.nodeId)
  const state = normalizeEntityState(
    firstString([source], "state", "status", "phase"),
    record.lifecycle,
    candidate.removed ? "removed" : undefined,
  )
  const evidence = evidenceWithGraph(
    evidenceForEntity(
      context.evidence,
      record,
      "node",
      candidate.nodeId,
      candidate.graphId,
      candidate.graphRevision,
    ),
    candidate.graphId,
    candidate.graphRevision,
    [`node:${candidate.nodeId}`],
  )
  const capabilities = uniqueStrings([
    ...(existing?.capabilities ?? []),
    ...mergedStringArrays(
      [source, metadata],
      "capabilities",
      "capability_refs",
      "required_capabilities",
    ),
  ])
  const dependencies = uniqueStrings([
    ...(existing?.dependencies ?? []),
    ...mergedStringArrays(
      [source, metadata],
      "dependencies",
      "depends_on",
      "dependency_ids",
    ),
  ])
  const artifactIds = uniqueStrings([
    ...(existing?.artifactIds ?? []),
    ...recordCandidates(record).flatMap((item) =>
      firstStringArray(
        [item],
        "artifact_ids",
        "artifact_refs",
        "committed_refs",
      ),
    ),
  ])
  return {
    id: candidate.nodeId,
    taskId: record.taskId,
    runId: record.runId,
    kind: "node",
    sequence: Math.max(existing?.sequence ?? 0, record.sequence),
    revision: Math.max(
      existing?.revision ?? 0,
      firstInteger([source], "revision", "node_revision") ?? record.revision,
    ),
    graphRevision: Math.max(
      existing?.graphRevision ?? 0,
      candidate.graphRevision,
    ),
    commitRevision: Math.max(
      existing?.commitRevision ?? 0,
      candidate.commitRevision,
    ),
    state,
    title:
      firstString([source], "title", "name", "label") ??
      existing?.title ??
      record.title,
    summary:
      firstString([source], "summary", "description") ??
      existing?.summary ??
      record.summary,
    namespace,
    subgraphId:
      firstString([source, metadata], "subgraph_id", "subgraph", "graph_id") ??
      existing?.subgraphId,
    branchId:
      firstString(all, "branch_id", "branchId") ?? existing?.branchId,
    checkpointId:
      firstString(all, "checkpoint_id", "checkpointId") ??
      record.checkpointId ??
      existing?.checkpointId,
    effective: record.effective,
    terminal:
      record.terminal ||
      ["succeeded", "completed", "failed", "cancelled", "superseded", "removed"].includes(
        state,
      ),
    pending: ["planned", "ready", "queued", "blocked", "waiting"].includes(state),
    removed: candidate.removed,
    evidence,
    attributes: Object.freeze(
      compactAttributes([source, metadata, labels], context.sensitiveFieldDrops),
    ),
    role:
      firstString([source, metadata], "role", "agent_role", "worker_role") ??
      existing?.role ??
      "",
    capabilities: Object.freeze(capabilities),
    dependencies: Object.freeze(dependencies),
    childNodeIds: Object.freeze(
      uniqueStrings([
        ...(existing?.childNodeIds ?? []),
        ...mergedStringArrays([source], "child_node_ids", "children"),
      ]),
    ),
    parentNodeId:
      firstString(
        [source, metadata],
        "parent_node_id",
        "parent_id",
        "parentNodeId",
      ) ?? existing?.parentNodeId,
    workerIds: Object.freeze([...(existing?.workerIds ?? [])]),
    routeIds: Object.freeze([...(existing?.routeIds ?? [])]),
    placementIds: Object.freeze([...(existing?.placementIds ?? [])]),
    artifactIds: Object.freeze(artifactIds),
    logicalTaskId:
      firstString([source], "logical_task_id", "logicalTaskId") ??
      existing?.logicalTaskId,
    physicalAttemptRef:
      firstString([source], "physical_attempt_ref", "attempt_id") ??
      existing?.physicalAttemptRef,
    workerLeaseRef:
      firstString([source], "worker_lease_ref", "lease_id") ??
      existing?.workerLeaseRef,
    workspaceRef:
      firstString([source], "workspace_ref", "workspace_id") ??
      existing?.workspaceRef,
    backendRouteRef:
      firstString([source], "backend_route_ref", "route_id") ??
      existing?.backendRouteRef,
    supersededBy:
      firstString(
        [source, metadata],
        "superseded_by",
        "superseded_by_requirement_change",
      ) ?? existing?.supersededBy,
    replacementNodeId:
      firstString(
        [source, metadata],
        "replacement_node_id",
        "replaced_by",
      ) ?? existing?.replacementNodeId,
    affectedByChangeIds: Object.freeze(
      uniqueStrings([
        ...(existing?.affectedByChangeIds ?? []),
        ...mergedStringArrays(
          [source, metadata],
          "requirement_change_ids",
          "affected_by_change_ids",
        ),
      ]),
    ),
    missingDependencyIds: Object.freeze([]),
    openWorldCreated:
      existing?.openWorldCreated === true ||
      candidate.mutationKind === "add_node" ||
      (candidate.openWorld && !candidate.removed && !candidate.replaced),
    openWorldRemoved:
      existing?.openWorldRemoved === true ||
      candidate.mutationKind === "remove_node",
    openWorldReplaced:
      existing?.openWorldReplaced === true ||
      candidate.mutationKind === "replace_node",
  }
}

function mergeNode(
  older: TopologyNodeView | undefined,
  newer: TopologyNodeView,
): TopologyNodeView {
  if (!older) return newer
  return {
    ...newer,
    capabilities: Object.freeze(
      uniqueStrings([...older.capabilities, ...newer.capabilities]),
    ),
    dependencies: Object.freeze(
      uniqueStrings([...older.dependencies, ...newer.dependencies]),
    ),
    childNodeIds: Object.freeze(
      uniqueStrings([...older.childNodeIds, ...newer.childNodeIds]),
    ),
    workerIds: Object.freeze(uniqueStrings([...older.workerIds, ...newer.workerIds])),
    routeIds: Object.freeze(uniqueStrings([...older.routeIds, ...newer.routeIds])),
    placementIds: Object.freeze(
      uniqueStrings([...older.placementIds, ...newer.placementIds]),
    ),
    artifactIds: Object.freeze(
      uniqueStrings([...older.artifactIds, ...newer.artifactIds]),
    ),
    affectedByChangeIds: Object.freeze(
      uniqueStrings([
        ...older.affectedByChangeIds,
        ...newer.affectedByChangeIds,
      ]),
    ),
    openWorldCreated: older.openWorldCreated || newer.openWorldCreated,
    openWorldRemoved: older.openWorldRemoved || newer.openWorldRemoved,
    openWorldReplaced: older.openWorldReplaced || newer.openWorldReplaced,
    evidence: mergeEvidence(older.evidence, newer.evidence),
  }
}

function attachWorkerRelationships(
  nodes: TopologyNodeView[],
  records: readonly ProjectionRecord[],
): void {
  const byId = new Map(nodes.map((node) => [node.id, node]))
  const workerIds = new Map<string, string[]>()
  for (const record of records) {
    if (record.domain !== "worker") continue
    const nodeId =
      record.nodeId ??
      firstString(recordCandidates(record), "node_id", "graph_node_id")
    if (!nodeId || !byId.has(nodeId)) continue
    appendRelation(workerIds, nodeId, record.entityId)
  }
  replaceRelations(nodes, workerIds, "workerIds")
}

function attachRouteRelationships(
  nodes: TopologyNodeView[],
  records: readonly ProjectionRecord[],
): void {
  const byId = new Map(nodes.map((node) => [node.id, node]))
  const routes = new Map<string, string[]>()
  const placements = new Map<string, string[]>()
  for (const record of records) {
    const candidates = recordCandidates(record)
    const nodeId =
      record.nodeId ??
      firstString(candidates, "node_id", "graph_node_id", "target_node_id")
    if (!nodeId || !byId.has(nodeId)) continue
    const routeId = firstString(
      candidates,
      "route_id",
      "backend_route_id",
      "decision_id",
    )
    const placementId = firstString(
      candidates,
      "placement_id",
      "resource_decision_id",
    )
    if (routeId) appendRelation(routes, nodeId, routeId)
    if (placementId) appendRelation(placements, nodeId, placementId)
  }
  replaceRelations(nodes, routes, "routeIds")
  replaceRelations(nodes, placements, "placementIds")
}

function attachChangeRelationships(
  nodes: TopologyNodeView[],
  records: readonly ProjectionRecord[],
): void {
  const byId = new Map(nodes.map((node) => [node.id, node]))
  const affected = new Map<string, string[]>()
  for (const record of records) {
    const candidates = recordCandidates(record)
    const changeId = firstString(
      candidates,
      "change_id",
      "requirement_change_id",
      "source_event_id",
      "event_id",
    )
    const nodeIds = mergedStringArrays(
      candidates,
      "affected_node_ids",
      "superseded_node_ids",
      "needs_revision_node_ids",
    )
    if (!changeId) continue
    for (const nodeId of nodeIds) {
      if (byId.has(nodeId)) appendRelation(affected, nodeId, changeId)
    }
  }
  replaceRelations(nodes, affected, "affectedByChangeIds")
}

function finalizeMissingDependencies(nodes: TopologyNodeView[]): void {
  const ids = new Set(nodes.filter((node) => !node.removed).map((node) => node.id))
  for (let index = 0; index < nodes.length; index += 1) {
    const node = nodes[index]!
    nodes[index] = {
      ...node,
      missingDependencyIds: Object.freeze(
        node.dependencies.filter((dependency) => !ids.has(dependency)).sort(),
      ),
    }
  }
}

function replaceRelations(
  nodes: TopologyNodeView[],
  relations: ReadonlyMap<string, readonly string[]>,
  field: "workerIds" | "routeIds" | "placementIds" | "affectedByChangeIds",
): void {
  for (let index = 0; index < nodes.length; index += 1) {
    const node = nodes[index]!
    const values = relations.get(node.id)
    if (!values) continue
    nodes[index] = {
      ...node,
      [field]: Object.freeze(uniqueStrings([...node[field], ...values]).sort()),
    }
  }
}

function appendRelation(
  relations: Map<string, string[]>,
  source: string,
  target: string,
): void {
  const values = relations.get(source) ?? []
  if (!values.includes(target)) values.push(target)
  relations.set(source, values)
}

function buildNamespaces(
  nodes: readonly TopologyNodeView[],
  context: TopologyProjectionContext,
): NamespaceView[] {
  const nodeIdsByNamespace = new Map<string, string[]>()
  for (const node of nodes) {
    const namespace = node.namespace || "root"
    appendRelation(nodeIdsByNamespace, namespace, node.id)
    for (const ancestor of namespaceAncestors(namespace)) {
      if (!nodeIdsByNamespace.has(ancestor)) nodeIdsByNamespace.set(ancestor, [])
    }
  }
  const children = new Map<string, string[]>()
  for (const namespace of nodeIdsByNamespace.keys()) {
    const parent = parentNamespace(namespace)
    if (parent) appendRelation(children, parent, namespace)
  }
  return [...nodeIdsByNamespace.entries()]
    .map(([namespaceId, nodeIds]) => {
      const records = nodes.filter((node) => nodeIds.includes(node.id))
      const evidence = context.evidence.merge(
        ...records.map((node) => node.evidence),
      )
      const sequence = Math.max(0, ...records.map((node) => node.sequence))
      const revision = Math.max(0, ...records.map((node) => node.revision))
      const graphRevision = Math.max(
        0,
        ...records.map((node) => node.graphRevision),
      )
      return {
        id: namespaceId,
        namespaceId,
        taskId: context.taskId,
        runId: records[0]?.runId ?? "",
        kind: "namespace" as const,
        sequence,
        revision,
        graphRevision,
        commitRevision: Math.max(
          0,
          ...records.map((node) => node.commitRevision),
        ),
        state: records.some((node) => node.state === "running")
          ? ("running" as const)
          : ("planned" as const),
        title: namespaceId,
        summary: `${nodeIds.length} topology node(s)`,
        namespace: parentNamespace(namespaceId) ?? "",
        effective: records.some((node) => node.effective),
        terminal: records.length > 0 && records.every((node) => node.terminal),
        pending: records.some((node) => node.pending),
        removed: records.length > 0 && records.every((node) => node.removed),
        evidence,
        attributes: Object.freeze({}),
        parentNamespaceId: parentNamespace(namespaceId),
        nodeIds: Object.freeze([...nodeIds].sort()),
        childNamespaceIds: Object.freeze(
          [...(children.get(namespaceId) ?? [])].sort(),
        ),
        depth: namespaceDepth(namespaceId),
      }
    })
    .sort(
      (left, right) =>
        left.depth - right.depth ||
        left.namespaceId.localeCompare(right.namespaceId),
    )
}

function mutationViews(
  record: ProjectionRecord,
  evidence: TopologyEvidenceResolver,
): TopologyMutationView[] {
  const output: TopologyMutationView[] = []
  for (const source of mutationRecords(record)) {
    const kind = normalizeMutationKind(
      firstString([source], "kind", "operation", "mutation_kind"),
    )
    const entityId =
      firstString(
        [source],
        "entity_id",
        "node_id",
        "edge_id",
        "target_id",
        "key",
      ) ?? record.entityId
    const graphId = firstString(
      [source, ...recordCandidates(record)],
      "graph_id",
      "graphId",
    )
    const baseRevision =
      firstInteger([source], "base_revision", "expected_revision") ?? 0
    const committedRevision =
      firstInteger(
        [source, ...recordCandidates(record)],
        "committed_revision",
        "graph_revision",
        "commit_revision",
      ) ?? 0
    const mutationId =
      firstString([source], "mutation_id", "entry_id", "id") ??
      hashKey([
        record.eventId,
        kind,
        entityId,
        baseRevision,
        committedRevision,
      ])
    const value = valueForAliases(source, ["value"])
    output.push({
      id: mutationId,
      taskId: record.taskId,
      graphId,
      branchId: firstString([source], "branch_id"),
      deltaId: firstString([source], "delta_id"),
      entityId,
      kind,
      sequence:
        firstInteger([source], "sequence", "producer_sequence") ??
        record.sequence,
      baseRevision,
      committedRevision,
      expectedEntityRevision: firstInteger(
        [source],
        "expected_entity_revision",
      ),
      effective:
        firstBoolean([source], "effective", "applied") ?? record.effective,
      openWorld: isOpenWorldMutation(kind),
      beforeDigest: firstString([source], "before_digest", "expected_digest"),
      afterDigest: firstString([source], "after_digest", "content_digest"),
      readSet: Object.freeze(
        firstStringArray([source], "read_set", "readSet"),
      ),
      writeSet: Object.freeze(
        firstStringArray([source], "write_set", "writeSet"),
      ),
      value: Object.freeze(isJsonObject(value) ? value : {}),
      evidence: evidenceWithGraph(
        evidence.forRecord(record),
        graphId,
        committedRevision,
        [`mutation:${mutationId}`, `${kind}:${entityId}`],
      ),
    })
  }
  return output
}

function mutationRecords(record: ProjectionRecord): JsonObject[] {
  const named = namedRecords(record, MUTATION_NAMES)
  const nested = findRecords(
    recordCandidates(record),
    (candidate, path) => {
      const kind = firstString([candidate], "kind", "operation", "mutation_kind")
      if (!kind) return false
      const normalized = normalizeToken(kind)
      if (
        !normalized.includes("node") &&
        !normalized.includes("edge") &&
        !normalized.includes("graph") &&
        !normalized.includes("route") &&
        !normalized.includes("placement") &&
        !normalized.includes("checkpoint") &&
        !normalized.includes("branch")
      ) {
        return false
      }
      return (
        path.some((part) => /mutation|delta|entry/i.test(part)) ||
        "entity_id" in candidate ||
        "write_set" in candidate
      )
    },
    4_000,
  )
  return uniqueObjectRecords([...named, ...nested])
}

export function normalizeMutationKind(
  value: string | undefined,
): TopologyMutationKind {
  const normalized = normalizeToken(value)
  const direct: TopologyMutationKind[] = [
    "add_node",
    "remove_node",
    "replace_node",
    "set_node_role",
    "set_node_capabilities",
    "set_node_dependencies",
    "add_edge",
    "remove_edge",
    "replace_edge",
    "set_graph_metadata",
    "remove_graph_metadata",
    "route_selected",
    "route_changed",
    "placement_changed",
    "requirement_changed",
    "checkpoint_committed",
    "branch_committed",
    "branch_rebased",
    "branch_rejected",
  ]
  const exact = direct.find((candidate) => candidate === normalized)
  if (exact) return exact
  if (/node.*(?:add|create)|(?:add|create).*node/.test(normalized)) {
    return "add_node"
  }
  if (/node.*(?:remove|delete)|(?:remove|delete).*node/.test(normalized)) {
    return "remove_node"
  }
  if (/node.*replace|replace.*node/.test(normalized)) return "replace_node"
  if (/role/.test(normalized)) return "set_node_role"
  if (/capabilit/.test(normalized)) return "set_node_capabilities"
  if (/dependenc/.test(normalized)) return "set_node_dependencies"
  if (/edge.*(?:add|create)|(?:add|create).*edge/.test(normalized)) {
    return "add_edge"
  }
  if (/edge.*(?:remove|delete)|(?:remove|delete).*edge/.test(normalized)) {
    return "remove_edge"
  }
  if (/edge.*replace|replace.*edge/.test(normalized)) return "replace_edge"
  if (/route.*select/.test(normalized)) return "route_selected"
  if (/route.*change|reroute|failover/.test(normalized)) return "route_changed"
  if (/placement.*change/.test(normalized)) return "placement_changed"
  if (/requirement.*change/.test(normalized)) return "requirement_changed"
  if (/checkpoint.*commit/.test(normalized)) return "checkpoint_committed"
  if (/branch.*rebase/.test(normalized)) return "branch_rebased"
  if (/branch.*(?:reject|conflict)/.test(normalized)) return "branch_rejected"
  if (/branch.*commit/.test(normalized)) return "branch_committed"
  return "unknown"
}

export function isOpenWorldMutation(kind: TopologyMutationKind): boolean {
  return [
    "add_node",
    "remove_node",
    "replace_node",
    "set_node_role",
    "set_node_capabilities",
    "set_node_dependencies",
    "add_edge",
    "remove_edge",
    "replace_edge",
  ].includes(kind)
}

function nodeIdentity(source: JsonObject): string | undefined {
  return firstString([source], "node_id", "nodeId", "id")
}

function dedupeCandidates(values: readonly NodeCandidate[]): NodeCandidate[] {
  const output: NodeCandidate[] = []
  const seen = new Set<string>()
  for (const value of values) {
    const key = [
      value.record.id,
      value.nodeId,
      value.graphRevision,
      value.commitRevision,
      value.mutationKind,
      value.removed,
      hashKey(value.source),
    ].join(":")
    if (seen.has(key)) continue
    seen.add(key)
    output.push(value)
  }
  return output
}

function dedupeMutations(
  values: readonly TopologyMutationView[],
): TopologyMutationView[] {
  const byId = new Map<string, TopologyMutationView>()
  for (const value of values) {
    const existing = byId.get(value.id)
    if (!existing || compareMutation(existing, value) <= 0) {
      byId.set(value.id, value)
    }
  }
  return [...byId.values()].sort(compareMutation)
}

function uniqueObjectRecords(values: readonly JsonObject[]): JsonObject[] {
  const output: JsonObject[] = []
  const seen = new Set<string>()
  for (const value of values) {
    const key = hashKey(value)
    if (seen.has(key)) continue
    seen.add(key)
    output.push(value)
  }
  return output
}

function compareNodeCandidate(
  left: NodeCandidate,
  right: NodeCandidate,
): number {
  return (
    left.record.sequence - right.record.sequence ||
    left.graphRevision - right.graphRevision ||
    left.commitRevision - right.commitRevision ||
    left.nodeId.localeCompare(right.nodeId) ||
    left.record.id.localeCompare(right.record.id)
  )
}

function compareNodeVersion(
  left: TopologyNodeView,
  right: TopologyNodeView,
): number {
  return (
    left.graphRevision - right.graphRevision ||
    left.commitRevision - right.commitRevision ||
    left.sequence - right.sequence ||
    left.revision - right.revision
  )
}

function compareNode(
  left: TopologyNodeView,
  right: TopologyNodeView,
): number {
  return (
    left.namespace.localeCompare(right.namespace) ||
    left.sequence - right.sequence ||
    left.id.localeCompare(right.id)
  )
}

function compareMutation(
  left: TopologyMutationView,
  right: TopologyMutationView,
): number {
  return (
    left.sequence - right.sequence ||
    left.committedRevision - right.committedRevision ||
    left.id.localeCompare(right.id)
  )
}

function namespaceFromNodeId(nodeId: string): string {
  const separators = ["::", "/", ":"]
  for (const separator of separators) {
    const position = nodeId.lastIndexOf(separator)
    if (position > 0) return nodeId.slice(0, position)
  }
  return "root"
}

function namespaceAncestors(namespace: string): string[] {
  const parts = namespace.split(/[/:]+/).filter(Boolean)
  const output: string[] = []
  for (let index = 1; index <= parts.length; index += 1) {
    output.push(parts.slice(0, index).join("/"))
  }
  if (!output.includes("root")) output.unshift("root")
  return uniqueStrings(output)
}

function parentNamespace(namespace: string): string | undefined {
  if (!namespace || namespace === "root") return undefined
  const parts = namespace.split(/[/:]+/).filter(Boolean)
  if (parts.length <= 1) return "root"
  return parts.slice(0, -1).join("/")
}

function namespaceDepth(namespace: string): number {
  if (!namespace || namespace === "root") return 0
  return namespace.split(/[/:]+/).filter(Boolean).length
}
