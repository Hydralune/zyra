import { readFileSync } from "node:fs"
import {
  CanonicalProjectionStore,
  CausalTraceProjectionEngine,
  TraceProjectionError,
  TraceViewKind,
  buildCausalTraceProjection,
  navigationTargetsForTraceNode,
} from "../src/state/index.ts"
import {
  normalizeEventFrame,
  type IngressBatch,
  type IngressEvent,
  type JsonObject,
} from "../src/events/ingress/index.ts"

interface RuntimeRecord {
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
  events: RuntimeRecord[]
}

interface AdaptedRecord {
  source: RuntimeRecord
  eventType: string
  domain: string
  workerId?: string
  nodeId?: string
  causationId?: string
  terminal: boolean
  inline: JsonObject
}

function record(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject
    : {}
}

function text(value: JsonObject, ...keys: readonly string[]): string | undefined {
  for (const key of keys) {
    const candidate = value[key]
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim()
  }
  return undefined
}

function decision(payload: JsonObject): JsonObject {
  const resource = record(payload.resource_decision)
  return Object.keys(resource).length ? resource : record(payload.decision)
}

function node(payload: JsonObject): JsonObject {
  return record(payload.node)
}

function selectedWorker(payload: JsonObject): string | undefined {
  const selected = decision(payload)
  const recovery = record(payload.recovery_plan)
  return text(payload, "selected_worker") ??
    text(selected, "selected_worker", "selected") ??
    text(recovery, "selected_worker") ??
    text(node(payload), "assigned_worker_id")
}

function routeId(payload: JsonObject, fallback: string): string {
  const selected = decision(payload)
  return text(payload, "resource_decision_id", "route_id") ??
    text(selected, "decision_id", "resource_decision_id", "route_id") ??
    fallback
}

function adapt(records: readonly RuntimeRecord[]): readonly AdaptedRecord[] {
  const output: AdaptedRecord[] = []
  let previousEventId: string | undefined
  let failureEventId: string | undefined
  let failureWorkerId: string | undefined
  for (const source of records) {
    const payload = record(source.payload)
    const kind = source.event_type.toLowerCase()
    let value: Omit<AdaptedRecord, "source"> | undefined
    if (kind === "topology_route" || kind === "resource_decision") {
      const selected = decision(payload)
      const workerId = selectedWorker(payload)
      const route = routeId(payload, `route:${source.event_id}`)
      value = {
        eventType: kind === "topology_route"
          ? "scheduler.route.selected"
          : failureEventId
            ? "scheduler.placement.provider.retry"
            : "scheduler.placement.provider.selected",
        domain: "scheduler",
        workerId,
        nodeId: source.node_id ?? undefined,
        causationId: failureEventId ?? previousEventId,
        terminal: true,
        inline: {
          scheduler_id: route,
          route_id: route,
          placement_id: text(payload, "selected_location") ?? text(selected, "selected_location") ?? "local",
          provider_id: text(payload, "selected_backend") ?? text(selected, "selected_backend") ?? "local-provider",
          selected_worker_id: workerId ?? null,
          status: "completed",
        },
      }
    } else if (kind === "node_updated") {
      const projected = node(payload)
      const metadata = record(projected.metadata)
      const status = text(projected, "status")
      const workerId = text(projected, "assigned_worker_id")
      if (text(metadata, "stage") !== "execute" || !workerId || !["running", "completed"].includes(status ?? "")) {
        continue
      }
      value = {
        eventType: `worker.${status}`,
        domain: "worker",
        workerId,
        nodeId: source.node_id ?? undefined,
        causationId: previousEventId,
        terminal: status === "completed",
        inline: {
          worker_id: workerId,
          role: workerId,
          status: status!,
        },
      }
    } else if (kind === "node_failed") {
      const projected = node(payload)
      const workerId = text(projected, "assigned_worker_id") ?? selectedWorker(payload) ?? "unknown-worker"
      failureEventId = source.event_id
      failureWorkerId = workerId
      value = {
        eventType: "worker.fault.failed",
        domain: "worker",
        workerId,
        nodeId: source.node_id ?? undefined,
        causationId: previousEventId,
        terminal: true,
        inline: {
          worker_id: workerId,
          failure_id: source.event_id,
          error_code: "node_failure",
          status: "failed",
        },
      }
    } else if (kind === "recovery_planned") {
      const plan = record(payload.recovery_plan)
      const recoveryId = text(plan, "plan_id") ?? `recovery:${source.event_id}`
      const workerId = text(plan, "selected_worker") ?? selectedWorker(payload)
      value = {
        eventType: "recovery.restore.planned",
        domain: "recovery",
        workerId,
        nodeId: source.node_id ?? undefined,
        causationId: failureEventId ?? previousEventId,
        terminal: false,
        inline: {
          recovery_id: recoveryId,
          failure_id: failureEventId ?? null,
          previous_worker_id: failureWorkerId ?? null,
          replacement_worker_id: workerId ?? null,
          resumed_checkpoint_id: text(plan, "checkpoint_id") ?? `checkpoint:${source.task_id}`,
          strategy: "local_replan",
          attempt: 1,
          status: "recovering",
        },
      }
    }
    if (!value) continue
    output.push({ source, ...value })
    previousEventId = source.event_id
  }
  return Object.freeze(output)
}

function ingress(input: ProbeInput, value: AdaptedRecord, sequence: number): IngressEvent {
  const committedAt = new Date(Date.parse(value.source.created_at) + sequence).toISOString()
  const raw = {
    schema: "zyra.runtime-event/v1",
    eventId: value.source.event_id,
    eventType: value.eventType,
    eventVersion: 1,
    aggregateId: `task:${input.task_id}`,
    aggregateSequence: sequence,
    globalSequence: sequence,
    producerSequence: sequence,
    idempotencyKey: `m2-causal-trace:${value.source.event_id}`,
    correlationId: `run:${input.run_id}`,
    causationId: value.causationId,
    createdAt: value.source.created_at,
    committedAt,
    durability: "durable",
    effect: "effective",
    identity: {
      taskId: input.task_id,
      runId: input.run_id,
      sessionId: `session:${input.run_id}`,
      nodeId: value.nodeId,
      workerId: value.workerId,
      spanId: `span:${value.source.event_id}`,
      parentSpanId: value.causationId ? `span:${value.causationId}` : undefined,
    },
    sender: { kind: "runtime", id: value.workerId ?? "zyra-orchestration", capabilityRefs: [] },
    intent: "status",
    topKRecipients: [],
    summary: `${value.source.event_type} -> ${value.eventType}`,
    stateDelta: {
      domain: value.domain,
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
      sourceModule: "m2-causal-trace-integration-probe",
      migrationRole: "zyra_owned",
      producerVersion: "test",
      trust: "internal",
    },
    inline: value.inline,
    sourceBytes: 512,
    inlineBytes: 384,
    envelopeBytes: 4_096,
    contentDigest: `sha256:${"d".repeat(64)}`,
    metadata: { source_event_type: value.source.event_type },
    settlement: "atomic",
    terminal: value.terminal,
  }
  return normalizeEventFrame(JSON.parse(JSON.stringify({
    schema: "zyra.event-ingress-frame/v1",
    kind: "event",
    source: "delta",
    generation: 1,
    taskId: input.task_id,
    sequence,
    previousSequence: Math.max(0, sequence - 1),
    eventId: value.source.event_id,
    eventType: value.eventType,
    correlationId: `run:${input.run_id}`,
    causationId: value.causationId,
    observedAtMs: 500_000 + sequence,
    cursor: `cursor.${sequence}`,
    event: raw,
  })), input.task_id, 1).event
}

const path = process.argv[2]
if (!path) throw new Error("causal-trace-integration-probe requires an input JSON path")
const input = JSON.parse(readFileSync(path, "utf8")) as ProbeInput
const adapted = adapt(input.events)
const events = adapted.map((value, index) => ingress(input, value, index + 1))
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
  receivedAt: 600_000,
  transport: "long_poll",
  snapshot: true,
  caughtUp: true,
}
const store = new CanonicalProjectionStore({ restore: false, autoPersist: false })
store.apply(batch)
const projection = buildCausalTraceProjection(store.state, input.task_id)
const disabled = new CausalTraceProjectionEngine({ disabled: true })
let disabledCode: string | undefined
try {
  disabled.project(store.state, input.task_id)
} catch (error) {
  disabledCode = error instanceof TraceProjectionError ? error.code : String(error)
}
console.log(JSON.stringify({
  adaptedEventCount: events.length,
  sourceEventIds: adapted.map((value) => value.source.event_id),
  eventTypes: Object.values(projection.admissionByEvent).map((value) => value.event.eventType),
  semantics: [...new Set(projection.nodes.flatMap((node) => node.semantics))],
  workerIds: Object.keys(projection.indexes.nodeKeysByWorker),
  providerIds: [...new Set(projection.nodes.map((node) => node.refs.providerId).filter(Boolean))],
  failureIds: Object.keys(projection.indexes.nodeKeysByFailure),
  recoveryIds: Object.keys(projection.indexes.nodeKeysByRecovery),
  criticalPath: projection.criticalPath,
  diagnostics: projection.diagnostics,
  navigationViews: [...new Set(projection.nodes.flatMap((node) =>
    navigationTargetsForTraceNode(node).map((target) => target.view),
  ))],
  hasTimelineTarget: projection.nodes.some((node) =>
    navigationTargetsForTraceNode(node).some((target) => target.view === TraceViewKind.TIMELINE),
  ),
  disabledCode,
  disabledAudit: disabled.audit(),
}))
