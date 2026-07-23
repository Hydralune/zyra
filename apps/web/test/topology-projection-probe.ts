import { readFileSync } from "node:fs"
import {
  CanonicalProjectionStore,
  buildTopologyProjection,
} from "../src/state/index.ts"
import {
  normalizeEventFrame,
  type IngressBatch,
  type IngressEvent,
  type JsonObject,
} from "../src/events/ingress/index.ts"

interface ProbeCase {
  task_id: string
  run_id: string
  graph: {
    snapshot: JsonObject
    mutations: JsonObject[]
  }
  decision: JsonObject
  requirement_change?: JsonObject
}

interface ProbeInput {
  cases: Record<string, ProbeCase>
}

const inputPath = process.argv[2]
if (!inputPath) throw new Error("topology-projection-probe requires an input JSON path")
const input = JSON.parse(readFileSync(inputPath, "utf8")) as ProbeInput

function event(
  value: ProbeCase,
  sequence: number,
  eventType: string,
  domain: string,
  inline: JsonObject,
  nodeId?: string,
  causationId?: string,
): IngressEvent {
  const eventId = `${value.task_id}:projection:${sequence}`
  const raw = {
    schema: "zyra.runtime-event/v1",
    eventId,
    eventType,
    eventVersion: 1,
    aggregateId: `task:${value.task_id}`,
    aggregateSequence: sequence,
    globalSequence: sequence,
    producerSequence: sequence,
    idempotencyKey: `probe-${eventId}`,
    correlationId: `probe-${value.task_id}`,
    causationId,
    createdAt: "2026-07-24T00:00:00.000Z",
    committedAt: new Date(
      Date.parse("2026-07-24T00:00:00.000Z") + sequence,
    ).toISOString(),
    durability: "durable",
    effect: "effective",
    identity: {
      taskId: value.task_id,
      runId: value.run_id,
      sessionId: `session:${value.task_id}`,
      nodeId,
      spanId: `span:${value.task_id}:${sequence}`,
    },
    sender: {
      kind: "runtime",
      id: "topology-integration-probe",
      capabilityRefs: [],
    },
    intent: "status",
    topKRecipients: [],
    summary: eventType,
    stateDelta: {
      domain,
      operation: "transition",
      path: ["status"],
      beforeDigest: `sha256:${"1".repeat(64)}`,
      afterDigest: `sha256:${"2".repeat(64)}`,
      effective: true,
    },
    evidenceRefs: [],
    artifactRefs: [],
    provenance: {
      sourceRepository: "zyra",
      sourceModule: "topology-projection-probe",
      migrationRole: "zyra_owned",
      producerVersion: "test",
      trust: "internal",
    },
    inline,
    sourceBytes: 256,
    inlineBytes: 192,
    envelopeBytes: 2048,
    contentDigest: `sha256:${"d".repeat(64)}`,
    metadata: {},
    settlement: "atomic",
    terminal: eventType.endsWith(".committed") || eventType.endsWith(".selected"),
  }
  return normalizeEventFrame(
    JSON.parse(
      JSON.stringify({
        schema: "zyra.event-ingress-frame/v1",
        kind: "event",
        source: "delta",
        generation: 1,
        taskId: value.task_id,
        sequence,
        previousSequence: Math.max(0, sequence - 1),
        eventId,
        eventType,
        correlationId: `probe-${value.task_id}`,
        causationId,
        observedAtMs: 10_000 + sequence,
        cursor: `cursor.${sequence}`,
        event: raw,
      }),
    ),
    value.task_id,
    1,
  ).event
}

function project(value: ProbeCase) {
  const decisionId = String(value.decision.decision_id ?? "resource-decision")
  const graphEvent = event(
    value,
    1,
    "topology.graph.committed",
    "node",
    {
      node_id: "root",
      status: "running",
      graph_snapshot: value.graph.snapshot,
      graph_mutations: value.graph.mutations,
    },
    "root",
  )
  const schedulerEvent = event(
    value,
    2,
    "scheduler.route.selected",
    "scheduler",
    {
      ...value.decision,
      scheduler_id: decisionId,
      route_id: decisionId,
      resource_decision_id: decisionId,
      status: "completed",
      selected_candidate_id: value.decision.selected_manifest_id ?? null,
      selected_worker_id: value.decision.selected_worker ?? null,
      selected_backend_id: value.decision.selected_backend ?? null,
      selected_location: value.decision.selected_location ?? null,
      selected_model_id:
        (value.decision.model_split as JsonObject | undefined)?.primary ?? null,
      candidates: value.decision.alternatives ?? [],
      top_k: Array.isArray(value.decision.alternatives)
        ? value.decision.alternatives.length + 1
        : 1,
    },
    "execute",
    graphEvent.eventId,
  )
  const events = [graphEvent, schedulerEvent]
  if (value.requirement_change) {
    events.push(
      event(
        value,
        3,
        "requirement.changed",
        "task",
        {
          ...value.requirement_change,
          change_id:
            value.requirement_change.event_id ??
            `${value.task_id}:requirement-change`,
          requirement_text: value.requirement_change.text ?? "",
          local_replan: true,
          status: "needs_revision",
        },
        String(value.requirement_change.node_id ?? "root"),
        schedulerEvent.eventId,
      ),
    )
  }
  const lastSequence = events.at(-1)!.globalSequence
  const batch: IngressBatch = {
    taskId: value.task_id,
    generation: 1,
    events: Object.freeze(events),
    receipts: Object.freeze([]),
    cursor: `cursor.${lastSequence}`,
    fromSequence: 1,
    sequence: lastSequence,
    highWatermark: lastSequence,
    receivedAt: 20_000,
    transport: "long_poll",
    snapshot: true,
    caughtUp: true,
  }
  const store = new CanonicalProjectionStore({
    restore: false,
    autoPersist: false,
  })
  store.apply(batch)
  const view = buildTopologyProjection(store.state, value.task_id)
  const selected = view.routes.find(
    (route) => route.routeId === decisionId,
  )
  const execute = view.nodes.find((node) => node.id === "execute")
  return {
    graphRevision: view.graphRevision,
    commitRevision: view.commitRevision,
    nodeIds: view.nodes.map((node) => node.id),
    executeBackendRouteRef: execute?.backendRouteRef,
    openWorldMutationCount: view.metrics.openWorldMutationCount,
    selectedRoute: {
      routeId: selected?.routeId,
      workerId: selected?.selectedWorkerId,
      backendId: selected?.selectedBackendId,
      location: selected?.selectedLocation,
      modelId: selected?.selectedModelId,
      fixedCandidateSelection: selected?.fixedCandidateSelection,
    },
    placement: view.placements.find(
      (placement) => placement.routeId === decisionId,
    ),
    requirementChanges: view.requirementChanges.map((change) => ({
      changeId: change.changeId,
      affectedNodeIds: change.affectedNodeIds,
      replanNodeId: change.replanNodeId,
      localReplan: change.localReplan,
    })),
    evidenceEventIds:
      view.evidenceByEntity[`route:${decisionId}`]?.eventIds ?? [],
    diagnostics: view.diagnostics,
  }
}

const output = Object.fromEntries(
  Object.entries(input.cases).map(([name, value]) => [name, project(value)]),
)
console.log(JSON.stringify(output))
