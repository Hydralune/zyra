import {
  EventDurability,
  EventEffect,
  MessageIntent,
  RecipientKind,
  SenderKind,
  TrustLevel,
  normalizeLegacyRecord,
  type LegacyEventRecord,
  type EventEffectValue,
  type MessageIntentValue,
  type RuntimeEventDraft,
  type SenderKindValue,
} from "./contracts.ts";
import {
  boundedText,
  digestJson,
  isPlainObject,
  normalizeIdentifier,
  optionalRecord,
  stableId,
  type JsonValue,
} from "./canonical.ts";

interface LegacyMapping {
  eventType: string;
  intent: MessageIntentValue;
  senderKind: SenderKindValue;
  senderId: string;
  effective: boolean;
  stateDomain: string;
  stateOperation: "set" | "merge" | "append" | "remove" | "transition" | "none";
  targetKind?: "api" | "worker" | "scheduler" | "artifact_store" | "recovery" | "control" | "memory" | "ui_projector" | "auditor";
  targetId?: string;
}

const LEGACY_MAP: Record<string, LegacyMapping> = {
  task_created: { eventType: "runtime.task.created", intent: MessageIntent.PLAN, senderKind: SenderKind.API, senderId: "zyra-api", effective: true, stateDomain: "task", stateOperation: "set" },
  node_created: { eventType: "runtime.node.created", intent: MessageIntent.PLAN, senderKind: SenderKind.SCHEDULER, senderId: "resource-scheduler", effective: true, stateDomain: "topology", stateOperation: "append" },
  node_updated: { eventType: "runtime.node.updated", intent: MessageIntent.STATUS, senderKind: SenderKind.SCHEDULER, senderId: "resource-scheduler", effective: true, stateDomain: "topology", stateOperation: "transition" },
  agent_message: { eventType: "runtime.agent.message", intent: MessageIntent.OBSERVATION, senderKind: SenderKind.RUNTIME, senderId: "agent-runtime", effective: true, stateDomain: "message", stateOperation: "append", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  artifact_written: { eventType: "runtime.artifact.committed", intent: MessageIntent.ARTIFACT, senderKind: SenderKind.ARTIFACT_STORE, senderId: "artifact-store", effective: true, stateDomain: "artifact", stateOperation: "append", targetKind: RecipientKind.ARTIFACT_STORE, targetId: "artifact-store" },
  skill_invoked: { eventType: "runtime.skill.invocation.started", intent: MessageIntent.SKILL, senderKind: SenderKind.WORKER, senderId: "code-worker", effective: true, stateDomain: "skill", stateOperation: "transition" },
  control_command: { eventType: "runtime.control.requested", intent: MessageIntent.CONTROL, senderKind: SenderKind.API, senderId: "zyra-api", effective: true, stateDomain: "control", stateOperation: "append", targetKind: RecipientKind.CONTROL, targetId: "control-runtime" },
  requirement_change: { eventType: "runtime.task.updated", intent: MessageIntent.STATUS, senderKind: SenderKind.RUNTIME, senderId: "requirement-runtime", effective: true, stateDomain: "requirement", stateOperation: "merge" },
  failure_injected: { eventType: "runtime.recovery.requested", intent: MessageIntent.RECOVERY, senderKind: SenderKind.RUNTIME, senderId: "fault-injector", effective: true, stateDomain: "recovery", stateOperation: "append", targetKind: RecipientKind.RECOVERY, targetId: "recovery-planner" },
  node_failed: { eventType: "runtime.node.failed", intent: MessageIntent.RECOVERY, senderKind: SenderKind.WORKER, senderId: "worker-runtime", effective: true, stateDomain: "topology", stateOperation: "transition", targetKind: RecipientKind.RECOVERY, targetId: "recovery-planner" },
  constraint_check: { eventType: "runtime.audit.finding", intent: MessageIntent.AUDIT, senderKind: SenderKind.SYSTEM, senderId: "constraint-runtime", effective: false, stateDomain: "audit", stateOperation: "none", targetKind: RecipientKind.AUDITOR, targetId: "runtime-auditor" },
  topology_route: { eventType: "runtime.topology.route", intent: MessageIntent.DISPATCH, senderKind: SenderKind.SCHEDULER, senderId: "resource-scheduler", effective: true, stateDomain: "topology", stateOperation: "transition", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  resource_decision: { eventType: "runtime.backend.dispatch.requested", intent: MessageIntent.DISPATCH, senderKind: SenderKind.SCHEDULER, senderId: "resource-scheduler", effective: true, stateDomain: "dispatch", stateOperation: "append", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  recovery_planned: { eventType: "runtime.recovery.requested", intent: MessageIntent.RECOVERY, senderKind: SenderKind.RUNTIME, senderId: "runtime", effective: true, stateDomain: "recovery", stateOperation: "append", targetKind: RecipientKind.RECOVERY, targetId: "recovery-planner" },
  worker_health: { eventType: "runtime.worker.health", intent: MessageIntent.STATUS, senderKind: SenderKind.WORKER, senderId: "worker-runtime", effective: false, stateDomain: "liveness", stateOperation: "none", targetKind: RecipientKind.SCHEDULER, targetId: "resource-scheduler" },
  evaluation: { eventType: "runtime.audit.finding", intent: MessageIntent.AUDIT, senderKind: SenderKind.SYSTEM, senderId: "evaluation-runtime", effective: false, stateDomain: "audit", stateOperation: "none", targetKind: RecipientKind.AUDITOR, targetId: "runtime-auditor" },
  budget_updated: { eventType: "runtime.task.updated", intent: MessageIntent.STATUS, senderKind: SenderKind.RUNTIME, senderId: "budget-runtime", effective: true, stateDomain: "budget", stateOperation: "merge" },
  mcp_config_changed: { eventType: "runtime.mcp.connection", intent: MessageIntent.MCP, senderKind: SenderKind.MCP, senderId: "mcp-runtime", effective: true, stateDomain: "mcp", stateOperation: "merge" },
  mcp_connection_changed: { eventType: "runtime.mcp.connection", intent: MessageIntent.MCP, senderKind: SenderKind.MCP, senderId: "mcp-runtime", effective: true, stateDomain: "mcp", stateOperation: "transition" },
  mcp_capabilities_changed: { eventType: "runtime.mcp.connection", intent: MessageIntent.MCP, senderKind: SenderKind.MCP, senderId: "mcp-runtime", effective: true, stateDomain: "mcp", stateOperation: "merge" },
  mcp_auth_changed: { eventType: "runtime.mcp.auth.requested", intent: MessageIntent.MCP, senderKind: SenderKind.MCP, senderId: "mcp-runtime", effective: true, stateDomain: "mcp", stateOperation: "transition", targetKind: RecipientKind.CONTROL, targetId: "control-runtime" },
  mcp_elicitation: { eventType: "runtime.mcp.elicitation.requested", intent: MessageIntent.MCP, senderKind: SenderKind.MCP, senderId: "mcp-runtime", effective: true, stateDomain: "mcp", stateOperation: "append", targetKind: RecipientKind.CONTROL, targetId: "control-runtime" },
  mcp_task_updated: { eventType: "runtime.mcp.connection", intent: MessageIntent.MCP, senderKind: SenderKind.MCP, senderId: "mcp-runtime", effective: true, stateDomain: "mcp", stateOperation: "merge" },
  mcp_instructions_changed: { eventType: "runtime.mcp.connection", intent: MessageIntent.MCP, senderKind: SenderKind.MCP, senderId: "mcp-runtime", effective: true, stateDomain: "mcp", stateOperation: "merge" },
  mcp_tool_result: { eventType: "runtime.mcp.tool.result", intent: MessageIntent.MCP, senderKind: SenderKind.MCP, senderId: "mcp-runtime", effective: true, stateDomain: "mcp", stateOperation: "append", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  subagent_task_created: { eventType: "runtime.subagent.created", intent: MessageIntent.SUBAGENT, senderKind: SenderKind.WORKER, senderId: "subagent-runtime", effective: true, stateDomain: "subagent", stateOperation: "append", targetKind: RecipientKind.SCHEDULER, targetId: "resource-scheduler" },
  subagent_task_updated: { eventType: "runtime.subagent.progress", intent: MessageIntent.SUBAGENT, senderKind: SenderKind.SUBAGENT, senderId: "subagent-runtime", effective: false, stateDomain: "subagent", stateOperation: "none", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  subagent_dispatched: { eventType: "runtime.subagent.dispatched", intent: MessageIntent.SUBAGENT, senderKind: SenderKind.SCHEDULER, senderId: "resource-scheduler", effective: true, stateDomain: "subagent", stateOperation: "transition", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  subagent_progress: { eventType: "runtime.subagent.progress", intent: MessageIntent.SUBAGENT, senderKind: SenderKind.SUBAGENT, senderId: "subagent-runtime", effective: false, stateDomain: "subagent", stateOperation: "none", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  subagent_completed: { eventType: "runtime.subagent.completed", intent: MessageIntent.SUBAGENT, senderKind: SenderKind.SUBAGENT, senderId: "subagent-runtime", effective: true, stateDomain: "subagent", stateOperation: "transition", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  subagent_failed: { eventType: "runtime.subagent.failed", intent: MessageIntent.SUBAGENT, senderKind: SenderKind.SUBAGENT, senderId: "subagent-runtime", effective: true, stateDomain: "subagent", stateOperation: "transition", targetKind: RecipientKind.RECOVERY, targetId: "recovery-planner" },
  subagent_cancelled: { eventType: "runtime.subagent.cancelled", intent: MessageIntent.SUBAGENT, senderKind: SenderKind.RUNTIME, senderId: "subagent-runtime", effective: true, stateDomain: "subagent", stateOperation: "transition", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  subagent_resumed: { eventType: "runtime.subagent.progress", intent: MessageIntent.SUBAGENT, senderKind: SenderKind.SUBAGENT, senderId: "subagent-runtime", effective: false, stateDomain: "subagent", stateOperation: "none", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  subagent_message: { eventType: "runtime.subagent.progress", intent: MessageIntent.SUBAGENT, senderKind: SenderKind.SUBAGENT, senderId: "subagent-runtime", effective: false, stateDomain: "message", stateOperation: "none", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  subagent_isolation: { eventType: "runtime.agent.message", intent: MessageIntent.OBSERVATION, senderKind: SenderKind.RUNTIME, senderId: "subagent-runtime", effective: true, stateDomain: "workspace", stateOperation: "transition", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  command_requested: { eventType: "runtime.control.requested", intent: MessageIntent.CONTROL, senderKind: SenderKind.API, senderId: "zyra-api", effective: true, stateDomain: "control", stateOperation: "append", targetKind: RecipientKind.CONTROL, targetId: "control-runtime" },
  command_validated: { eventType: "runtime.control.accepted", intent: MessageIntent.CONTROL, senderKind: SenderKind.CONTROL, senderId: "control-runtime", effective: true, stateDomain: "control", stateOperation: "transition", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  command_queued: { eventType: "runtime.control.accepted", intent: MessageIntent.CONTROL, senderKind: SenderKind.CONTROL, senderId: "control-runtime", effective: true, stateDomain: "control", stateOperation: "append", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  command_started: { eventType: "runtime.control.accepted", intent: MessageIntent.CONTROL, senderKind: SenderKind.CONTROL, senderId: "control-runtime", effective: true, stateDomain: "control", stateOperation: "transition", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  command_succeeded: { eventType: "runtime.control.completed", intent: MessageIntent.CONTROL, senderKind: SenderKind.RUNTIME, senderId: "control-runtime", effective: true, stateDomain: "control", stateOperation: "transition", targetKind: RecipientKind.API, targetId: "api-projection" },
  command_failed: { eventType: "runtime.recovery.requested", intent: MessageIntent.RECOVERY, senderKind: SenderKind.RUNTIME, senderId: "control-runtime", effective: true, stateDomain: "recovery", stateOperation: "append", targetKind: RecipientKind.RECOVERY, targetId: "recovery-planner" },
  command_cancelled: { eventType: "runtime.control.completed", intent: MessageIntent.CONTROL, senderKind: SenderKind.RUNTIME, senderId: "control-runtime", effective: true, stateDomain: "control", stateOperation: "transition", targetKind: RecipientKind.API, targetId: "api-projection" },
  command_registry_refreshed: { eventType: "runtime.agent.message", intent: MessageIntent.OBSERVATION, senderKind: SenderKind.RUNTIME, senderId: "control-runtime", effective: false, stateDomain: "control", stateOperation: "none", targetKind: RecipientKind.API, targetId: "api-projection" },
  prompt_queue_updated: { eventType: "runtime.query.queued", intent: MessageIntent.QUERY, senderKind: SenderKind.RUNTIME, senderId: "query-runtime", effective: true, stateDomain: "session", stateOperation: "append", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  side_question: { eventType: "runtime.agent.message", intent: MessageIntent.OBSERVATION, senderKind: SenderKind.RUNTIME, senderId: "zyra-api", effective: true, stateDomain: "message", stateOperation: "append", targetKind: RecipientKind.WORKER, targetId: "worker-runtime" },
  system_notice: { eventType: "runtime.agent.message", intent: MessageIntent.OBSERVATION, senderKind: SenderKind.RUNTIME, senderId: "zyra-system", effective: false, stateDomain: "message", stateOperation: "none", targetKind: RecipientKind.API, targetId: "api-projection" },
  browser_session_lifecycle: { eventType: "runtime.browser.observation", intent: MessageIntent.OBSERVATION, senderKind: SenderKind.WORKER, senderId: "browser-worker", effective: true, stateDomain: "browser", stateOperation: "transition", targetKind: RecipientKind.API, targetId: "api-projection" },
  browser_target_lifecycle: { eventType: "runtime.browser.observation", intent: MessageIntent.OBSERVATION, senderKind: SenderKind.WORKER, senderId: "browser-worker", effective: true, stateDomain: "browser", stateOperation: "transition", targetKind: RecipientKind.API, targetId: "api-projection" },
  browser_cdp_request: { eventType: "runtime.browser.observation", intent: MessageIntent.OBSERVATION, senderKind: SenderKind.WORKER, senderId: "browser-worker", effective: false, stateDomain: "browser", stateOperation: "none", targetKind: RecipientKind.API, targetId: "api-projection" },
  browser_runtime_diagnostic: { eventType: "runtime.browser.observation", intent: MessageIntent.OBSERVATION, senderKind: SenderKind.WORKER, senderId: "browser-worker", effective: false, stateDomain: "browser", stateOperation: "none", targetKind: RecipientKind.API, targetId: "api-projection" },
  terminal_session_lifecycle: { eventType: "runtime.agent.message", intent: MessageIntent.OBSERVATION, senderKind: SenderKind.RUNTIME, senderId: "terminal-runtime", effective: true, stateDomain: "terminal", stateOperation: "transition", targetKind: RecipientKind.API, targetId: "api-projection" },
  terminal_output: { eventType: "runtime.agent.message", intent: MessageIntent.OBSERVATION, senderKind: SenderKind.RUNTIME, senderId: "terminal-runtime", effective: false, stateDomain: "terminal", stateOperation: "none", targetKind: RecipientKind.API, targetId: "api-projection" },
  terminal_control: { eventType: "runtime.agent.message", intent: MessageIntent.OBSERVATION, senderKind: SenderKind.RUNTIME, senderId: "terminal-runtime", effective: true, stateDomain: "terminal", stateOperation: "transition", targetKind: RecipientKind.API, targetId: "api-projection" },
};

function payloadSummary(payload: Readonly<Record<string, JsonValue>>, eventType: string): string {
  for (const key of ["summary", "message", "title", "status", "reason", "error"]) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) return boundedText(value.trim(), 1024);
  }
  const nestedCandidates = [payload.event, payload.result, payload.decision, payload.worker, payload.command];
  for (const nested of nestedCandidates) {
    if (!isPlainObject(nested)) continue;
    for (const key of ["summary", "message", "status", "reason", "error"]) {
      const value = nested[key];
      if (typeof value === "string" && value.trim()) return boundedText(value.trim(), 1024);
    }
  }
  return `Legacy ${eventType} event normalized into the runtime event spine.`;
}

function payloadString(payload: Readonly<Record<string, JsonValue>>, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return undefined;
}

function legacyMapping(eventType: string): LegacyMapping {
  return LEGACY_MAP[eventType] ?? {
    eventType: "runtime.agent.message",
    intent: MessageIntent.OBSERVATION,
    senderKind: SenderKind.RUNTIME,
    senderId: "legacy-runtime",
    effective: false,
    stateDomain: "legacy",
    stateOperation: "none",
    targetKind: RecipientKind.AUDITOR,
    targetId: "runtime-auditor",
  };
}

export interface NormalizedLegacyEvent {
  record: LegacyEventRecord;
  draft: RuntimeEventDraft;
  sourcePayload: Readonly<Record<string, JsonValue>>;
  mapping: LegacyMapping;
}

export function normalizeLegacyEvent(value: unknown): NormalizedLegacyEvent {
  const record = normalizeLegacyRecord(value);
  const payload = optionalRecord(record.payload, "legacy.payload");
  const legacyType = String(record.event_type);
  const mapping = legacyMapping(legacyType);
  const eventId = record.event_id || stableId("event", record.run_id, record.task_id, legacyType, record.created_at, digestJson(payload));
  const sessionId = payloadString(payload, "session_id", "query_session_id", "browser_session_id");
  const workerId = payloadString(payload, "worker_id", "assigned_worker_id", "backend_id");
  const correlationId = payloadString(payload, "correlation_id", "request_id", "trace_id") ?? `run:${record.run_id}`;
  const controlId = payloadString(payload, "command_id", "control_id", "permission_id") ?? (mapping.eventType === "runtime.control.requested" ? eventId : undefined);
  const artifactCount = Array.isArray(payload.artifact_refs)
    ? payload.artifact_refs.length
    : Array.isArray(payload.artifacts)
      ? payload.artifacts.length
      : legacyType === "artifact_written" ? 1 : 0;
  const inline: Record<string, JsonValue> = {
    legacy_event_type: legacyType,
    legacy_payload_digest: digestJson(payload),
    source_event_id: eventId,
  };
  if (mapping.eventType === "runtime.task.created") inline.legacy_event_type = legacyType;
  if (mapping.eventType === "runtime.artifact.committed") inline.artifact_count = artifactCount;
  if (mapping.eventType === "runtime.control.requested") {
    inline.control_id = controlId ?? eventId;
    inline.control_kind = payloadString(payload, "name", "command", "command_name") ?? "legacy_control";
  }
  const target = mapping.targetKind && mapping.targetId
    ? { kind: mapping.targetKind, id: mapping.targetId, requiredCapabilities: [] }
    : undefined;
  const stateValue: JsonValue = {
    legacy_event_type: legacyType,
    payload_digest: digestJson(payload),
    node_id: record.node_id ?? null,
  };
  const draft: RuntimeEventDraft = {
    eventId,
    eventType: mapping.eventType,
    aggregateId: `run:${record.run_id}:task:${record.task_id}`,
    idempotencyKey: `legacy:${eventId}`,
    correlationId: normalizeIdentifier(correlationId, "legacy.correlation_id", 512),
    createdAt: String(record.created_at),
    durability: EventDurability.DURABLE,
    effect: mapping.effective ? EventEffect.EFFECTIVE : EventEffect.NON_EFFECTIVE,
    identity: {
      runId: String(record.run_id),
      sessionId,
      taskId: String(record.task_id),
      nodeId: record.node_id ?? undefined,
      workerId,
      commandId: controlId,
    },
    sender: {
      kind: mapping.senderKind,
      id: payloadString(payload, "sender_id", "actor_id", "worker_id") ?? mapping.senderId,
      role: payloadString(payload, "sender_role", "role"),
      capabilityRefs: [],
    },
    intent: mapping.intent,
    target,
    summary: payloadSummary(payload, legacyType),
    stateDelta: {
      domain: mapping.stateDomain,
      operation: mapping.stateOperation,
      path: [mapping.stateDomain, record.node_id ?? record.task_id ?? "task"],
      afterDigest: digestJson(stateValue),
      value: stateValue,
      effective: mapping.effective,
    },
    provenance: {
      sourceRepository: "zyra",
      sourceModule: "zyra_core.EventRecord",
      migrationRole: "zyra_owned",
      producerVersion: "pre-05C-compatibility",
      trust: TrustLevel.INTERNAL,
      normalizedFrom: "legacy-event-record",
      sourceEventId: eventId,
      sourceDigest: digestJson(payload),
    },
    inline,
    sourceBytes: Buffer.byteLength(JSON.stringify(payload), "utf8"),
    metadata: {
      compatibility_projection: true,
      legacy_event_type: legacyType,
      legacy_unknown_type: !(legacyType in LEGACY_MAP),
      old_event_record_remains_projection: true,
    },
  };
  return { record, draft, sourcePayload: payload, mapping };
}

export interface OmpFrameInput {
  frame_type?: unknown;
  type?: unknown;
  id?: unknown;
  request_id?: unknown;
  parent_id?: unknown;
  run_id?: unknown;
  session_id?: unknown;
  task_id?: unknown;
  worker_id?: unknown;
  agent_id?: unknown;
  tool_call_id?: unknown;
  payload?: unknown;
  data?: unknown;
  timestamp?: unknown;
}

export function normalizeOmpFrame(value: OmpFrameInput): RuntimeEventDraft {
  const frameType = String(value.frame_type ?? value.type ?? "unknown").toLowerCase();
  const runId = String(value.run_id ?? "run-unknown");
  const sessionId = value.session_id ? String(value.session_id) : undefined;
  const taskId = String(value.task_id ?? "task-unknown");
  const sourcePayload = isPlainObject(value.payload) ? value.payload : isPlainObject(value.data) ? value.data : { value: value.data as JsonValue };
  const eventId = String(value.id ?? stableId("omp", frameType, value.request_id ?? "", digestJson(sourcePayload)));
  const correlationId = String(value.request_id ?? sessionId ?? runId);
  const parentId = value.parent_id ? String(value.parent_id) : undefined;
  let eventType = "runtime.agent.message";
  let intent: MessageIntentValue = MessageIntent.OBSERVATION;
  let effect: EventEffectValue = EventEffect.NON_EFFECTIVE;
  if (frameType === "turn_start" || frameType === "agent_start") {
    eventType = "runtime.turn.started";
    intent = MessageIntent.STATUS;
    effect = EventEffect.EFFECTIVE;
  } else if (frameType === "turn_end" || frameType === "agent_end" || frameType === "final") {
    eventType = "runtime.turn.completed";
    intent = MessageIntent.STATUS;
    effect = EventEffect.EFFECTIVE;
  } else if (frameType.includes("tool") && (frameType.includes("call") || frameType.includes("start"))) {
    eventType = "runtime.tool.called";
    intent = MessageIntent.TOOL_CALL;
    effect = EventEffect.EFFECTIVE;
  } else if (frameType.includes("tool") && (frameType.includes("progress") || frameType.includes("update"))) {
    eventType = "runtime.tool.progress";
    intent = MessageIntent.STATUS;
  } else if (frameType.includes("tool") && (frameType.includes("result") || frameType.includes("end"))) {
    const failed = sourcePayload.error !== undefined || sourcePayload.is_error === true || sourcePayload.status === "failed";
    eventType = failed ? "runtime.tool.failed" : "runtime.tool.succeeded";
    intent = MessageIntent.TOOL_RESULT;
    effect = EventEffect.EFFECTIVE;
  } else if (frameType.includes("subagent") && frameType.includes("progress")) {
    eventType = "runtime.subagent.progress";
    intent = MessageIntent.SUBAGENT;
  } else if (frameType.includes("subagent") && frameType.includes("yield")) {
    eventType = "runtime.subagent.yield";
    intent = MessageIntent.SUBAGENT;
    effect = EventEffect.EFFECTIVE;
  } else if (frameType.includes("subagent") && frameType.includes("late")) {
    eventType = "runtime.subagent.late_result";
    intent = MessageIntent.SUBAGENT;
    effect = EventEffect.EFFECTIVE;
  } else if (frameType.includes("subagent") && (frameType.includes("fail") || frameType.includes("error"))) {
    eventType = "runtime.subagent.failed";
    intent = MessageIntent.SUBAGENT;
    effect = EventEffect.EFFECTIVE;
  } else if (frameType.includes("subagent") && (frameType.includes("cancel") || frameType.includes("abort"))) {
    eventType = "runtime.subagent.cancelled";
    intent = MessageIntent.SUBAGENT;
    effect = EventEffect.EFFECTIVE;
  } else if (frameType.includes("subagent") && (frameType.includes("result") || frameType.includes("complete") || frameType.includes("end"))) {
    eventType = "runtime.subagent.completed";
    intent = MessageIntent.SUBAGENT;
    effect = EventEffect.EFFECTIVE;
  } else if (frameType.includes("compact") && frameType.includes("start")) {
    eventType = "runtime.compact.started";
    intent = MessageIntent.COMPACT;
    effect = EventEffect.EFFECTIVE;
  } else if (frameType.includes("compact") && (frameType.includes("end") || frameType.includes("complete"))) {
    eventType = "runtime.compact.completed";
    intent = MessageIntent.COMPACT;
    effect = EventEffect.EFFECTIVE;
  } else if (frameType.includes("partial") || frameType.includes("delta")) {
    eventType = "runtime.text.delta";
    intent = MessageIntent.OBSERVATION;
  }
  const toolCallId = value.tool_call_id ? String(value.tool_call_id) : payloadString(sourcePayload as Record<string, JsonValue>, "tool_call_id", "call_id");
  const inline: Record<string, JsonValue> = {
    omp_frame_type: frameType,
    request_id: correlationId,
    source_digest: digestJson(sourcePayload),
  };
  if (eventType === "runtime.tool.called") {
    inline.tool_name = payloadString(sourcePayload as Record<string, JsonValue>, "tool_name", "name") ?? "unknown";
    inline.input_digest = digestJson(sourcePayload);
    inline.tool_call_id = toolCallId ?? stableId("call", eventId);
  }
  if (["runtime.tool.progress", "runtime.tool.succeeded", "runtime.tool.failed"].includes(eventType)) {
    inline.tool_name = payloadString(sourcePayload as Record<string, JsonValue>, "tool_name", "name") ?? "unknown";
    inline.tool_call_id = toolCallId ?? stableId("call", parentId ?? eventId);
  }
  if (eventType === "runtime.tool.succeeded") inline.result_digest = digestJson(sourcePayload);
  if (eventType === "runtime.tool.failed") inline.error_code = payloadString(sourcePayload as Record<string, JsonValue>, "error_code", "error", "message") ?? "tool_failed";
  if (eventType === "runtime.text.delta") {
    inline.stream_id = payloadString(sourcePayload as Record<string, JsonValue>, "stream_id", "message_id") ?? correlationId;
    inline.delta = payloadString(sourcePayload as Record<string, JsonValue>, "delta", "text") ?? "";
  }
  return {
    eventId,
    eventType,
    aggregateId: `run:${runId}:task:${taskId}`,
    idempotencyKey: `omp:${eventId}`,
    correlationId,
    causationId: parentId,
    createdAt: typeof value.timestamp === "string" ? value.timestamp : undefined,
    durability: eventType.endsWith(".delta") ? EventDurability.LIVE_ONLY : EventDurability.DURABLE,
    effect,
    identity: {
      runId,
      sessionId,
      taskId,
      workerId: value.worker_id ? String(value.worker_id) : value.agent_id ? String(value.agent_id) : undefined,
      toolCallId: toolCallId ?? (eventType === "runtime.tool.called" ? String(inline.tool_call_id) : undefined),
    },
    sender: {
      kind: eventType.startsWith("runtime.subagent.")
        ? (eventType === "runtime.subagent.cancelled" ? SenderKind.RUNTIME : SenderKind.SUBAGENT)
        : eventType.startsWith("runtime.tool.") && eventType !== "runtime.tool.called"
          ? SenderKind.TOOL
          : eventType.startsWith("runtime.turn.") || eventType === "runtime.tool.called" || eventType === "runtime.text.delta"
            ? SenderKind.WORKER
            : SenderKind.RUNTIME,
      id: value.agent_id ? String(value.agent_id) : "omp-frame",
      capabilityRefs: ["omp.typed-frame"],
    },
    intent,
    summary: `OMP ${frameType} frame`,
    stateDelta: {
      domain: frameType.includes("subagent") ? "subagent" : frameType.includes("tool") ? "tool" : "session",
      operation: effect === EventEffect.EFFECTIVE ? "transition" : "none",
      path: [frameType],
      afterDigest: digestJson(sourcePayload),
      effective: effect === EventEffect.EFFECTIVE,
    },
    provenance: {
      sourceRepository: "oh-my-pi",
      sourceModule: "packages/coding-agent/src/modes/rpc + packages/agent/src/agent-loop.ts",
      migrationRole: "supplementary",
      producerVersion: "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
      trust: TrustLevel.EXTERNAL,
      normalizedFrom: "omp-rpc-frame",
      sourceEventId: eventId,
      sourceDigest: digestJson(sourcePayload),
    },
    inline,
    metadata: {
      parent_frame_id: parentId ?? "",
      stdout_is_not_canonical: true,
      registry_snapshot_is_not_canonical: true,
    },
  };
}

export function legacyEventTypes(): readonly string[] {
  return Object.keys(LEGACY_MAP).sort();
}

export function legacyMappingSummary(): Readonly<Record<string, JsonValue>> {
  return {
    schema: "zyra.legacy-event-normalization/v1",
    mapping_count: Object.keys(LEGACY_MAP).length,
    canonical_types: [...new Set(Object.values(LEGACY_MAP).map((item) => item.eventType))].sort(),
    unknown_fallback: "runtime.agent.message -> runtime-auditor",
    canonical_owner: "RuntimeEventSqliteStore",
  };
}
