import { readFileSync } from "node:fs"
import {
  CanonicalProjectionStore,
  TimelineProjectionError,
  WorkerCausalTimelineProjectionEngine,
  buildWorkerCausalTimeline,
} from "../src/state/index.ts"
import {
  normalizeEventFrame,
  type IngressBatch,
  type IngressEvent,
  type JsonObject,
} from "../src/events/ingress/index.ts"

interface M1EventRecord {
  event_id: string
  event_type: string
  run_id: string
  task_id: string
  node_id?: string | null
  created_at: string
  payload: JsonObject
}

interface ProbeInput {
  task_id: string
  run_id: string
  events: M1EventRecord[]
}

interface AdaptedEvent {
  eventType: string
  domain: string
  workerId?: string
  nodeId?: string
  causationId?: string
  terminal: boolean
  inline: JsonObject
}

function objectValue(value: unknown): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {}
  return value as JsonObject
}

function stringValue(
  value: JsonObject,
  ...keys: readonly string[]
): string | undefined {
  for (const key of keys) {
    const candidate = value[key]
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate
    }
  }
  return undefined
}

function stringArray(value: unknown): readonly string[] {
  if (!Array.isArray(value)) return Object.freeze([])
  return Object.freeze(
    value.filter((item): item is string => typeof item === "string"),
  )
}

function decisionPayload(payload: JsonObject): JsonObject {
  const resource = objectValue(payload.resource_decision)
  if (Object.keys(resource).length) return resource
  const decision = objectValue(payload.decision)
  if (Object.keys(decision).length) return decision
  return payload
}

function nodePayload(payload: JsonObject): JsonObject {
  return objectValue(payload.node)
}

function nodeMetadata(payload: JsonObject): JsonObject {
  return objectValue(nodePayload(payload).metadata)
}

function selectedWorker(payload: JsonObject): string | undefined {
  const decision = decisionPayload(payload)
  const plan = objectValue(payload.recovery_plan)
  return (
    stringValue(payload, "selected_worker") ??
    stringValue(decision, "selected_worker", "selected") ??
    stringValue(plan, "selected_worker") ??
    stringValue(nodePayload(payload), "assigned_worker_id")
  )
}

function decisionId(payload: JsonObject): string | undefined {
  const decision = decisionPayload(payload)
  return stringValue(
    decision,
    "decision_id",
    "resource_decision_id",
    "route_id",
  )
}

function leaseId(payload: JsonObject): string | undefined {
  const decision = decisionPayload(payload)
  const id = decisionId(payload)
  const manifest = stringValue(
    payload,
    "selected_manifest_id",
  ) ?? stringValue(decision, "selected_manifest_id")
  if (!id && !manifest) return undefined
  return `lease:${manifest ?? "worker"}:${id ?? "decision"}`
}

function routeInline(record: M1EventRecord): JsonObject {
  const decision = decisionPayload(record.payload)
  const routeId =
    decisionId(record.payload) ?? `route:${record.event_id}`
  const workerId = selectedWorker(record.payload)
  const selectedBackend =
    stringValue(record.payload, "selected_backend") ??
    stringValue(decision, "selected_backend")
  const selectedLocation =
    stringValue(record.payload, "selected_location") ??
    stringValue(decision, "selected_location")
  const selectedManifest =
    stringValue(record.payload, "selected_manifest_id") ??
    stringValue(decision, "selected_manifest_id")
  const modelSplit = objectValue(
    record.payload.model_split ?? decision.model_split,
  )
  return {
    scheduler_id: routeId,
    route_id: routeId,
    resource_decision_id: routeId,
    selected_worker_id: workerId ?? null,
    selected_candidate_id: selectedManifest ?? null,
    selected_backend_id: selectedBackend ?? null,
    selected_location: selectedLocation ?? null,
    selected_model_id: stringValue(modelSplit, "primary") ?? null,
    status: "completed",
    source_event_type: record.event_type,
    m1_event_id: record.event_id,
  }
}

function workerInline(
  record: M1EventRecord,
  status: string,
  workerId: string,
  lease: string | undefined,
): JsonObject {
  const metadata = nodeMetadata(record.payload)
  const route =
    decisionId(record.payload) ??
    stringValue(metadata, "resource_decision_id")
  return {
    worker_id: workerId,
    lease_id: lease ?? null,
    route_id: route ?? null,
    placement_id:
      stringValue(record.payload, "selected_location") ??
      stringValue(decisionPayload(record.payload), "selected_location") ??
      stringValue(metadata, "location") ??
      null,
    role: workerId,
    status,
    source_event_type: record.event_type,
    m1_event_id: record.event_id,
  }
}

function adaptRecords(records: readonly M1EventRecord[]): readonly {
  source: M1EventRecord
  adapted: AdaptedEvent
}[] {
  const result: { source: M1EventRecord; adapted: AdaptedEvent }[] = []
  const leaseByWorker = new Map<string, string>()
  let previousEventId: string | undefined
  let lastFailureEventId: string | undefined
  let replacementExpected = false
  for (const record of records) {
    const payload = objectValue(record.payload)
    const type = record.event_type.toLowerCase()
    let adapted: AdaptedEvent | undefined
    if (type === "topology_route") {
      const workerId = selectedWorker(payload)
      const cause = replacementExpected ? lastFailureEventId : previousEventId
      adapted = {
        eventType: "scheduler.route.selected",
        domain: "scheduler",
        workerId,
        nodeId: record.node_id ?? undefined,
        causationId: cause,
        terminal: true,
        inline: routeInline(record),
      }
    } else if (type === "resource_decision") {
      const workerId = selectedWorker(payload)
      if (!workerId) continue
      const lease = leaseId(payload)
      if (lease) leaseByWorker.set(workerId, lease)
      adapted = {
        eventType: replacementExpected
          ? "worker.lease.replaced"
          : "worker.admitted",
        domain: "worker",
        workerId,
        nodeId: record.node_id ?? undefined,
        causationId: replacementExpected
          ? lastFailureEventId
          : previousEventId,
        terminal: false,
        inline: workerInline(
          record,
          replacementExpected ? "recovering" : "admitted",
          workerId,
          lease,
        ),
      }
    } else if (type === "node_updated") {
      const node = nodePayload(payload)
      const metadata = objectValue(node.metadata)
      if (stringValue(metadata, "stage") !== "execute") continue
      const workerId = stringValue(node, "assigned_worker_id")
      const status = stringValue(node, "status")
      if (!workerId || (status !== "running" && status !== "completed")) {
        continue
      }
      adapted = {
        eventType: `worker.${status}`,
        domain: "worker",
        workerId,
        nodeId: record.node_id ?? undefined,
        causationId: previousEventId,
        terminal: status === "completed",
        inline: workerInline(
          record,
          status,
          workerId,
          leaseByWorker.get(workerId),
        ),
      }
    } else if (type === "node_failed") {
      const node = nodePayload(payload)
      const workerId =
        stringValue(node, "assigned_worker_id") ??
        selectedWorker(payload) ??
        "unknown-worker"
      lastFailureEventId = record.event_id
      replacementExpected = true
      adapted = {
        eventType: "watchdog.worker.failed",
        domain: "worker",
        workerId,
        nodeId: record.node_id ?? undefined,
        causationId: previousEventId,
        terminal: true,
        inline: {
          ...workerInline(
            record,
            "failed",
            workerId,
            leaseByWorker.get(workerId),
          ),
          failure_id: record.event_id,
          failure_kind: "node_failure",
          reason:
            stringValue(payload, "summary", "raw") ??
            "M1 runtime reported a worker failure.",
        },
      }
    } else if (type === "recovery_planned") {
      const plan = objectValue(payload.recovery_plan)
      const signal = objectValue(payload.failure_signal)
      const workerId =
        stringValue(plan, "selected_worker") ??
        stringValue(payload, "selected_worker")
      const recoveryId =
        stringValue(plan, "plan_id") ?? `recovery:${record.event_id}`
      adapted = {
        eventType: "recovery.started",
        domain: "recovery",
        workerId,
        nodeId: record.node_id ?? undefined,
        causationId: lastFailureEventId ?? previousEventId,
        terminal: false,
        inline: {
          recovery_id: recoveryId,
          failure_id:
            lastFailureEventId ??
            stringValue(plan, "failure_signal_id") ??
            stringValue(signal, "signal_id") ??
            null,
          strategy: stringArray(payload.actions).join(" + ") || "local_replan",
          attempt: 1,
          status: "recovering",
          worker_id: workerId ?? null,
          selected_manifest_id:
            stringValue(plan, "selected_manifest_id") ??
            stringValue(payload, "selected_manifest_id") ??
            null,
          source_event_type: record.event_type,
          m1_event_id: record.event_id,
        },
      }
    }
    if (!adapted) continue
    result.push({ source: record, adapted })
    previousEventId = record.event_id
  }
  return Object.freeze(result)
}

function ingressEvent(
  input: ProbeInput,
  sequence: number,
  value: { source: M1EventRecord; adapted: AdaptedEvent },
): IngressEvent {
  const { source, adapted } = value
  const committedAt = new Date(
    Date.parse(source.created_at) + sequence,
  ).toISOString()
  const raw = {
    schema: "zyra.runtime-event/v1",
    eventId: source.event_id,
    eventType: adapted.eventType,
    eventVersion: 1,
    aggregateId: `task:${input.task_id}`,
    aggregateSequence: sequence,
    globalSequence: sequence,
    producerSequence: sequence,
    idempotencyKey: `m1-worker-timeline:${source.event_id}`,
    correlationId: `run:${input.run_id}`,
    causationId: adapted.causationId,
    createdAt: source.created_at,
    committedAt,
    durability: "durable",
    effect: "effective",
    identity: {
      taskId: input.task_id,
      runId: input.run_id,
      sessionId: `session:${input.run_id}`,
      nodeId: adapted.nodeId,
      workerId: adapted.workerId,
      spanId: `span:${source.event_id}`,
    },
    sender: {
      kind: "runtime",
      id: adapted.workerId ?? "m1-orchestration-runtime",
      capabilityRefs: [],
    },
    intent: "status",
    topKRecipients: [],
    summary: `${source.event_type} projected as ${adapted.eventType}`,
    stateDelta: {
      domain: adapted.domain,
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
      sourceModule: "m1-worker-fault-recovery-projection-probe",
      migrationRole: "zyra_owned",
      producerVersion: "test",
      trust: "internal",
    },
    inline: adapted.inline,
    sourceBytes: 512,
    inlineBytes: 384,
    envelopeBytes: 4096,
    contentDigest: `sha256:${"d".repeat(64)}`,
    metadata: {
      m1_event_id: source.event_id,
      m1_event_type: source.event_type,
    },
    settlement: "atomic",
    terminal: adapted.terminal,
  }
  return normalizeEventFrame(
    JSON.parse(
      JSON.stringify({
        schema: "zyra.event-ingress-frame/v1",
        kind: "event",
        source: "delta",
        generation: 1,
        taskId: input.task_id,
        sequence,
        previousSequence: Math.max(0, sequence - 1),
        eventId: source.event_id,
        eventType: adapted.eventType,
        correlationId: `run:${input.run_id}`,
        causationId: adapted.causationId,
        observedAtMs: 100_000 + sequence,
        cursor: `cursor.${sequence}`,
        event: raw,
      }),
    ),
    input.task_id,
    1,
  ).event
}

const inputPath = process.argv[2]
if (!inputPath) {
  throw new Error("worker-causal-timeline-probe requires an input JSON path")
}
const input = JSON.parse(readFileSync(inputPath, "utf8")) as ProbeInput
const adapted = adaptRecords(input.events)
const events = adapted.map((value, index) =>
  ingressEvent(input, index + 1, value),
)
const lastSequence = events.at(-1)?.globalSequence ?? 0
const batch: IngressBatch = {
  taskId: input.task_id,
  generation: 1,
  events: Object.freeze(events),
  receipts: Object.freeze([]),
  cursor: `cursor.${lastSequence}`,
  fromSequence: events.length ? 1 : 0,
  sequence: lastSequence,
  highWatermark: lastSequence,
  receivedAt: 200_000,
  transport: "long_poll",
  snapshot: true,
  caughtUp: true,
}
const store = new CanonicalProjectionStore({
  restore: false,
  autoPersist: false,
})
store.apply(batch)
const projection = buildWorkerCausalTimeline(store.state, input.task_id)
const disabledEngine = new WorkerCausalTimelineProjectionEngine({
  disabled: true,
})
let disabledErrorCode: string | undefined
try {
  disabledEngine.project(store.state, input.task_id)
} catch (error) {
  disabledErrorCode =
    error instanceof TimelineProjectionError ? error.code : String(error)
}

console.log(
  JSON.stringify({
    adaptedEventCount: events.length,
    sourceEventIds: adapted.map(({ source }) => source.event_id),
    eventTypes: projection.events.map((event) => event.event.eventType),
    phases: [
      ...new Set([
        ...projection.events.map((facts) => facts.phase),
        ...projection.rows.map((row) => row.phase),
        ...projection.workerEpochs.flatMap((epoch) =>
          epoch.phases.map((phase) => phase.phase),
        ),
      ]),
    ],
    workerIds: [...new Set(
      projection.rows
        .map((row) => row.workerId)
        .filter((value): value is string => Boolean(value)),
    )],
    workerEpochs: projection.workerEpochs.map((epoch) => ({
      workerId: epoch.workerId,
      leaseId: epoch.leaseId,
      startPhase: epoch.phases[0]?.phase,
      terminalPhase: epoch.finalPhase,
      replacementEpochId: epoch.replacementEpochId,
      previousEpochId: epoch.previousEpochId,
    })),
    recoveryChains: projection.recoveryChains.map((chain) => ({
      recoveryId: chain.id,
      failureId: chain.failureId,
      workerIds: chain.workerIds,
      status: chain.resolved
        ? "resolved"
        : chain.exhausted
          ? "exhausted"
          : chain.attempts.at(-1)?.phase ?? "partial",
      attempts: chain.attempts.length,
      eventIds: chain.eventIds,
    })),
    rows: projection.rows.map((row) => ({
      key: row.key,
      phase: row.phase,
      kind: row.rowKind,
      workerId: row.workerId,
      failureId: row.failureId,
      recoveryId: row.recoveryId,
      eventIds: row.eventIds,
    })),
    failureRows: projection.rows
      .filter((row) => row.phase === "failed")
      .map((row) => ({
        key: row.key,
        workerId: row.workerId,
        failureId: row.failureId,
        eventIds: row.eventIds,
      })),
    recoveryRows: projection.rows
      .filter(
        (row) =>
          row.phase === "recovering" &&
          (row.rowKind === "recovery" || Boolean(row.recoveryId)),
      )
      .map((row) => ({
        key: row.key,
        workerId: row.workerId,
        recoveryId: row.recoveryId,
        failureId: row.failureId,
        eventIds: row.eventIds,
      })),
    criticalPath: {
      eventIds: projection.criticalPath.eventIds,
      workerIds: projection.criticalPath.workerIds,
      complete: projection.criticalPath.complete,
    },
    graph: {
      edgeCount: projection.graph.edges.length,
      explicitEdgeCount: projection.graph.edges.filter(
        (edge) => edge.explicit,
      ).length,
      failureRecoveryEdgeCount: projection.graph.edges.filter(
        (edge) =>
          edge.kind === "recovery" ||
          edge.kind === "lease-replacement",
      ).length,
    },
    diagnostics: projection.diagnostics,
    disabledErrorCode,
    disabledAudit: disabledEngine.audit(),
  }),
)
