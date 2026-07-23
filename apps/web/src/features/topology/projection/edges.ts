import type { JsonObject } from "../../../events/ingress/index.ts"
import type {
  EdgeKind,
  ProjectionRecord,
  TopologyEdgeView,
  TopologyMutationView,
  TopologyNodeView,
  TopologyProjectionContext,
} from "./contracts.ts"
import { evidenceForEntity, evidenceWithGraph, mergeEvidence } from "./evidence.ts"
import { isOpenWorldMutation, normalizeMutationKind } from "./nodes.ts"
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
  namedRecords,
  normalizeEntityState,
  normalizeToken,
  recordCandidates,
  recordLooksLike,
  uniqueStrings,
  valueForAliases,
} from "./record-reader.ts"

const EDGE_NAMES = [
  "edge",
  "graph_edge",
  "topology_edge",
  "dependency_edge",
  "before_edge",
  "after_edge",
  "replacement_edge",
]
const SNAPSHOT_NAMES = ["snapshot", "graph_snapshot", "graph_state", "graphState"]

interface EdgeCandidate {
  record: ProjectionRecord
  source: JsonObject
  edgeId: string
  sourceId: string
  targetId: string
  graphId?: string
  graphRevision: number
  commitRevision: number
  edgeKind: EdgeKind
  inferred: boolean
  mutationKind: string
  removed: boolean
  runtimeMutation: boolean
}

export function projectEdges(
  context: TopologyProjectionContext,
  nodes: readonly TopologyNodeView[],
  mutations: readonly TopologyMutationView[],
): TopologyEdgeView[] {
  const candidates: EdgeCandidate[] = []
  for (const record of context.records) {
    candidates.push(...explicitEdgeCandidates(record))
  }
  candidates.push(...dependencyEdgeCandidates(nodes, context))
  candidates.push(...parentEdgeCandidates(nodes, context))
  candidates.push(...namespaceEdgeCandidates(nodes, context))
  candidates.push(...mutationEdgeCandidates(context.records, mutations))
  candidates.sort(compareCandidate)
  const nodeIds = new Set(nodes.filter((node) => !node.removed).map((node) => node.id))
  const byId = new Map<string, TopologyEdgeView>()
  for (const candidate of candidates) {
    const view = edgeView(candidate, context, nodeIds)
    const existing = byId.get(view.id)
    if (!existing) {
      byId.set(view.id, view)
      continue
    }
    if (compareEdgeVersion(existing, view) <= 0) {
      byId.set(view.id, mergeEdge(existing, view))
    } else {
      byId.set(view.id, mergeEdge(view, existing))
    }
  }
  return [...byId.values()]
    .filter((edge) => context.options.includeRemoved || !edge.removed)
    .sort(compareEdge)
}

function explicitEdgeCandidates(record: ProjectionRecord): EdgeCandidate[] {
  const output: EdgeCandidate[] = []
  for (const source of namedRecords(record, EDGE_NAMES)) {
    const candidate = candidateFromRecord(record, source)
    if (candidate) output.push(candidate)
  }
  for (const snapshot of namedRecords(record, SNAPSHOT_NAMES)) {
    const graphId = firstString([snapshot], "graph_id", "graphId")
    const graphRevision =
      firstInteger([snapshot], "revision", "graph_revision") ?? 0
    const commitRevision =
      firstInteger([snapshot], "commit_revision", "committed_revision") ??
      graphRevision
    const values = valueForAliases(snapshot, ["edges"])
    if (!Array.isArray(values)) continue
    for (const source of values) {
      if (!isJsonObject(source)) continue
      const candidate = candidateFromRecord(record, source)
      if (!candidate) continue
      output.push({
        ...candidate,
        graphId,
        graphRevision,
        commitRevision,
      })
    }
  }
  const nested = findRecords(
    recordCandidates(record),
    (candidate, path) => {
      if (
        !recordLooksLike(
          candidate,
          ["relation", "source_node_id", "target_node_id", "source_id", "target_id"],
          ["edge_id"],
        )
      ) {
        return false
      }
      return !path.some((part) => /candidate|route/i.test(part))
    },
    4_000,
  )
  for (const source of nested) {
    const candidate = candidateFromRecord(record, source)
    if (candidate) output.push(candidate)
  }
  return dedupeCandidates(output)
}

function candidateFromRecord(
  record: ProjectionRecord,
  source: JsonObject,
): EdgeCandidate | undefined {
  const all = [source, ...recordCandidates(record)]
  const sourceId = firstString(
    [source],
    "source_node_id",
    "source_id",
    "from_node_id",
    "from",
    "source",
  )
  const targetId = firstString(
    [source],
    "target_node_id",
    "target_id",
    "to_node_id",
    "to",
    "target",
  )
  if (!sourceId || !targetId || sourceId === targetId) return undefined
  const relation =
    firstString([source], "relation", "edge_kind", "kind", "type") ??
    "explicit"
  const edgeId =
    firstString([source], "edge_id", "edgeId", "id") ??
    `edge:${sourceId}->${targetId}:${normalizeToken(relation)}`
  const mutationKind = normalizeMutationKind(
    firstString(all, "mutation_kind", "operation", "kind"),
  )
  return {
    record,
    source,
    edgeId,
    sourceId,
    targetId,
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
    edgeKind: edgeKind(relation, false),
    inferred: false,
    mutationKind,
    removed:
      record.removed ||
      mutationKind === "remove_edge" ||
      firstBoolean([source], "removed", "deleted") === true,
    runtimeMutation: isOpenWorldMutation(mutationKind),
  }
}

function dependencyEdgeCandidates(
  nodes: readonly TopologyNodeView[],
  context: TopologyProjectionContext,
): EdgeCandidate[] {
  const output: EdgeCandidate[] = []
  for (const node of nodes) {
    const record = recordForNode(context.records, node)
    if (!record) continue
    for (const dependency of node.dependencies) {
      output.push({
        record,
        source: {
          edge_id: `dependency:${dependency}->${node.id}`,
          source_node_id: dependency,
          target_node_id: node.id,
          relation: "depends_on",
        },
        edgeId: `dependency:${dependency}->${node.id}`,
        sourceId: dependency,
        targetId: node.id,
        graphId: node.evidence.graphIds.at(-1),
        graphRevision: node.graphRevision,
        commitRevision: node.commitRevision,
        edgeKind: "dependency",
        inferred: true,
        mutationKind: "set_node_dependencies",
        removed: node.removed,
        runtimeMutation: node.openWorldCreated || node.openWorldReplaced,
      })
    }
  }
  return output
}

function parentEdgeCandidates(
  nodes: readonly TopologyNodeView[],
  context: TopologyProjectionContext,
): EdgeCandidate[] {
  const output: EdgeCandidate[] = []
  for (const node of nodes) {
    if (!node.parentNodeId || node.parentNodeId === node.id) continue
    const record = recordForNode(context.records, node)
    if (!record) continue
    output.push({
      record,
      source: {
        edge_id: `parent:${node.parentNodeId}->${node.id}`,
        source_node_id: node.parentNodeId,
        target_node_id: node.id,
        relation: "parent",
      },
      edgeId: `parent:${node.parentNodeId}->${node.id}`,
      sourceId: node.parentNodeId,
      targetId: node.id,
      graphId: node.evidence.graphIds.at(-1),
      graphRevision: node.graphRevision,
      commitRevision: node.commitRevision,
      edgeKind: "parent",
      inferred: true,
      mutationKind: "unknown",
      removed: node.removed,
      runtimeMutation: node.openWorldCreated,
    })
  }
  return output
}

function namespaceEdgeCandidates(
  nodes: readonly TopologyNodeView[],
  context: TopologyProjectionContext,
): EdgeCandidate[] {
  const output: EdgeCandidate[] = []
  const firstByNamespace = new Map<string, TopologyNodeView>()
  for (const node of nodes) {
    if (!firstByNamespace.has(node.namespace)) {
      firstByNamespace.set(node.namespace, node)
    }
  }
  for (const node of nodes) {
    if (!node.subgraphId) continue
    const owner = firstByNamespace.get(node.namespace)
    if (!owner || owner.id === node.id) continue
    const record = recordForNode(context.records, node)
    if (!record) continue
    output.push({
      record,
      source: {
        edge_id: `namespace:${owner.id}->${node.id}`,
        source_node_id: owner.id,
        target_node_id: node.id,
        relation: "subgraph",
      },
      edgeId: `namespace:${owner.id}->${node.id}`,
      sourceId: owner.id,
      targetId: node.id,
      graphId: node.subgraphId,
      graphRevision: node.graphRevision,
      commitRevision: node.commitRevision,
      edgeKind: "namespace",
      inferred: true,
      mutationKind: "unknown",
      removed: node.removed,
      runtimeMutation: node.openWorldCreated,
    })
  }
  return output
}

function mutationEdgeCandidates(
  records: readonly ProjectionRecord[],
  mutations: readonly TopologyMutationView[],
): EdgeCandidate[] {
  const output: EdgeCandidate[] = []
  const recordsByEvent = new Map<string, ProjectionRecord>()
  for (const record of records) recordsByEvent.set(record.eventId, record)
  for (const mutation of mutations) {
    if (!["add_edge", "remove_edge", "replace_edge"].includes(mutation.kind)) {
      continue
    }
    const source = mutation.value
    const sourceId = firstString(
      [source],
      "source_node_id",
      "source_id",
      "from",
    )
    const targetId = firstString(
      [source],
      "target_node_id",
      "target_id",
      "to",
    )
    if (!sourceId || !targetId) continue
    const eventId = mutation.evidence.eventIds.at(-1)
    const record = eventId ? recordsByEvent.get(eventId) : undefined
    if (!record) continue
    output.push({
      record,
      source,
      edgeId: mutation.entityId,
      sourceId,
      targetId,
      graphId: mutation.graphId,
      graphRevision: mutation.committedRevision,
      commitRevision: mutation.committedRevision,
      edgeKind: edgeKind(
        firstString([source], "relation", "edge_kind") ?? "explicit",
        false,
      ),
      inferred: false,
      mutationKind: mutation.kind,
      removed: mutation.kind === "remove_edge",
      runtimeMutation: true,
    })
  }
  return output
}

function edgeView(
  candidate: EdgeCandidate,
  context: TopologyProjectionContext,
  nodeIds: ReadonlySet<string>,
): TopologyEdgeView {
  const { record, source } = candidate
  const all = [source, ...recordCandidates(record)]
  const relation =
    firstString([source], "relation", "edge_kind", "kind", "type") ??
    candidate.edgeKind
  const state = normalizeEntityState(
    firstString([source], "state", "status", "phase"),
    candidate.removed ? "removed" : undefined,
    record.lifecycle,
  )
  const evidence = evidenceWithGraph(
    evidenceForEntity(
      context.evidence,
      record,
      "edge",
      candidate.edgeId,
      candidate.graphId,
      candidate.graphRevision,
    ),
    candidate.graphId,
    candidate.graphRevision,
    [
      `edge:${candidate.edgeId}`,
      `node:${candidate.sourceId}`,
      `node:${candidate.targetId}`,
    ],
  )
  const condition = valueForAliases(source, ["condition", "when"])
  return {
    id: candidate.edgeId,
    taskId: record.taskId,
    runId: record.runId,
    kind: "edge",
    sequence: record.sequence,
    revision:
      firstInteger([source], "revision", "edge_revision") ?? record.revision,
    graphRevision: candidate.graphRevision,
    commitRevision: candidate.commitRevision,
    state,
    title:
      firstString([source], "title", "name", "label") ??
      `${candidate.sourceId} → ${candidate.targetId}`,
    summary:
      firstString([source], "summary", "reason") ??
      `${relation} topology edge`,
    namespace:
      firstString(all, "namespace", "checkpoint_ns") ?? "root",
    subgraphId: firstString(all, "subgraph_id", "graph_id"),
    branchId: firstString(all, "branch_id"),
    checkpointId:
      firstString(all, "checkpoint_id") ?? record.checkpointId,
    effective: record.effective,
    terminal: record.terminal || candidate.removed,
    pending: ["planned", "queued", "waiting"].includes(state),
    removed: candidate.removed,
    evidence,
    attributes: Object.freeze(
      compactAttributes([source], context.sensitiveFieldDrops),
    ),
    edgeKind: candidate.edgeKind,
    sourceId: candidate.sourceId,
    targetId: candidate.targetId,
    relation,
    weight: firstNumber([source], "weight", "score", "edge_weight"),
    requiredCapabilities: Object.freeze(
      firstStringArray(
        [source],
        "required_capabilities",
        "capability_refs",
      ),
    ),
    condition: Object.freeze(isJsonObject(condition) ? condition : {}),
    sourceMissing: !nodeIds.has(candidate.sourceId),
    targetMissing: !nodeIds.has(candidate.targetId),
    inferred: candidate.inferred,
    runtimeMutation: candidate.runtimeMutation,
    replacementEdgeId: firstString(
      [source],
      "replacement_edge_id",
      "replaced_by",
    ),
  }
}

function mergeEdge(
  older: TopologyEdgeView,
  newer: TopologyEdgeView,
): TopologyEdgeView {
  return {
    ...newer,
    requiredCapabilities: Object.freeze(
      uniqueStrings([
        ...older.requiredCapabilities,
        ...newer.requiredCapabilities,
      ]),
    ),
    sourceMissing: older.sourceMissing && newer.sourceMissing,
    targetMissing: older.targetMissing && newer.targetMissing,
    runtimeMutation: older.runtimeMutation || newer.runtimeMutation,
    evidence: mergeEvidence(older.evidence, newer.evidence),
  }
}

function recordForNode(
  records: readonly ProjectionRecord[],
  node: TopologyNodeView,
): ProjectionRecord | undefined {
  return (
    records.find(
      (record) =>
        record.domain === "node" &&
        (record.entityId === node.id || record.nodeId === node.id),
    ) ??
    records.find((record) => node.evidence.eventIds.includes(record.eventId))
  )
}

function edgeKind(value: string, inferred: boolean): EdgeKind {
  const normalized = normalizeToken(value)
  if (/depend|precede|handoff/.test(normalized)) return "dependency"
  if (/parent|child/.test(normalized)) return "parent"
  if (/namespace|subgraph/.test(normalized)) return "namespace"
  if (/route|dispatch/.test(normalized)) return "route"
  if (/placement|worker/.test(normalized)) return "placement"
  if (/checkpoint|resume/.test(normalized)) return "checkpoint"
  if (/branch|delta/.test(normalized)) return "branch"
  return inferred ? "dependency" : "explicit"
}

function dedupeCandidates(values: readonly EdgeCandidate[]): EdgeCandidate[] {
  const output: EdgeCandidate[] = []
  const seen = new Set<string>()
  for (const value of values) {
    const key = [
      value.edgeId,
      value.sourceId,
      value.targetId,
      value.graphRevision,
      value.commitRevision,
      value.record.eventId,
      value.mutationKind,
    ].join("\0")
    if (seen.has(key)) continue
    seen.add(key)
    output.push(value)
  }
  return output
}

function compareCandidate(
  left: EdgeCandidate,
  right: EdgeCandidate,
): number {
  return (
    left.record.sequence - right.record.sequence ||
    left.graphRevision - right.graphRevision ||
    left.commitRevision - right.commitRevision ||
    left.edgeId.localeCompare(right.edgeId)
  )
}

function compareEdgeVersion(
  left: TopologyEdgeView,
  right: TopologyEdgeView,
): number {
  return (
    left.graphRevision - right.graphRevision ||
    left.commitRevision - right.commitRevision ||
    left.sequence - right.sequence ||
    left.revision - right.revision
  )
}

function compareEdge(
  left: TopologyEdgeView,
  right: TopologyEdgeView,
): number {
  return (
    left.sequence - right.sequence ||
    left.sourceId.localeCompare(right.sourceId) ||
    left.targetId.localeCompare(right.targetId) ||
    left.id.localeCompare(right.id)
  )
}
