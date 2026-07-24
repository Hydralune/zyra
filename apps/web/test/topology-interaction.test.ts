import { describe, expect, test } from "bun:test"
import type {
  BranchConflictView,
  BranchView,
  CheckpointView,
  PlacementView,
  RequirementChangeView,
  RouteDecisionView,
  TopologyEdgeView,
  TopologyEvidence,
  TopologyNodeView,
  TopologyProjectionView,
} from "../src/features/topology/projection/index.ts"
import {
  TopologyWorkbenchController,
  buildAccessibleGraph,
  buildMinimapProjection,
  focusRepair,
  nextAccessibleNode,
  selectionBoxNodeIds,
  topologyControlTransport,
  topologyFilterFacets,
} from "../src/features/topology/view/index.ts"

const TASK_ID = "task-topology-interaction"
const RUN_ID = "run-topology-interaction"
const EMPTY = Object.freeze([]) as readonly string[]
const EMPTY_NUMBERS = Object.freeze([]) as readonly number[]

function evidence(
  entityId: string,
  sequence: number,
  options: {
    controlCommandIds?: readonly string[]
    entityRefs?: readonly string[]
    checkpointIds?: readonly string[]
  } = {},
): TopologyEvidence {
  return Object.freeze({
    eventIds: Object.freeze([`event-${entityId}-${sequence}`]),
    mutationIds: Object.freeze([`mutation-${entityId}-${sequence}`]),
    correlationIds: Object.freeze([`correlation-${sequence}`]),
    causationIds: Object.freeze([`cause-${Math.max(0, sequence - 1)}`]),
    spanIds: Object.freeze([`span-${entityId}`]),
    parentSpanIds: Object.freeze([]),
    checkpointIds: Object.freeze([...(options.checkpointIds ?? [])]),
    controlCommandIds: Object.freeze([...(options.controlCommandIds ?? [])]),
    graphIds: Object.freeze(["graph-large"]),
    graphRevisions: Object.freeze([sequence]),
    entityRefs: Object.freeze([...(options.entityRefs ?? [])]),
    firstSequence: sequence,
    lastSequence: sequence,
  })
}

function baseEntity(
  id: string,
  kind: string,
  sequence: number,
  revision: number,
  namespace: string,
) {
  return {
    id,
    taskId: TASK_ID,
    runId: RUN_ID,
    kind,
    sequence,
    revision,
    graphRevision: revision,
    commitRevision: revision,
    state: sequence % 37 === 0 ? "recovering" : sequence % 19 === 0 ? "waiting" : "running",
    title: `Topology ${kind} ${id}`,
    summary: `Live ${kind} ${id} in ${namespace}`,
    namespace,
    effective: true,
    terminal: false,
    pending: false,
    removed: false,
    evidence: evidence(id, sequence),
    attributes: Object.freeze({ source: "synthetic-live-state" }),
  }
}

function node(
  index: number,
  revision: number,
  options: {
    removed?: boolean
    replaced?: boolean
    controlCommandIds?: readonly string[]
  } = {},
): TopologyNodeView {
  const id = `node-${index}`
  const namespace = `domain-${Math.floor(index / 120)}`
  const dependency =
    index === 0 ? EMPTY : Object.freeze([`node-${Math.max(0, index - 1)}`])
  return Object.freeze({
    ...baseEntity(id, "node", index + revision * 10_000, revision, namespace),
    kind: "node" as const,
    role:
      index % 11 === 0
        ? "verifier"
        : index % 7 === 0
          ? "planner"
          : index % 5 === 0
            ? "browser"
            : "coder",
    capabilities: Object.freeze([
      index % 5 === 0 ? "browser" : "code",
      index % 7 === 0 ? "planning" : "execution",
    ]),
    dependencies: dependency,
    childNodeIds: EMPTY,
    workerIds: Object.freeze([`worker-${index % 48}`]),
    routeIds: index % 3 === 0 ? Object.freeze([`route-${index}`]) : EMPTY,
    placementIds:
      index % 3 === 0 ? Object.freeze([`placement-${index}`]) : EMPTY,
    artifactIds:
      index % 17 === 0 ? Object.freeze([`artifact-${index}`]) : EMPTY,
    affectedByChangeIds: index % 127 === 0 ? Object.freeze(["change-live"]) : EMPTY,
    missingDependencyIds: index % 211 === 0 && index > 0 ? dependency : EMPTY,
    physicalAttemptRef: `attempt-${index}`,
    workerLeaseRef: `lease-${index}`,
    workspaceRef: `workspace-${index % 24}`,
    backendRouteRef: index % 3 === 0 ? `route-${index}` : undefined,
    openWorldCreated: revision > 1 && index >= 2_400,
    openWorldRemoved: options.removed === true,
    openWorldReplaced: options.replaced === true,
    removed: options.removed === true,
    evidence: evidence(id, index + revision * 10_000, {
      controlCommandIds: options.controlCommandIds,
      entityRefs:
        index % 17 === 0 ? Object.freeze([`artifact:artifact-${index}`]) : EMPTY,
    }),
  } as TopologyNodeView)
}

function edge(
  index: number,
  sourceId: string,
  targetId: string,
  revision: number,
): TopologyEdgeView {
  const id = `edge-${index}-${sourceId}-${targetId}`
  return Object.freeze({
    ...baseEntity(id, "edge", index + revision * 20_000, revision, "graph"),
    kind: "edge" as const,
    edgeKind: "dependency" as const,
    sourceId,
    targetId,
    relation: index % 13 === 0 ? "causal_handoff" : "dependency",
    weight: 1 + (index % 7) / 10,
    requiredCapabilities: EMPTY,
    condition: Object.freeze({}),
    sourceMissing: false,
    targetMissing: false,
    inferred: index % 23 === 0,
    runtimeMutation: revision > 1 && index % 31 === 0,
    evidence: evidence(id, index + revision * 20_000, {
      entityRefs: [`node:${sourceId}`, `node:${targetId}`],
    }),
  } as TopologyEdgeView)
}

function route(index: number, revision: number): RouteDecisionView {
  const id = `route-${index}`
  const location = index % 4 === 0 ? "device" : index % 4 === 1 ? "local" : index % 4 === 2 ? "edge" : "cloud"
  return Object.freeze({
    ...baseEntity(id, "route", index + revision * 30_000, revision, `domain-${Math.floor(index / 120)}`),
    kind: "route" as const,
    routeId: id,
    routeType: index % 9 === 0 ? "broadcast" : "direct",
    nodeId: `node-${index}`,
    selectedWorkerId: `worker-${index % 48}`,
    selectedCandidateId: `candidate-${index}-0`,
    selectedProviderId: `provider-${index % 4}`,
    selectedModelId: `model-${index % 7}`,
    selectedBackendId: `backend-${index % 5}`,
    selectedLocation: location,
    topK: 3,
    candidateCount: 3,
    candidates: Object.freeze([
      {
        id: `candidate-${index}-0`,
        routeId: id,
        workerId: `worker-${index % 48}`,
        workerName: `Worker ${index % 48}`,
        providerId: `provider-${index % 4}`,
        modelId: `model-${index % 7}`,
        backendId: `backend-${index % 5}`,
        location,
        rank: 1,
        score: 0.9,
        accepted: true,
        selected: true,
        health: "healthy",
        reasons: Object.freeze(["capability fit", "privacy fit"]),
        capabilities: Object.freeze(["execution"]),
        requiredCapabilities: Object.freeze(["execution"]),
        missingCapabilities: EMPTY,
        privacyClass: index % 40 === 39 ? "restricted" : "internal",
        evidence: evidence(`candidate-${index}-0`, index),
        attributes: Object.freeze({}),
      },
      {
        id: `candidate-${index}-1`,
        routeId: id,
        workerId: `worker-${(index + 1) % 48}`,
        workerName: `Worker ${(index + 1) % 48}`,
        location: "cloud",
        rank: 2,
        score: 0.65,
        accepted: true,
        selected: false,
        health: "healthy",
        reasons: Object.freeze(["fallback"]),
        capabilities: Object.freeze(["execution"]),
        requiredCapabilities: Object.freeze(["execution"]),
        missingCapabilities: EMPTY,
        evidence: evidence(`candidate-${index}-1`, index),
        attributes: Object.freeze({}),
      },
      {
        id: `candidate-${index}-2`,
        routeId: id,
        workerId: `worker-${(index + 2) % 48}`,
        workerName: `Worker ${(index + 2) % 48}`,
        location: "edge",
        rank: 3,
        score: 0.2,
        accepted: false,
        selected: false,
        health: index % 29 === 0 ? "failed" : "degraded",
        reasons: Object.freeze(["resource mismatch"]),
        capabilities: EMPTY,
        requiredCapabilities: Object.freeze(["execution"]),
        missingCapabilities: Object.freeze(["execution"]),
        evidence: evidence(`candidate-${index}-2`, index),
        attributes: Object.freeze({}),
      },
    ]),
    requiredCapabilities: Object.freeze(["execution"]),
    requiredTools: index % 5 === 0 ? Object.freeze(["browser"]) : Object.freeze(["shell"]),
    reasons: Object.freeze(["deterministic scheduler decision"]),
    accepted: true,
    routeHealth: index % 29 === 0 ? "degraded" : "healthy",
    routeChanged: revision > 1 || index % 41 === 0,
    fixedCandidateSelection: false,
    topologyMutation: revision > 1 && index % 31 === 0,
    policyId: `policy-${index % 3}`,
  } as RouteDecisionView)
}

function placement(index: number, revision: number): PlacementView {
  const id = `placement-${index}`
  const location = index % 4 === 0 ? "device" : index % 4 === 1 ? "local" : index % 4 === 2 ? "edge" : "cloud"
  const violated = index % 97 === 0
  return Object.freeze({
    ...baseEntity(id, "placement", index + revision * 40_000, revision, `domain-${Math.floor(index / 120)}`),
    kind: "placement" as const,
    placementId: id,
    routeId: `route-${index}`,
    nodeId: `node-${index}`,
    workerId: `worker-${index % 48}`,
    backendId: `backend-${index % 5}`,
    providerId: `provider-${index % 4}`,
    modelId: `model-${index % 7}`,
    location,
    privacyClass: index % 40 === 39 ? "restricted" : "internal",
    modelSplitStrategy: index % 13 === 0 ? "layered" : "none",
    modelSplit: Object.freeze([]) as PlacementView["modelSplit"],
    costEstimate: (index % 20) / 10,
    latencyEstimateMs: 20 + (index % 70),
    sla: Object.freeze({
      budgetLimit: 4,
      costEstimate: (index % 20) / 10,
      latencyLimitMs: 80,
      latencyEstimateMs: 20 + (index % 70),
      costSatisfied: true,
      latencySatisfied: !violated,
      privacySatisfied: !violated,
      resourceSatisfied: true,
      satisfied: !violated,
      violations: violated ? Object.freeze(["privacy policy"]) : EMPTY,
    }),
    changed: revision > 1 || index % 43 === 0,
  } as PlacementView)
}

function checkpoint(
  revision: number,
  controlCommandIds: readonly string[] = EMPTY,
): CheckpointView {
  return Object.freeze({
    ...baseEntity("checkpoint-head", "checkpoint", 80_000 + revision, revision, "recovery"),
    kind: "checkpoint" as const,
    checkpointId: "checkpoint-head",
    parentCheckpointId: "checkpoint-parent",
    ancestry: Object.freeze(["checkpoint-root", "checkpoint-parent"]),
    phase: "committed",
    iteration: revision,
    workflowSignature: "workflow-v1",
    graphSignature: `graph-v${revision}`,
    topologySignature: `topology-v${revision}`,
    contentDigest: `sha256:${"a".repeat(64)}`,
    pendingWrites: Object.freeze([
      {
        id: "write-pending",
        checkpointId: "checkpoint-head",
        taskKey: TASK_ID,
        channel: "next_tasks",
        state: "pending",
        sequence: 80_000 + revision,
        writerId: "planner",
        branchId: "branch-live",
        evidence: evidence("write-pending", revision),
      },
    ]),
    committedWrites: Object.freeze([
      {
        id: "write-committed",
        checkpointId: "checkpoint-head",
        taskKey: TASK_ID,
        channel: "canonical_state",
        state: "committed",
        sequence: 79_999 + revision,
        writerId: "commit-owner",
        idempotencyKey: "checkpoint-write-once",
        evidence: evidence("write-committed", revision),
      },
    ]),
    pendingRequestIds: Object.freeze(["request-pending"]),
    inflightMessageIds: Object.freeze(["message-inflight"]),
    completedStepIds: Object.freeze(["step-1"]),
    processedResponseIds: Object.freeze(["response-1"]),
    sideEffectFenceKeys: Object.freeze(["effect-fence-1"]),
    interruptIds: Object.freeze(["interrupt-1"]),
    resumeIds: revision > 1 ? Object.freeze(["resume-1"]) : EMPTY,
    nextTaskIds: Object.freeze(["node-17", "node-18"]),
    lineageValid: true,
    visibilityValid: true,
    evidence: evidence("checkpoint-head", 80_000 + revision, {
      controlCommandIds,
      checkpointIds: ["checkpoint-head"],
    }),
  } as CheckpointView)
}

function branch(revision: number): BranchView {
  const conflict: BranchConflictView = Object.freeze({
    id: "conflict-live",
    kind: "write_write",
    key: "route:node-17",
    branchId: "branch-live",
    conflictingBranchId: "branch-peer",
    baseRevision: revision - 1,
    currentRevision: revision,
    reason: "Concurrent route placement change",
    recoverable: true,
    evidence: evidence("conflict-live", 81_000 + revision),
  })
  return Object.freeze({
    ...baseEntity("branch-live", "branch", 81_000 + revision, revision, "recovery"),
    kind: "branch" as const,
    branchId: "branch-live",
    deltaId: "delta-live",
    checkpointId: "checkpoint-head",
    owner: "GraphStateCustody",
    baseRevision: revision - 1,
    outcome: revision > 1 ? "rebased" : "conflicted",
    strategy: "deterministic-rebase",
    readSet: Object.freeze(["route:node-17"]),
    writeSet: Object.freeze(["route:node-17", "placement:node-17"]),
    entryIds: Object.freeze(["entry-1"]),
    mutationIds: Object.freeze(["mutation-branch-live"]),
    conflicts: Object.freeze([conflict]),
    rebasedFromRevision: revision > 1 ? revision - 1 : undefined,
    deterministicOrderKey: `branch-live:${revision}`,
    visibleInCanonicalState: revision > 1,
  } as BranchView)
}

function requirementChange(
  revision: number,
  controlCommandIds: readonly string[] = EMPTY,
): RequirementChangeView {
  return Object.freeze({
    ...baseEntity("change-live", "requirement_change", 82_000 + revision, revision, "control"),
    kind: "requirement_change" as const,
    changeId: "change-live",
    sourceEventId: `event-change-${revision}`,
    text: "Replan verifier nodes with stricter evidence",
    affectedNodeIds: Object.freeze(["node-0", "node-11", "node-22"]),
    supersededNodeIds: Object.freeze(["node-0"]),
    needsRevisionNodeIds: Object.freeze(["node-11", "node-22"]),
    replanNodeId: "node-11",
    decisionId: `decision-change-${revision}`,
    resourceDecisionId: `resource-change-${revision}`,
    routeIds: Object.freeze(["route-0"]),
    localReplan: true,
    faultClassified: false,
    causalClosureEventIds: Object.freeze([`event-change-${revision}`]),
    evidence: evidence("change-live", 82_000 + revision, {
      controlCommandIds,
    }),
  } as RequirementChangeView)
}

function projection(
  nodeCount: number,
  revision = 1,
  options: {
    commandIds?: readonly string[]
    removeFirst?: boolean
    connected?: boolean
    complete?: boolean
  } = {},
): TopologyProjectionView {
  const nodes = Array.from({ length: nodeCount }, (_, index) =>
    node(index, revision, {
      removed: options.removeFirst && index === 0,
      replaced: options.removeFirst && index === 1,
    }),
  )
  const edges: TopologyEdgeView[] = []
  for (let index = 1; index < nodeCount; index += 1) {
    edges.push(edge(index, `node-${index - 1}`, `node-${index}`, revision))
    if (index >= 12 && index % 3 === 0) {
      edges.push(
        edge(nodeCount + index, `node-${index - 12}`, `node-${index}`, revision),
      )
    }
  }
  const routedIndexes = Array.from(
    { length: Math.ceil(nodeCount / 3) },
    (_, value) => value * 3,
  ).filter((value) => value < nodeCount)
  const routes = routedIndexes.map((index) => route(index, revision))
  const placements = routedIndexes.map((index) => placement(index, revision))
  const depthByNode = Object.fromEntries(
    nodes.map((value, index) => [value.id, Math.floor(index / 20)]),
  )
  const conflict = branch(revision).conflicts[0]
  const graphDensity =
    nodeCount <= 1 ? 0 : edges.length / (nodeCount * (nodeCount - 1))
  return Object.freeze({
    schema: "zyra.topology-projection/v1",
    taskId: TASK_ID,
    runIds: Object.freeze([RUN_ID]),
    projectionRevision: revision,
    graphRevision: revision,
    commitRevision: revision,
    nodes: Object.freeze(nodes),
    edges: Object.freeze(edges),
    mutations: Object.freeze([]),
    routes: Object.freeze(routes),
    placements: Object.freeze(placements),
    checkpoints: Object.freeze([checkpoint(revision, options.commandIds)]),
    branches: Object.freeze([branch(revision)]),
    conflicts: Object.freeze([conflict]),
    requirementChanges: Object.freeze([
      requirementChange(revision, options.commandIds),
    ]),
    namespaces: Object.freeze([]),
    analysis: Object.freeze({
      roots: nodeCount ? Object.freeze(["node-0"]) : EMPTY,
      leaves: nodeCount ? Object.freeze([`node-${nodeCount - 1}`]) : EMPTY,
      isolated: EMPTY,
      cycles: Object.freeze([]),
      components: Object.freeze([]),
      topologicalOrder: Object.freeze(nodes.map((value) => value.id)),
      criticalPath: Object.freeze({
        nodeIds: Object.freeze(nodes.slice(0, 40).map((value) => value.id)),
        edgeIds: Object.freeze(edges.slice(0, 39).map((value) => value.id)),
        weight: Math.min(40, nodeCount),
        complete: true,
      }),
      reachableByNode: Object.freeze({}),
      ancestorsByNode: Object.freeze({}),
      depthByNode: Object.freeze(depthByNode),
      missingNodeIds: EMPTY,
      duplicateEdgeIds: EMPTY,
    }),
    metrics: Object.freeze({
      decisionCount: routes.length,
      selectedRouteCount: routes.length,
      candidateCount: routes.length * 3,
      acceptedCandidateCount: routes.length * 2,
      routeChangeCount: routes.filter((value) => value.routeChanged).length,
      topologyMutationCount: revision > 1 ? Math.ceil(nodeCount / 31) : 0,
      openWorldMutationCount: revision > 1 ? 1 : 0,
      fixedCandidateSelectionCount: 0,
      placementChangeCount: placements.filter((value) => value.changed).length,
      devicePlacementCount: placements.filter((value) => value.location === "device").length,
      edgePlacementCount: placements.filter((value) => value.location === "edge").length,
      cloudPlacementCount: placements.filter((value) => value.location === "cloud").length,
      rejectedBranchCount: revision === 1 ? 1 : 0,
      rebasedBranchCount: revision > 1 ? 1 : 0,
      pendingWriteCount: 1,
      committedWriteCount: 1,
      requirementChangeCount: 1,
      supersededNodeCount: options.removeFirst ? 1 : 0,
      effectiveStepCount: nodeCount + edges.length,
      effectiveRouteTransitionCount: routes.length,
      routeDensity: routes.length / Math.max(1, nodeCount),
      graphDensity,
    }),
    diagnostics: Object.freeze({
      ready: true,
      disabled: false,
      snapshotComplete: options.complete ?? true,
      connected: options.connected ?? true,
      projectionRevision: revision,
      cursorGeneration: 1,
      committedSequence: 90_000 + revision,
      highWatermark: 90_000 + revision,
      lag: 0,
      duplicateEvents: 0,
      staleEvents: 0,
      missingSequences: EMPTY_NUMBERS,
      warnings: EMPTY,
      errors: EMPTY,
      rejectedEntityIds: EMPTY,
      missingEvidenceEntityIds: EMPTY,
      ambiguousEntityIds: EMPTY,
      sensitiveFieldDrops: 0,
    }),
    evidenceByEntity: Object.freeze({}),
  })
}

describe("topology interaction and large graph control", () => {
  test(
    "virtualizes and clusters a 2,400-node graph while preserving semantic layers and minimap navigation",
    () => {
      const controller = new TopologyWorkbenchController(TASK_ID, {
        initialViewport: { x: 0, y: 0, width: 1_280, height: 720 },
        maximumRenderedNodes: 180,
        maximumRenderedEdges: 320,
        clusterThreshold: 400,
      })
      const snapshot = controller.project(projection(2_400), { fit: true })

      expect(snapshot.model.nodes).toHaveLength(2_400)
      expect(snapshot.model.edges.length).toBeGreaterThan(3_100)
      expect(snapshot.renderPlan.nodes.length).toBeLessThanOrEqual(180)
      expect(snapshot.renderPlan.edges.length).toBeLessThanOrEqual(320)
      expect(snapshot.renderPlan.clipped).toBe(true)
      expect(snapshot.clusters.clusters.length).toBeGreaterThan(20)
      expect(snapshot.clusters.levels.length).toBeGreaterThan(1)
      expect(snapshot.layout.collisionCount).toBeGreaterThanOrEqual(0)
      expect(snapshot.layers.size).toBe(8)
      expect(snapshot.layers.get("route-density")?.visibleCount).toBeGreaterThan(700)
      expect(snapshot.layers.get("provider-model")?.visibleCount).toBeGreaterThan(700)
      expect(snapshot.layers.get("privacy")?.criticalCount).toBeGreaterThan(0)
      expect(controller.setLayer("policy-violation")).toBe(true)

      const minimap = buildMinimapProjection(
        snapshot.model,
        snapshot.layout,
        snapshot.clusters,
        controller.viewport,
        { x: 0, y: 0, width: 200, height: 120 },
      )
      expect(minimap.nodes.length).toBeGreaterThan(0)
      expect(minimap.viewport.width).toBeGreaterThan(0)
      controller.viewport.minimapNavigate(
        { x: 160, y: 80 },
        { x: 0, y: 0, width: 200, height: 120 },
      )
      expect(controller.getSnapshot().viewport.lastInput).toBe("minimap")
      controller.close()
    },
    30_000,
  )

  test("keeps unchanged nodes stable across open-world additions, removal/replacement, restart, and rejects stale frames", () => {
    const controller = new TopologyWorkbenchController(TASK_ID, {
      initialViewport: { x: 0, y: 0, width: 1_000, height: 700 },
      maximumRenderedNodes: 300,
      clusterThreshold: 250,
    })
    const first = controller.project(projection(320, 1), { fit: true })
    const before = first.layout.nodes.get("node-150")
    expect(before).toBeDefined()

    const second = controller.project(
      projection(324, 2, { removeFirst: true }),
      { preserveFilters: true },
    )
    const after = second.layout.nodes.get("node-150")
    expect(after).toBeDefined()
    expect(second.layout.addedNodeIds).toEqual(
      expect.arrayContaining(["node-320", "node-321", "node-322", "node-323"]),
    )
    expect(second.model.nodeById.get("node-0")?.removed).toBe(true)
    expect(second.model.nodeById.get("node-1")?.openWorldReplaced).toBe(true)
    expect(second.layout.stableNodeCount).toBeGreaterThan(250)
    expect(Math.hypot(
      (after?.center.x ?? 0) - (before?.center.x ?? 0),
      (after?.center.y ?? 0) - (before?.center.y ?? 0),
    )).toBeLessThan(350)

    const revision = controller.getSnapshot().revision
    const ignored = controller.project(projection(120, 1))
    expect(ignored.revision).toBe(revision)
    expect(ignored.model.projectionRevision).toBe(2)
    expect(controller.takeAnnouncements().join(" ")).toContain("Ignored stale topology revision")

    const restarted = new TopologyWorkbenchController(TASK_ID, {
      initialViewport: { x: 0, y: 0, width: 1_000, height: 700 },
      maximumRenderedNodes: 300,
      clusterThreshold: 250,
    })
    const restored = restarted.project(projection(324, 2, { removeFirst: true }))
    expect(restored.model.nodeById.has("node-323")).toBe(true)
    expect(restored.model.nodeById.get("node-0")?.removed).toBe(true)
    expect(restored.model.graphRevision).toBe(2)
    controller.close()
    restarted.close()
  })

  test("searches, filters, selects, links evidence, and exposes checkpoint/branch detail", () => {
    const controller = new TopologyWorkbenchController(TASK_ID, {
      initialViewport: { x: 0, y: 0, width: 1_100, height: 720 },
    })
    let snapshot = controller.project(projection(240), { fit: true })
    const facets = topologyFilterFacets(snapshot.model)
    expect(facets.roles).toContain("verifier")
    expect(facets.locations).toEqual(expect.arrayContaining(["device", "edge", "cloud"]))
    expect(facets.providers.length).toBe(4)

    snapshot = controller.setFilters({
      query: 'role:verifier namespace:"domain 0" -state:failed',
      changedOnly: false,
    })
    expect(snapshot.filtered.nodeIds.length).toBeGreaterThan(0)
    expect(snapshot.filtered.nodeIds.length).toBeLessThan(20)
    expect(
      snapshot.filtered.nodeIds.every(
        (id) => snapshot.model.nodeById.get(id)?.role === "verifier",
      ),
    ).toBe(true)

    controller.resetFilters()
    expect(controller.select("node", "node-17", "pointer")?.id).toBe("node-17")
    const details = controller.getSnapshot().selectedDetails
    expect(details?.kind).toBe("node")
    expect(details?.artifactIds).toEqual(["artifact-17"])
    expect(details?.eventIds.length).toBeGreaterThan(0)
    expect(controller.navigationIntents().some((intent) => intent.kind === "open-artifact")).toBe(true)
    expect(controller.navigationIntents().some((intent) => intent.kind === "open-timeline")).toBe(true)

    expect(controller.select("checkpoint", "checkpoint-head", "programmatic")?.id).toBe("checkpoint-head")
    const checkpointDetails = controller.getSnapshot().selectedDetails
    expect(
      checkpointDetails?.facts.some(
        (fact) => fact.label === "Pending writes" && fact.value === "1",
      ),
    ).toBe(true)
    expect(controller.select("branch", "branch-live", "programmatic")?.id).toBe("branch-live")
    expect(
      controller
        .getSnapshot()
        .selectedDetails?.facts.some(
          (fact) => fact.label === "Conflicts" && fact.value === "1",
        ),
    ).toBe(true)
    controller.close()
  })

  test("supports keyboard, screen reader, hover, box selection, zoom and focus repair without DOM enumeration", () => {
    const controller = new TopologyWorkbenchController(TASK_ID, {
      initialViewport: { x: 0, y: 0, width: 900, height: 620 },
      maximumRenderedNodes: 120,
    })
    const snapshot = controller.project(projection(180), { fit: true })
    const accessible = buildAccessibleGraph(
      snapshot.model,
      snapshot.layout,
      {
        visibleIds: new Set(snapshot.filtered.nodeIds),
        focusedId: snapshot.viewport.focusEntityId,
        selection: snapshot.viewport.selected,
      },
    )
    expect(accessible.rows.length).toBeGreaterThan(0)
    expect(accessible.description).toContain("nodes")
    const first = accessible.rows[0]?.id
    expect(first).toBeDefined()
    const next = nextAccessibleNode(accessible.rows, first, "ArrowDown")
    expect(next).not.toBe(first)
    expect(focusRepair(accessible.rows, "missing-node")).toBe(first)

    expect(controller.viewport.keyboard({ key: "Home" })).toBe(true)
    expect(controller.viewport.keyboard({ key: "Enter", now: 100 })).toBe(true)
    expect(controller.getSnapshot().viewport.selected?.source).toBe("keyboard")
    expect(controller.viewport.keyboard({ key: "+" })).toBe(true)
    expect(controller.getSnapshot().viewport.lastInput).toBe("keyboard")
    expect(controller.viewport.keyboard({ key: "Escape" })).toBe(true)
    expect(controller.getSnapshot().viewport.selected).toBeUndefined()

    const bounds = snapshot.layout.nodes.get("node-0")?.bounds
    expect(bounds).toBeDefined()
    const selected = selectionBoxNodeIds(
      snapshot.layout,
      bounds
        ? {
            x: bounds.x - 10,
            y: bounds.y - 10,
            width: bounds.width + 20,
            height: bounds.height + 20,
          }
        : { x: 0, y: 0, width: 1, height: 1 },
    )
    expect(selected).toContain("node-0")
    expect(controller.takeAnnouncements().length).toBeGreaterThan(0)
    controller.close()
  })

  test("tracks loading, reconnect, projection diagnostics and fails loudly when the module is disconnected", () => {
    const controller = new TopologyWorkbenchController(TASK_ID)
    const loading = controller.project(
      projection(30, 1, { complete: false, connected: false }),
    )
    expect(loading.loading).toBe(true)
    expect(loading.reconnecting).toBe(false)
    controller.setConnectionState({ loading: false, reconnecting: true })
    expect(controller.getSnapshot().reconnecting).toBe(true)
    controller.setConnectionState({ error: "delta stream unavailable" })
    expect(controller.getSnapshot().error).toBe("delta stream unavailable")
    expect(controller.takeAnnouncements().join(" ")).toContain("reconnecting")
    expect(() => controller.disableProbe()).toThrow("disabled")
    controller.close()
    expect(() => controller.subscribe(() => undefined)).toThrow("closed")
  })

  test("submits typed backend controls, observes canonical effects, and counts sealed denials", async () => {
    const submitted: Array<Readonly<Record<string, unknown>>> = []
    let now = 1_000
    const transport = topologyControlTransport(async (input) => {
      submitted.push(Object.freeze({ ...input }))
      if (input.sealed) {
        return Object.freeze({
          ok: false,
          intervention_counted: true,
          command_result: Object.freeze({
            ok: false,
            status: "denied",
            error_code: "permission_denied",
            error: Object.freeze({
              code: "permission_denied",
              message: "sealed autonomous mutation denied",
            }),
            intervention_counted: true,
          }),
        })
      }
      return Object.freeze({
        ok: true,
        command_result: Object.freeze({
          ok: true,
          status: "pending",
          summary: "Canonical owner accepted the command.",
          data: Object.freeze({ checkpoint_ref: input.arguments.checkpoint_ref }),
        }),
      })
    })
    const controller = new TopologyWorkbenchController(TASK_ID, {
      now: () => ++now,
      controlTransport: transport,
    })
    controller.project(projection(80))

    const pending = await controller.submitControl({
      action: "requirement-change",
      text: "Require an additional verification artifact",
      sealed: false,
      actorId: "operator-test",
    })
    expect(pending.phase).toBe("pending")
    expect(submitted[0]?.text).toContain("/change")
    expect(String(submitted[0]?.idempotencyKey)).toStartWith("task.control-command:")

    controller.project(
      projection(81, 2, { commandIds: [pending.commandId] }),
      { preserveFilters: true },
    )
    const observed = controller.controls?.receipt(pending.id)
    expect(observed?.phase).toBe("committed")
    expect(observed?.observedRevision).toBe(2)
    expect(observed?.observedEventIds.length).toBeGreaterThan(0)

    const rewind = await controller.submitControl({
      action: "time-travel",
      checkpointId: "checkpoint-head",
      expectedRevision: 1,
      sealed: false,
      actorId: "operator-test",
    })
    expect(rewind.commandName).toBe("/rewind")
    expect(rewind.phase).toBe("pending")
    expect(submitted.at(-1)?.text).toBe("/rewind checkpoint-head")

    const resume = await controller.submitControl({
      action: "resume-checkpoint",
      checkpointId: "checkpoint-head",
      sessionId: "session-topology",
      expectedRevision: 1,
      sealed: false,
      actorId: "operator-test",
    })
    expect(resume.commandName).toBe("/resume")
    expect(submitted.at(-1)?.text).toBe("/resume session-topology")

    const denied = await controller.submitControl({
      action: "local-update",
      nodeId: "node-17",
      fields: { state: "needs_revision" },
      expectedRevision: 2,
      sealed: true,
      actorId: "sealed-policy",
    })
    expect(denied.phase).toBe("denied")
    expect(denied.denied).toBe(true)
    expect(denied.interventionCounted).toBe(true)

    const invalid = await controller.submitControl({
      action: "local-update",
      nodeId: "node-17",
      fields: {},
      sealed: false,
      actorId: "operator-test",
    })
    expect(invalid.phase).toBe("failed")
    expect(invalid.errorCode).toBe("topology_control_validation_failed")
    expect(controller.getSnapshot().receipts.length).toBeGreaterThanOrEqual(5)
    controller.close()
  })
})
