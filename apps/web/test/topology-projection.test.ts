import { describe, expect, test } from "bun:test"
import {
  CanonicalProjectionStore,
  TopologyProjectionEngine,
  TopologyProjectionError,
  buildTopologyProjection,
  selectTopologyPanel,
  selectTopologyProjection,
} from "../src/state/index.ts"
import {
  normalizeEventFrame,
  type IngressBatch,
  type IngressEvent,
  type JsonObject,
} from "../src/events/ingress/index.ts"

const TASK = "task_topology_projection"
const RUN = "run_topology_projection"
const SESSION = "session_topology_projection"
const NOW = "2026-07-24T00:00:00.000Z"
const DIGEST = `sha256:${"c".repeat(64)}`

interface TestEventOptions {
  eventId?: string
  eventType: string
  domain: string
  inline: JsonObject
  nodeId?: string
  workerId?: string
  checkpointId?: string
  causationId?: string
  terminal?: boolean
  effective?: boolean
}

function topologyEvent(
  sequence: number,
  options: TestEventOptions,
): IngressEvent {
  const eventId = options.eventId ?? `topology_event_${sequence}`
  const raw = {
    schema: "zyra.runtime-event/v1",
    eventId,
    eventType: options.eventType,
    eventVersion: 1,
    aggregateId: `task:${TASK}`,
    aggregateSequence: sequence,
    globalSequence: sequence,
    producerSequence: sequence,
    idempotencyKey: `topology-${eventId}`,
    correlationId: "request_topology_projection",
    causationId: options.causationId,
    createdAt: NOW,
    committedAt: new Date(Date.parse(NOW) + sequence).toISOString(),
    durability: "durable",
    effect: options.effective === false ? "non_effective" : "effective",
    identity: {
      taskId: TASK,
      runId: RUN,
      sessionId: SESSION,
      nodeId: options.nodeId,
      workerId: options.workerId,
      spanId: `span_topology_${sequence}`,
      checkpointId: options.checkpointId,
    },
    sender: {
      kind: "runtime",
      id: options.workerId ?? "topology-runtime",
      capabilityRefs: [],
    },
    intent: "status",
    topKRecipients: [],
    summary: `${options.eventType} ${sequence}`,
    stateDelta: {
      domain: options.domain,
      operation: "transition",
      path: ["status"],
      beforeDigest: `sha256:${"1".repeat(64)}`,
      afterDigest: `sha256:${"2".repeat(64)}`,
      effective: options.effective !== false,
    },
    evidenceRefs: [],
    artifactRefs: [],
    provenance: {
      sourceRepository: "zyra",
      sourceModule: "topology-projection-test",
      migrationRole: "zyra_owned",
      producerVersion: "test",
      trust: "internal",
    },
    inline: options.inline,
    sourceBytes: 128,
    inlineBytes: 96,
    envelopeBytes: 1024,
    contentDigest: DIGEST,
    metadata: {},
    settlement: "atomic",
    terminal: options.terminal ?? false,
  }
  return normalizeEventFrame(
    JSON.parse(
      JSON.stringify({
        schema: "zyra.event-ingress-frame/v1",
        kind: "event",
        source: "delta",
        generation: 1,
        taskId: TASK,
        sequence,
        previousSequence: Math.max(0, sequence - 1),
        eventId,
        eventType: options.eventType,
        correlationId: "request_topology_projection",
        causationId: options.causationId,
        observedAtMs: 10_000 + sequence,
        cursor: `cursor.${sequence}`,
        event: raw,
      }),
    ),
    TASK,
    1,
  ).event
}

function topologyBatch(
  events: readonly IngressEvent[],
  options: { sequence?: number; highWatermark?: number; snapshot?: boolean } = {},
): IngressBatch {
  const sequence =
    options.sequence ??
    Math.max(0, ...events.map((event) => event.globalSequence))
  return {
    taskId: TASK,
    generation: 1,
    events: Object.freeze([...events]),
    receipts: Object.freeze([]),
    cursor: `cursor.${sequence}`,
    fromSequence: Math.min(sequence, ...events.map((event) => event.globalSequence)),
    sequence,
    highWatermark: options.highWatermark ?? sequence,
    receivedAt: 20_000 + sequence,
    transport: "long_poll",
    snapshot: options.snapshot ?? true,
    caughtUp: (options.highWatermark ?? sequence) === sequence,
  }
}

function projectionStore(): CanonicalProjectionStore {
  return new CanonicalProjectionStore({
    restore: false,
    autoPersist: false,
    now: () => Date.parse(NOW) + 10_000,
  })
}

function baseTopologyEvents(): IngressEvent[] {
  return [
    topologyEvent(1, {
      eventType: "task.started",
      domain: "task",
      inline: { task_id: TASK, status: "running", title: "Topology task" },
    }),
    topologyEvent(2, {
      eventId: "graph_commit_1",
      eventType: "topology.graph.committed",
      domain: "node",
      nodeId: "plan",
      inline: {
        node_id: "plan",
        status: "running",
        graph_snapshot: {
          graph_id: "graph-main",
          revision: 1,
          commit_revision: 1,
          nodes: [
            {
              node_id: "plan",
              role: "planner",
              namespace: "root",
              capabilities: ["planning"],
              state: "running",
            },
            {
              node_id: "code",
              role: "coder",
              namespace: "implementation",
              subgraph_id: "subgraph-code",
              dependencies: ["plan"],
              capabilities: ["typescript", "shell"],
              state: "ready",
            },
          ],
          edges: [
            {
              edge_id: "edge-plan-code",
              source_node_id: "plan",
              target_node_id: "code",
              relation: "dependency",
            },
          ],
        },
        graph_mutations: [
          {
            mutation_id: "mutation-add-code",
            kind: "add_node",
            entity_id: "code",
            base_revision: 0,
            committed_revision: 1,
            value: { node_id: "code", role: "coder" },
          },
          {
            mutation_id: "mutation-add-edge",
            kind: "add_edge",
            entity_id: "edge-plan-code",
            base_revision: 0,
            committed_revision: 1,
            value: {
              source_node_id: "plan",
              target_node_id: "code",
              relation: "dependency",
            },
          },
          {
            mutation_id: "mutation-role-code",
            kind: "set_node_role",
            entity_id: "code",
            base_revision: 1,
            committed_revision: 2,
            value: {
              node_id: "code",
              role: "reviewer",
              namespace: "implementation",
            },
          },
          {
            mutation_id: "mutation-capabilities-code",
            kind: "set_node_capabilities",
            entity_id: "code",
            base_revision: 2,
            committed_revision: 3,
            value: {
              node_id: "code",
              capabilities: ["typescript", "shell", "review"],
              namespace: "implementation",
            },
          },
          {
            mutation_id: "mutation-replace-code",
            kind: "replace_node",
            entity_id: "code",
            base_revision: 3,
            committed_revision: 4,
            value: {
              node_id: "code",
              role: "reviewer",
              capabilities: ["typescript", "shell", "review"],
              namespace: "implementation",
              replacement_node_id: "code-v2",
            },
          },
          {
            mutation_id: "mutation-remove-old-node",
            kind: "remove_node",
            entity_id: "old-code",
            base_revision: 4,
            committed_revision: 5,
            value: { node_id: "old-code" },
          },
          {
            mutation_id: "mutation-replace-edge",
            kind: "replace_edge",
            entity_id: "edge-old",
            base_revision: 5,
            committed_revision: 6,
            value: {
              source_node_id: "plan",
              target_node_id: "code",
              relation: "review_handoff",
              replacement_edge_id: "edge-plan-code",
            },
          },
          {
            mutation_id: "mutation-remove-edge",
            kind: "remove_edge",
            entity_id: "edge-obsolete",
            base_revision: 6,
            committed_revision: 7,
            value: {
              source_node_id: "plan",
              target_node_id: "code",
              relation: "obsolete",
            },
          },
        ],
      },
    }),
    topologyEvent(3, {
      eventType: "worker.admitted",
      domain: "worker",
      nodeId: "code",
      workerId: "worker-edge",
      inline: {
        worker_id: "worker-edge",
        node_id: "code",
        status: "running",
        capabilities: ["typescript", "shell"],
      },
    }),
  ]
}

describe("topology route and placement projection", () => {
  test("distinguishes open-world topology mutation from fixed candidate route selection", () => {
    const store = projectionStore()
    store.apply(
      topologyBatch([
        ...baseTopologyEvents(),
        topologyEvent(4, {
          eventType: "scheduler.route.selected",
          domain: "scheduler",
          nodeId: "code",
          inline: {
            scheduler_id: "scheduler-main",
            route_id: "route-code",
            decision_id: "decision-code",
            resource_decision_id: "resource-code",
            node_id: "code",
            status: "completed",
            top_k: 2,
            selected_candidate_id: "candidate-edge",
            selected_worker_id: "worker-edge",
            selected_location: "edge",
            candidates: [
              {
                candidate_id: "candidate-edge",
                worker_id: "worker-edge",
                location: "edge",
                rank: 1,
                score: 0.94,
                accepted: true,
                selected: true,
                capabilities: ["typescript", "shell"],
              },
              {
                candidate_id: "candidate-cloud",
                worker_id: "worker-cloud",
                location: "cloud",
                rank: 2,
                score: 0.7,
                accepted: true,
                selected: false,
                capabilities: ["typescript", "shell"],
              },
            ],
          },
          terminal: true,
        }),
      ]),
    )
    const view = store.select(selectTopologyProjection(TASK))
    expect(view.nodes.map((node) => node.id).sort()).toEqual([
      "code",
      "old-code",
      "plan",
    ])
    expect(view.edges.some((edge) => edge.id === "edge-plan-code")).toBe(true)
    expect(view.namespaces.map((namespace) => namespace.namespaceId)).toContain(
      "implementation",
    )
    expect(
      view.mutations.find((mutation) => mutation.id === "mutation-add-code")
        ?.openWorld,
    ).toBe(true)
    expect(new Set(view.mutations.map((mutation) => mutation.kind))).toEqual(
      new Set([
        "add_node",
        "add_edge",
        "set_node_role",
        "set_node_capabilities",
        "replace_node",
        "remove_node",
        "replace_edge",
        "remove_edge",
      ]),
    )
    expect(view.nodes.find((node) => node.id === "code")?.openWorldReplaced).toBe(
      true,
    )
    expect(
      view.nodes.find((node) => node.id === "old-code")?.openWorldRemoved,
    ).toBe(true)
    expect(view.routes[0]?.fixedCandidateSelection).toBe(true)
    expect(view.routes[0]?.topologyMutation).toBe(false)
    expect(view.metrics.openWorldMutationCount).toBeGreaterThanOrEqual(2)
    expect(view.metrics.fixedCandidateSelectionCount).toBe(1)
    expect(view.evidenceByEntity["route:route-code"]?.eventIds).toContain(
      "topology_event_4",
    )
    expect(view.analysis.roots).toContain("plan")
    expect(view.analysis.reachableByNode.plan).toContain("code")
  })

  test("projects top-k resource fit, provider/model split, device-edge-cloud placement, and SLA", () => {
    const store = projectionStore()
    store.apply(
      topologyBatch([
        ...baseTopologyEvents(),
        topologyEvent(4, {
          eventType: "resource.decision.committed",
          domain: "scheduler",
          nodeId: "code",
          inline: {
            scheduler_id: "resource-code",
            route_id: "route-private-code",
            placement_id: "placement-private-code",
            node_id: "code",
            status: "completed",
            selected_candidate_id: "candidate-local",
            selected_worker_id: "worker-local",
            selected_backend: "local-terminal",
            selected_provider_id: "provider-local",
            selected_model_id: "model-code",
            selected_location: "device",
            privacy_class: "sensitive",
            required_location: "device",
            budget_limit: 0.2,
            cost_estimate: 0.05,
            latency_limit_ms: 500,
            latency_estimate_ms: 120,
            api_key: "must-not-reach-projection",
            requested_resources: {
              cpu_cores: 2,
              memory_mb: 2048,
              browser_slots: 1,
            },
            resource_capacity: {
              cpu_cores: 8,
              memory_mb: 16384,
              browser_slots: 2,
            },
            resource_allocated: {
              cpu_cores: 2,
              memory_mb: 4096,
              browser_slots: 0,
            },
            model_split: {
              primary: "model-code",
              primary_backend: "local-terminal",
              primary_location: "device",
              secondary: "model-review",
              secondary_backend: "edge-runtime",
              secondary_location: "edge",
              handoff: "review",
            },
            candidates: [
              {
                candidate_id: "candidate-local",
                worker_id: "worker-local",
                rank: 1,
                score: 0.98,
                location: "device",
                accepted: true,
                selected: true,
                capabilities: ["typescript", "shell"],
                required_capabilities: ["typescript", "shell"],
              },
              {
                candidate_id: "candidate-edge",
                worker_id: "worker-edge",
                rank: 2,
                score: 0.8,
                location: "edge",
                accepted: true,
              },
              {
                candidate_id: "candidate-cloud",
                worker_id: "worker-cloud",
                rank: 3,
                score: 0.4,
                location: "cloud",
                accepted: false,
                reasons: ["privacy_location_violated"],
              },
            ],
          },
          terminal: true,
        }),
      ]),
    )
    const view = buildTopologyProjection(store.state, TASK)
    const route = view.routes.find(
      (candidate) => candidate.routeId === "route-private-code",
    )
    const placement = view.placements.find(
      (candidate) => candidate.placementId === "placement-private-code",
    )
    expect(route?.candidates.map((candidate) => candidate.rank)).toEqual([1, 2, 3])
    expect(route?.selectedLocation).toBe("device")
    expect(placement?.providerId).toBe("provider-local")
    expect(placement?.modelId).toBe("model-code")
    expect(placement?.modelSplit.map((segment) => segment.location)).toEqual([
      "device",
      "edge",
    ])
    expect(placement?.resourceFit?.fits).toBe(true)
    expect(placement?.sla.satisfied).toBe(true)
    expect(placement?.sla.privacySatisfied).toBe(true)
    expect(view.metrics.devicePlacementCount).toBeGreaterThanOrEqual(1)
    expect(JSON.stringify(placement?.attributes)).not.toContain(
      "must-not-reach-projection",
    )
    expect(view.diagnostics.sensitiveFieldDrops).toBeGreaterThanOrEqual(1)
  })

  test("keeps pending writes invisible and orders conflict, rebase, interrupt, resume, and next-task facts deterministically", () => {
    const store = projectionStore()
    store.apply(
      topologyBatch([
        ...baseTopologyEvents(),
        topologyEvent(4, {
          eventId: "checkpoint_event",
          eventType: "recovery.checkpoint.committed",
          domain: "recovery",
          checkpointId: "checkpoint-2",
          inline: {
            recovery_id: "checkpoint-recovery",
            checkpoint: {
              checkpoint_id: "checkpoint-2",
              parent_checkpoint_id: "checkpoint-1",
              ancestry: ["checkpoint-1"],
              phase: "interrupted",
              iteration: 2,
              graph_revision: 4,
              pending_writes: [
                {
                  id: "write-pending",
                  task_key: "code",
                  channel: "result",
                  sequence: 4,
                  idempotency_key: "fence-pending",
                },
              ],
              committed_writes: [
                {
                  id: "write-committed",
                  task_key: "plan",
                  channel: "result",
                  sequence: 3,
                  idempotency_key: "fence-committed",
                },
              ],
              interrupt_ids: ["interrupt-1"],
              resume_ids: ["resume-1"],
              next_task_ids: ["verify"],
            },
          },
        }),
        topologyEvent(5, {
          eventType: "branch.delta.conflicted",
          domain: "recovery",
          inline: {
            recovery_id: "branch-conflict-recovery",
            branch_delta: {
              branch_id: "branch-b",
              delta_id: "delta-b",
              base_revision: 3,
              graph_revision: 4,
              outcome: "conflicted",
              read_set: ["node:plan"],
              write_set: ["node:code"],
              conflicts: [
                {
                  conflict_id: "conflict-code",
                  kind: "write_conflict",
                  key: "node:code",
                  current_revision: 4,
                  reason: "branch-a committed first",
                  recoverable: true,
                },
              ],
            },
          },
        }),
        topologyEvent(6, {
          eventType: "branch.delta.rebased",
          domain: "recovery",
          inline: {
            recovery_id: "branch-rebase-recovery",
            branch_delta: {
              branch_id: "branch-b",
              delta_id: "delta-b-rebased",
              base_revision: 4,
              graph_revision: 5,
              commit_revision: 5,
              outcome: "rebased",
              rebased_from_revision: 3,
              deterministic_order_key: "0004:branch-b",
              write_set: ["node:code"],
            },
          },
          terminal: true,
        }),
        topologyEvent(7, {
          eventType: "branch.delta.committed",
          domain: "recovery",
          inline: {
            recovery_id: "branch-alias-recovery",
            branchDelta: {
              branchId: "branch-a",
              deltaId: "delta-a",
              baseRevision: 3,
              graphRevision: 4,
              commitRevision: 4,
              outcome: "committed",
              deterministicOrderKey: "0003:branch-a",
              readSet: ["node:plan"],
              writeSet: ["node:verify"],
            },
          },
          terminal: true,
        }),
      ]),
    )
    const view = buildTopologyProjection(store.state, TASK, {
      includeRejected: true,
    })
    const checkpoint = view.checkpoints.find(
      (candidate) => candidate.checkpointId === "checkpoint-2",
    )
    const branch = view.branches.find(
      (candidate) => candidate.branchId === "branch-b",
    )
    expect(checkpoint?.pendingWrites.map((write) => write.id)).toEqual([
      "write-pending",
    ])
    expect(checkpoint?.committedWrites.map((write) => write.id)).toEqual([
      "write-committed",
    ])
    expect(checkpoint?.interruptIds).toEqual(["interrupt-1"])
    expect(checkpoint?.resumeIds).toEqual(["resume-1"])
    expect(checkpoint?.nextTaskIds).toEqual(["verify"])
    expect(branch?.outcome).toBe("rebased")
    expect(branch?.rebasedFromRevision).toBe(3)
    expect(branch?.conflicts[0]?.id).toBe("conflict-code")
    expect(branch?.visibleInCanonicalState).toBe(true)
    expect(view.conflicts.map((conflict) => conflict.id)).toContain(
      "conflict-code",
    )
    expect(view.metrics.rebasedBranchCount).toBe(1)
    expect(view.metrics.pendingWriteCount).toBe(1)
    expect(view.metrics.committedWriteCount).toBe(1)
    expect(view.branches.map((candidate) => candidate.branchId)).toEqual([
      "branch-a",
      "branch-b",
    ])
    expect(view.branches[0]?.readSet).toEqual(["node:plan"])
    expect(view.branches[0]?.writeSet).toEqual(["node:verify"])
  })

  test("links requirement changes to affected, superseded, revision, replan, route, and reverse evidence", () => {
    const store = projectionStore()
    store.apply(
      topologyBatch([
        ...baseTopologyEvents(),
        topologyEvent(4, {
          eventId: "requirement_change_event",
          eventType: "requirement.changed",
          domain: "task",
          inline: {
            task_id: TASK,
            change_id: "change-private",
            requirement_text: "Keep source code on device",
            affected_node_ids: ["code"],
            superseded_node_ids: ["old-code"],
            needs_revision_node_ids: ["code"],
            replan_node_id: "replan-code",
            local_replan: true,
            route_ids: ["route-private"],
            decision_id: "decision-private",
            resource_decision_id: "resource-private",
            status: "needs_revision",
          },
        }),
        topologyEvent(5, {
          eventType: "scheduler.route.changed",
          domain: "scheduler",
          nodeId: "code",
          causationId: "requirement_change_event",
          inline: {
            scheduler_id: "resource-private",
            route_id: "route-private",
            node_id: "code",
            selected_worker_id: "worker-local",
            selected_location: "device",
            previous_route_id: "route-cloud",
            status: "completed",
          },
          terminal: true,
        }),
      ]),
    )
    const view = buildTopologyProjection(store.state, TASK)
    const change = view.requirementChanges.find(
      (candidate) => candidate.changeId === "change-private",
    )
    expect(change?.affectedNodeIds).toContain("code")
    expect(change?.supersededNodeIds).toEqual(["old-code"])
    expect(change?.needsRevisionNodeIds).toEqual(["code"])
    expect(change?.replanNodeId).toBe("replan-code")
    expect(change?.routeIds).toContain("route-private")
    expect(change?.localReplan).toBe(true)
    expect(change?.causalClosureEventIds).toContain("topology_event_5")
    expect(
      view.evidenceByEntity["requirement_change:change-private"]?.entityRefs,
    ).toContain("route:route-private")
  })

  test("uses revisioned dedupe/stale/gap diagnostics and fails closed when disabled", () => {
    const store = projectionStore()
    const initial = topologyBatch(baseTopologyEvents(), {
      sequence: 3,
      highWatermark: 5,
    })
    store.apply(initial)
    store.apply(initial)
    store.apply(
      topologyBatch(
        [
          topologyEvent(2, {
            eventId: "stale-node-event",
            eventType: "topology.node.updated",
            domain: "node",
            nodeId: "plan",
            inline: {
              node_id: "plan",
              revision: 0,
              status: "running",
            },
          }),
        ],
        { sequence: 2, highWatermark: 5, snapshot: false },
      ),
    )
    const view = buildTopologyProjection(store.state, TASK)
    expect(view.diagnostics.duplicateEvents).toBeGreaterThanOrEqual(1)
    expect(view.diagnostics.staleEvents).toBeGreaterThanOrEqual(1)
    expect(view.diagnostics.lag).toBeGreaterThanOrEqual(2)
    expect(view.diagnostics.ready).toBe(false)
    expect(() =>
      buildTopologyProjection(store.state, TASK, { disabled: true }),
    ).toThrow(TopologyProjectionError)
    expect(() =>
      new TopologyProjectionEngine({ disabled: true }).project(store.state, TASK),
    ).toThrow("Topology projection is disabled")
  })

  test("keeps the existing topology panel on the richer canonical projector", () => {
    const store = projectionStore()
    store.apply(topologyBatch(baseTopologyEvents()))
    const rich = buildTopologyProjection(store.state, TASK)
    const panel = store.select(selectTopologyPanel(TASK))
    expect(panel.revision).toBe(rich.projectionRevision)
    expect(panel.nodes.map((node) => node.id)).toEqual(
      rich.nodes.map((node) => node.id),
    )
    expect(panel.missingDependencies).toEqual(rich.analysis.missingNodeIds)
    expect(panel.cycles).toEqual(rich.analysis.cycles)
  })
})
