import {
  DeliveryMode,
  RUNTIME_INTEGRATION_SCHEMA,
  SourceDomain,
  SourceRecordKind,
  assertMappedSource,
  buildSourceDraft,
  deliveryOptions,
  parseSourceBatch,
  parseSourceRecord,
  sourceStateDelta,
  type MappedSourceEvent,
  type SourceBatch,
  type SourceRecord,
  type SourceRecordKindValue,
} from "./integration-contracts.ts";
import {
  EventDurability,
  EventEffect,
  SenderKind,
  type RuntimeEventDraft,
} from "./contracts.ts";
import {
  boundedText,
  assertJsonValue,
  canonicalJson,
  cloneJson,
  digestJson,
  isPlainObject,
  optionalString,
  requireBoolean,
  requireFinite,
  requireInteger,
  requireRecord,
  requireString,
  type JsonValue,
} from "./canonical.ts";
import { EnvelopeValidationError } from "./errors.ts";

/**
 * Source-specific normalizer for the CodeWorker, permission, MCP, skill,
 * subagent, browser/gateway, artifact, control, compact, transport, backend,
 * and recovery paths.  This is deliberately not a generic pass-through: each
 * source kind has an explicit canonical event, sender, state transition, and
 * payload allowlist.  Removing this mapper makes typed OMP frames fail closed.
 */

interface MappingRule {
  kind: SourceRecordKindValue;
  domain: SourceRecord["domain"];
  eventType: string;
  stateDomain: string;
  operation: RuntimeEventDraft["stateDelta"] extends infer _T
    ? "set" | "merge" | "append" | "remove" | "transition" | "none"
    : never;
  path: (record: SourceRecord) => readonly string[];
  inline: (record: SourceRecord) => Readonly<Record<string, JsonValue>>;
  value?: (record: SourceRecord) => JsonValue;
  validate?: (record: SourceRecord) => void;
  override?: (record: SourceRecord) => Partial<RuntimeEventDraft>;
}

const text = (payload: Readonly<Record<string, JsonValue>>, key: string, fallback = ""): string => {
  const value = payload[key];
  return typeof value === "string" ? value : fallback;
};

const optionalText = (payload: Readonly<Record<string, JsonValue>>, key: string): string | undefined => {
  const value = payload[key];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
};

const integer = (payload: Readonly<Record<string, JsonValue>>, key: string, fallback = 0): number => {
  const value = payload[key];
  return typeof value === "number" && Number.isInteger(value) ? value : fallback;
};

const numberValue = (payload: Readonly<Record<string, JsonValue>>, key: string, fallback = 0): number => {
  const value = payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
};

const booleanValue = (payload: Readonly<Record<string, JsonValue>>, key: string, fallback = false): boolean => {
  const value = payload[key];
  return typeof value === "boolean" ? value : fallback;
};

const stringList = (payload: Readonly<Record<string, JsonValue>>, key: string): readonly string[] => {
  const value = payload[key];
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string").map((item) => item.slice(0, 256));
};

const objectValue = (payload: Readonly<Record<string, JsonValue>>, key: string): Readonly<Record<string, JsonValue>> => {
  const value = payload[key];
  return isPlainObject(value) ? cloneJson(value) : {};
};

const compactId = (record: SourceRecord, ...keys: string[]): string => {
  for (const key of keys) {
    const value = optionalText(record.payload, key);
    if (value) return value;
  }
  return record.subjectId ?? record.sourceId;
};

const requiredPayloadText = (record: SourceRecord, key: string): string => {
  const value = optionalText(record.payload, key);
  if (!value) {
    throw new EnvelopeValidationError("source payload is missing required text", {
      source_id: record.sourceId,
      source_kind: record.kind,
      field: key,
    });
  }
  return value;
};

const requireCause = (record: SourceRecord): void => {
  if (!record.causality.causationEventId) {
    throw new EnvelopeValidationError("source record requires canonical causation", {
      source_id: record.sourceId,
      source_kind: record.kind,
    });
  }
};

const requireRequestId = (record: SourceRecord): void => {
  if (!record.causality.requestId && !optionalText(record.payload, "request_id")) {
    throw new EnvelopeValidationError("source record requires request correlation", {
      source_id: record.sourceId,
      source_kind: record.kind,
    });
  }
};

const requireToolCallId = (record: SourceRecord): void => {
  if (!record.identity.toolCallId && !optionalText(record.payload, "tool_call_id")) {
    throw new EnvelopeValidationError("tool source record requires tool call identity", {
      source_id: record.sourceId,
      source_kind: record.kind,
    });
  }
};

const safeStatusInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => ({
  status: text(record.payload, "status", record.kind),
  phase: text(record.payload, "phase", "runtime"),
  attempt: integer(record.payload, "attempt", 0),
  source_event_id: record.sourceId,
});

const PRODUCT_TEXT_CHUNK_LIMIT_BYTES = 1_024;

const productTextChunk = (record: SourceRecord): string | undefined => {
  if (record.kind !== SourceRecordKind.TEXT_DELTA && record.kind !== SourceRecordKind.TEXT_ENDED) return undefined;
  for (const key of ["presentation_text", "content", "delta", "text", "final_text", "message"] as const) {
    const value = record.payload[key];
    if (typeof value === "string" && value.length > 0 && Buffer.byteLength(value, "utf8") <= PRODUCT_TEXT_CHUNK_LIMIT_BYTES) {
      return value;
    }
  }
  return undefined;
};

const streamInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => {
  const streamId = compactId(record, "stream_id", "message_id");
  const presentationText = productTextChunk(record);
  const declaredDeltaBytes = Math.max(0, integer(record.payload, "delta_bytes", 0));
  return {
    stream_id: streamId,
    assistant_message_id: compactId(record, "assistant_message_id", "message_id", "stream_id"),
    segment_index: integer(record.payload, "segment_index", 0),
    delta_bytes: declaredDeltaBytes || (record.kind === SourceRecordKind.TEXT_DELTA && presentationText !== undefined
      ? Buffer.byteLength(presentationText, "utf8")
      : 0),
    content_digest: optionalText(record.payload, "content_digest") ?? digestJson(record.payload),
    source_event_id: record.sourceId,
    ...(presentationText === undefined ? {} : { presentation_text: presentationText }),
  };
};

const toolIdentity = (record: SourceRecord): string => record.identity.toolCallId
  ?? optionalText(record.payload, "tool_call_id")
  ?? record.sourceId;

const toolBaseInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => ({
  tool_name: requiredPayloadText(record, "tool_name"),
  tool_call_id: toolIdentity(record),
  batch_index: integer(record.payload, "batch_index", 0),
  source_event_id: record.sourceId,
});

const permissionInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => ({
  permission_id: requiredPayloadText(record, "permission_id"),
  action_digest: optionalText(record.payload, "action_digest") ?? digestJson(objectValue(record.payload, "action")),
  decision: text(record.payload, "decision", "pending"),
  rule_id: text(record.payload, "rule_id", ""),
  expires_at: text(record.payload, "expires_at", ""),
  source_event_id: record.sourceId,
});

const mcpInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => ({
  server_id: text(record.payload, "server_id", "unknown-mcp-server"),
  request_id: record.causality.requestId ?? text(record.payload, "request_id", record.causality.correlationId),
  method: text(record.payload, "method", record.kind),
  capability: text(record.payload, "capability", ""),
  status: text(record.payload, "status", "unknown"),
  source_event_id: record.sourceId,
});

const skillInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => ({
  skill_id: requiredPayloadText(record, "skill_id"),
  invocation_id: compactId(record, "invocation_id"),
  status: text(record.payload, "status", record.kind),
  result_digest: optionalText(record.payload, "result_digest") ?? digestJson(record.payload),
  error_code: text(record.payload, "error_code", ""),
  source_event_id: record.sourceId,
});

const subagentInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => ({
  subagent_id: requiredPayloadText(record, "subagent_id"),
  parent_agent_id: text(record.payload, "parent_agent_id", record.identity.workerId ?? "root"),
  lifecycle: record.kind,
  progress_sequence: integer(record.payload, "progress_sequence", record.causality.producerSequence),
  yield_kind: text(record.payload, "yield_kind", ""),
  status: text(record.payload, "status", record.kind),
  source_event_id: record.sourceId,
});

const browserInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => ({
  browser_session_id: text(record.payload, "browser_session_id", record.identity.sessionId ?? ""),
  observation_kind: text(record.payload, "observation_kind", record.kind),
  url_digest: optionalText(record.payload, "url_digest") ?? digestJson(text(record.payload, "url", "")),
  page_state_digest: optionalText(record.payload, "page_state_digest") ?? digestJson(record.payload),
  error_code: text(record.payload, "error_code", ""),
  source_event_id: record.sourceId,
});

const artifactInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => ({
  artifact_count: record.artifactRefs.length,
  artifact_ids: record.artifactRefs.map((item) => item.artifactId),
  total_bytes: record.artifactRefs.reduce((sum, item) => sum + item.sizeBytes, 0),
  quarantine_reason: text(record.payload, "quarantine_reason", ""),
  source_event_id: record.sourceId,
});

const controlInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => ({
  control_id: requiredPayloadText(record, "control_id"),
  control_kind: requiredPayloadText(record, "control_kind"),
  decision: text(record.payload, "decision", "pending"),
  actor_id: text(record.payload, "actor_id", record.owner.ownerId),
  reason_code: text(record.payload, "reason_code", ""),
  source_event_id: record.sourceId,
});

const compactInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => ({
  compact_id: compactId(record, "compact_id", "checkpoint_id"),
  reason: text(record.payload, "reason", "budget"),
  before_tokens: integer(record.payload, "before_tokens", 0),
  after_tokens: integer(record.payload, "after_tokens", 0),
  restore_digest: text(record.payload, "restore_digest", ""),
  source_event_id: record.sourceId,
});

const transportInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => ({
  stream_id: compactId(record, "stream_id"),
  attempt: integer(record.payload, "attempt", 0),
  retry_after_ms: integer(record.payload, "retry_after_ms", 0),
  reason_code: text(record.payload, "reason_code", "transport"),
  last_global_sequence: integer(record.payload, "last_global_sequence", 0),
  source_event_id: record.sourceId,
});

const backendInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => ({
  dispatch_id: compactId(record, "dispatch_id"),
  backend_id: text(record.payload, "backend_id", "unknown"),
  placement: text(record.payload, "placement", "local"),
  attempt: integer(record.payload, "attempt", 0),
  error_code: text(record.payload, "error_code", ""),
  source_event_id: record.sourceId,
});

const recoveryInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => ({
  recovery_id: compactId(record, "recovery_id"),
  fault_id: text(record.payload, "fault_id", ""),
  strategy: text(record.payload, "strategy", "replan"),
  attempt: integer(record.payload, "attempt", 0),
  status: text(record.payload, "status", record.kind),
  source_event_id: record.sourceId,
});

const queryInline = (record: SourceRecord): Readonly<Record<string, JsonValue>> => ({
  query_id: compactId(record, "query_id"),
  delivery: text(record.payload, "delivery", "immediate"),
  priority: integer(record.payload, "priority", 0),
  context_digest: optionalText(record.payload, "context_digest") ?? digestJson(objectValue(record.payload, "context")),
  source_event_id: record.sourceId,
});

const rule = (
  kind: SourceRecordKindValue,
  domain: SourceRecord["domain"],
  eventType: string,
  stateDomain: string,
  operation: MappingRule["operation"],
  path: MappingRule["path"],
  inline: MappingRule["inline"],
  options: Pick<MappingRule, "value" | "validate" | "override"> = {},
): MappingRule => ({ kind, domain, eventType, stateDomain, operation, path, inline, ...options });

const rules: readonly MappingRule[] = [
  rule(SourceRecordKind.QUERY_ADMITTED, SourceDomain.CODEWORKER, "runtime.query.admitted", "session", "append", (r) => ["query", compactId(r, "query_id")], queryInline),
  rule(SourceRecordKind.QUERY_QUEUED, SourceDomain.CODEWORKER, "runtime.query.queued", "session", "transition", (r) => ["query", compactId(r, "query_id"), "status"], queryInline, { validate: requireCause, value: () => "queued" }),
  rule(SourceRecordKind.QUERY_PROMOTED, SourceDomain.CODEWORKER, "runtime.query.promoted", "session", "transition", (r) => ["query", compactId(r, "query_id"), "status"], queryInline, { validate: requireCause, value: () => "promoted" }),
  rule(SourceRecordKind.TURN_STARTED, SourceDomain.CODEWORKER, "runtime.turn.started", "session", "append", (r) => ["turn", compactId(r, "turn_id")], safeStatusInline),
  rule(SourceRecordKind.TURN_COMPLETED, SourceDomain.CODEWORKER, "runtime.turn.completed", "session", "transition", (r) => ["turn", compactId(r, "turn_id"), "status"], safeStatusInline, { validate: requireCause, value: () => "completed" }),
  rule(SourceRecordKind.TURN_FAILED, SourceDomain.CODEWORKER, "runtime.turn.failed", "session", "transition", (r) => ["turn", compactId(r, "turn_id"), "status"], safeStatusInline, { validate: requireCause, value: () => "failed" }),
  rule(SourceRecordKind.TEXT_STARTED, SourceDomain.CODEWORKER, "runtime.text.started", "stream", "append", (r) => ["text", compactId(r, "stream_id")], streamInline),
  rule(SourceRecordKind.TEXT_DELTA, SourceDomain.CODEWORKER, "runtime.text.delta", "stream", "none", (r) => ["text", compactId(r, "stream_id"), "delta"], streamInline, { override: () => ({ durability: EventDurability.LIVE_ONLY, effect: EventEffect.NON_EFFECTIVE }) }),
  rule(SourceRecordKind.TEXT_ENDED, SourceDomain.CODEWORKER, "runtime.text.ended", "stream", "transition", (r) => ["text", compactId(r, "stream_id"), "status"], streamInline, { value: () => "ended" }),
  rule(SourceRecordKind.REASONING_STARTED, SourceDomain.CODEWORKER, "runtime.reasoning.started", "stream", "append", (r) => ["reasoning", compactId(r, "stream_id")], streamInline),
  rule(SourceRecordKind.REASONING_DELTA, SourceDomain.CODEWORKER, "runtime.reasoning.delta", "stream", "none", (r) => ["reasoning", compactId(r, "stream_id"), "delta"], streamInline, { override: () => ({ durability: EventDurability.LIVE_ONLY, effect: EventEffect.NON_EFFECTIVE }) }),
  rule(SourceRecordKind.REASONING_ENDED, SourceDomain.CODEWORKER, "runtime.reasoning.ended", "stream", "transition", (r) => ["reasoning", compactId(r, "stream_id"), "status"], streamInline, { value: () => "ended" }),
  rule(SourceRecordKind.TOOL_INPUT_STARTED, SourceDomain.CODEWORKER, "runtime.tool.input.started", "tool", "append", (r) => ["tool", toolIdentity(r), "input"], toolBaseInline, { validate: requireToolCallId }),
  rule(SourceRecordKind.TOOL_INPUT_DELTA, SourceDomain.CODEWORKER, "runtime.tool.input.delta", "tool", "none", (r) => ["tool", toolIdentity(r), "input", "delta"], (r) => ({ ...toolBaseInline(r), input_digest: digestJson(r.payload) }), { validate: requireToolCallId, override: () => ({ durability: EventDurability.LIVE_ONLY, effect: EventEffect.NON_EFFECTIVE }) }),
  rule(SourceRecordKind.TOOL_INPUT_ENDED, SourceDomain.CODEWORKER, "runtime.tool.input.ended", "tool", "merge", (r) => ["tool", toolIdentity(r), "input"], (r) => ({ ...toolBaseInline(r), input_digest: optionalText(r.payload, "input_digest") ?? digestJson(r.payload) }), { validate: requireToolCallId }),
  rule(SourceRecordKind.TOOL_CALLED, SourceDomain.CODEWORKER, "runtime.tool.called", "tool", "append", (r) => ["tool", toolIdentity(r)], (r) => ({ ...toolBaseInline(r), input_digest: optionalText(r.payload, "input_digest") ?? digestJson(objectValue(r.payload, "arguments")), interruptible: booleanValue(r.payload, "interruptible", false), started: booleanValue(r.payload, "started", false) }), { validate: requireToolCallId, value: () => "called" }),
  rule(SourceRecordKind.TOOL_PROGRESS, SourceDomain.CODEWORKER, "runtime.tool.progress", "tool", "none", (r) => ["tool", toolIdentity(r), "progress"], (r) => ({ ...toolBaseInline(r), progress_sequence: integer(r.payload, "progress_sequence", r.causality.producerSequence), progress_digest: digestJson(r.payload), percent: numberValue(r.payload, "percent", 0) }), { validate: (r) => { requireCause(r); requireToolCallId(r); }, override: () => ({ sender: { kind: SenderKind.TOOL, id: "tool-runtime", capabilityRefs: ["tool.execute"] }, effect: EventEffect.NON_EFFECTIVE }) }),
  rule(SourceRecordKind.TOOL_SUCCEEDED, SourceDomain.CODEWORKER, "runtime.tool.succeeded", "tool", "transition", (r) => ["tool", toolIdentity(r), "status"], (r) => ({ ...toolBaseInline(r), result_digest: optionalText(r.payload, "result_digest") ?? digestJson(r.payload), malformed: booleanValue(r.payload, "malformed", false) }), { validate: (r) => { requireCause(r); requireToolCallId(r); }, value: () => "succeeded", override: () => ({ sender: { kind: SenderKind.TOOL, id: "tool-runtime", capabilityRefs: ["tool.execute"] } }) }),
  rule(SourceRecordKind.TOOL_FAILED, SourceDomain.CODEWORKER, "runtime.tool.failed", "tool", "transition", (r) => ["tool", toolIdentity(r), "status"], (r) => ({ ...toolBaseInline(r), error_code: text(r.payload, "error_code", "tool_error"), error_digest: digestJson(r.payload) }), { validate: (r) => { requireCause(r); requireToolCallId(r); }, value: () => "failed", override: () => ({ sender: { kind: SenderKind.TOOL, id: "tool-runtime", capabilityRefs: ["tool.execute"] } }) }),
  rule(SourceRecordKind.TOOL_CANCELLED, SourceDomain.CODEWORKER, "runtime.tool.cancelled", "tool", "transition", (r) => ["tool", toolIdentity(r), "status"], (r) => ({ ...toolBaseInline(r), cancel_reason: text(r.payload, "cancel_reason", "interrupt"), started: booleanValue(r.payload, "started", false) }), { validate: (r) => { requireCause(r); requireToolCallId(r); if (booleanValue(r.payload, "started", false)) throw new EnvelopeValidationError("interrupt may cancel only not-started tools", { tool_call_id: toolIdentity(r) }); }, value: () => "cancelled" }),

  rule(SourceRecordKind.PERMISSION_REQUESTED, SourceDomain.PERMISSION, "runtime.permission.requested", "permission", "append", (r) => ["permission", compactId(r, "permission_id")], permissionInline, { value: () => "requested" }),
  rule(SourceRecordKind.PERMISSION_PENDING, SourceDomain.PERMISSION, "runtime.permission.pending", "permission", "transition", (r) => ["permission", compactId(r, "permission_id"), "status"], permissionInline, { validate: requireCause, value: () => "pending", override: () => ({ sender: { kind: SenderKind.CONTROL, id: "permission-runtime", capabilityRefs: ["control.decide"] } }) }),
  rule(SourceRecordKind.PERMISSION_ALLOWED, SourceDomain.PERMISSION, "runtime.permission.allowed", "permission", "transition", (r) => ["permission", compactId(r, "permission_id"), "status"], permissionInline, { validate: requireCause, value: () => "allowed", override: () => ({ sender: { kind: SenderKind.CONTROL, id: "permission-runtime", capabilityRefs: ["control.decide"] } }) }),
  rule(SourceRecordKind.PERMISSION_DENIED, SourceDomain.PERMISSION, "runtime.permission.denied", "permission", "transition", (r) => ["permission", compactId(r, "permission_id"), "status"], permissionInline, { validate: requireCause, value: () => "denied", override: () => ({ sender: { kind: SenderKind.CONTROL, id: "permission-runtime", capabilityRefs: ["control.decide"] } }) }),
  rule(SourceRecordKind.PERMISSION_GRANT_CONSUMED, SourceDomain.PERMISSION, "runtime.permission.grant.consumed", "permission", "transition", (r) => ["permission", compactId(r, "permission_id"), "grant"], (r) => ({ ...permissionInline(r), grant_digest: requiredPayloadText(r, "grant_digest") }), { validate: requireCause, value: () => "consumed" }),

  rule(SourceRecordKind.MCP_CONNECTION, SourceDomain.MCP, "runtime.mcp.connection", "mcp", "set", (r) => ["mcp", compactId(r, "server_id"), "connection"], mcpInline),
  rule(SourceRecordKind.MCP_AUTH_REQUESTED, SourceDomain.MCP, "runtime.mcp.auth.requested", "mcp", "append", (r) => ["mcp", compactId(r, "server_id"), "auth"], mcpInline, { validate: requireRequestId }),
  rule(SourceRecordKind.MCP_AUTH_RESOLVED, SourceDomain.MCP, "runtime.mcp.auth.resolved", "mcp", "transition", (r) => ["mcp", compactId(r, "server_id"), "auth"], mcpInline, { validate: (r) => { requireCause(r); requireRequestId(r); }, override: () => ({ sender: { kind: SenderKind.CONTROL, id: "mcp-auth-control", capabilityRefs: ["control.decide"] } }) }),
  rule(SourceRecordKind.MCP_ELICITATION_REQUESTED, SourceDomain.MCP, "runtime.mcp.elicitation.requested", "mcp", "append", (r) => ["mcp", compactId(r, "server_id"), "elicitation"], mcpInline, { validate: requireRequestId }),
  rule(SourceRecordKind.MCP_ELICITATION_RESOLVED, SourceDomain.MCP, "runtime.mcp.elicitation.resolved", "mcp", "transition", (r) => ["mcp", compactId(r, "server_id"), "elicitation"], mcpInline, { validate: (r) => { requireCause(r); requireRequestId(r); }, override: () => ({ sender: { kind: SenderKind.CONTROL, id: "mcp-elicitation-control", capabilityRefs: ["control.decide"] } }) }),
  rule(SourceRecordKind.MCP_TOOL_CALLED, SourceDomain.MCP, "runtime.mcp.tool.called", "mcp", "append", (r) => ["mcp", compactId(r, "request_id"), "tool"], mcpInline, { validate: requireRequestId, override: (r) => ({ sender: { kind: SenderKind.WORKER, id: r.identity.workerId ?? "codeworker", capabilityRefs: ["worker.execute", "mcp.invoke"] } }) }),
  rule(SourceRecordKind.MCP_TOOL_RESULT, SourceDomain.MCP, "runtime.mcp.tool.result", "mcp", "transition", (r) => ["mcp", compactId(r, "request_id"), "tool"], (r) => normalizeMcpResultInline(r), { validate: (r) => { requireCause(r); requireRequestId(r); } }),
  rule(SourceRecordKind.MCP_RESOURCE_READ, SourceDomain.MCP, "runtime.mcp.resource.read", "mcp", "append", (r) => ["mcp", compactId(r, "server_id"), "resource"], mcpInline),
  rule(SourceRecordKind.MCP_PROMPT_LOADED, SourceDomain.MCP, "runtime.mcp.prompt.loaded", "mcp", "append", (r) => ["mcp", compactId(r, "server_id"), "prompt"], mcpInline),

  rule(SourceRecordKind.SKILL_STARTED, SourceDomain.SKILL, "runtime.skill.invocation.started", "skill", "append", (r) => ["skill", compactId(r, "invocation_id")], skillInline, { override: (r) => ({ sender: { kind: SenderKind.WORKER, id: r.identity.workerId ?? "codeworker", capabilityRefs: ["worker.execute", "skill.invoke"] } }) }),
  rule(SourceRecordKind.SKILL_COMPLETED, SourceDomain.SKILL, "runtime.skill.invocation.completed", "skill", "transition", (r) => ["skill", compactId(r, "invocation_id"), "status"], skillInline, { validate: requireCause, value: () => "completed" }),
  rule(SourceRecordKind.SKILL_FAILED, SourceDomain.SKILL, "runtime.skill.invocation.failed", "skill", "transition", (r) => ["skill", compactId(r, "invocation_id"), "status"], skillInline, { validate: requireCause, value: () => "failed" }),

  rule(SourceRecordKind.SUBAGENT_CREATED, SourceDomain.SUBAGENT, "runtime.subagent.created", "subagent", "append", (r) => ["subagent", compactId(r, "subagent_id")], subagentInline, { value: () => "created", override: (r) => ({ sender: { kind: SenderKind.WORKER, id: r.identity.workerId ?? "codeworker", capabilityRefs: ["worker.execute", "subagent.spawn"] } }) }),
  rule(SourceRecordKind.SUBAGENT_DISPATCHED, SourceDomain.SUBAGENT, "runtime.subagent.dispatched", "subagent", "transition", (r) => ["subagent", compactId(r, "subagent_id"), "status"], subagentInline, { validate: requireCause, value: () => "dispatched", override: () => ({ sender: { kind: SenderKind.SCHEDULER, id: "subagent-scheduler", capabilityRefs: ["scheduler.route"] } }) }),
  rule(SourceRecordKind.SUBAGENT_PROGRESS, SourceDomain.SUBAGENT, "runtime.subagent.progress", "subagent", "none", (r) => ["subagent", compactId(r, "subagent_id"), "progress"], subagentInline, { validate: requireCause, override: () => ({ effect: EventEffect.NON_EFFECTIVE }) }),
  rule(SourceRecordKind.SUBAGENT_YIELD, SourceDomain.SUBAGENT, "runtime.subagent.yield", "subagent", "append", (r) => ["subagent", compactId(r, "subagent_id"), "yield"], subagentInline, { validate: (r) => { requireCause(r); if (r.artifactRefs.length === 0 && !optionalText(r.payload, "result_digest")) throw new EnvelopeValidationError("typed subagent yield requires artifact or result digest", { source_id: r.sourceId }); } }),
  rule(SourceRecordKind.SUBAGENT_COMPLETED, SourceDomain.SUBAGENT, "runtime.subagent.completed", "subagent", "transition", (r) => ["subagent", compactId(r, "subagent_id"), "status"], subagentInline, { validate: requireCause, value: () => "completed" }),
  rule(SourceRecordKind.SUBAGENT_FAILED, SourceDomain.SUBAGENT, "runtime.subagent.failed", "subagent", "transition", (r) => ["subagent", compactId(r, "subagent_id"), "status"], subagentInline, { validate: requireCause, value: () => "failed" }),
  rule(SourceRecordKind.SUBAGENT_CANCELLED, SourceDomain.SUBAGENT, "runtime.subagent.cancelled", "subagent", "transition", (r) => ["subagent", compactId(r, "subagent_id"), "status"], subagentInline, { validate: requireCause, value: () => "cancelled", override: () => ({ sender: { kind: SenderKind.RUNTIME, id: "subagent-runtime", capabilityRefs: ["subagent.cancel"] } }) }),
  rule(SourceRecordKind.SUBAGENT_LATE_RESULT, SourceDomain.SUBAGENT, "runtime.subagent.late_result", "subagent", "append", (r) => ["subagent", compactId(r, "subagent_id"), "late_result"], subagentInline, { validate: requireCause }),

  rule(SourceRecordKind.BROWSER_OBSERVATION, SourceDomain.BROWSER, "runtime.browser.observation", "browser", "merge", (r) => ["browser", compactId(r, "browser_session_id"), "observation"], browserInline, { override: () => ({ effect: EventEffect.NON_EFFECTIVE }) }),
  rule(SourceRecordKind.BROWSER_FAILURE, SourceDomain.BROWSER, "runtime.browser.failure", "browser", "transition", (r) => ["browser", compactId(r, "browser_session_id"), "status"], browserInline, { value: () => "failed" }),
  rule(SourceRecordKind.WORKER_HEALTH, SourceDomain.GATEWAY, "runtime.worker.health", "liveness", "none", (r) => ["worker", r.identity.workerId ?? r.subjectId ?? "unknown", "health"], safeStatusInline, { override: (r) => ({ sender: { kind: SenderKind.WORKER, id: r.identity.workerId ?? "gateway-worker", capabilityRefs: ["worker.health"] }, effect: EventEffect.NON_EFFECTIVE }) }),
  rule(SourceRecordKind.ARTIFACT_COMMITTED, SourceDomain.ARTIFACT, "runtime.artifact.committed", "artifact", "append", (r) => ["artifact", compactId(r, "artifact_id")], artifactInline, { validate: (r) => { if (r.artifactRefs.length === 0) throw new EnvelopeValidationError("artifact commit requires artifact reference", { source_id: r.sourceId }); } }),
  rule(SourceRecordKind.ARTIFACT_QUARANTINED, SourceDomain.ARTIFACT, "runtime.artifact.quarantined", "artifact", "transition", (r) => ["artifact", compactId(r, "artifact_id"), "status"], artifactInline, { value: () => "quarantined" }),

  rule(SourceRecordKind.CONTROL_REQUESTED, SourceDomain.CONTROL, "runtime.control.requested", "control", "append", (r) => ["control", compactId(r, "control_id")], controlInline, { value: () => "requested", override: () => ({ sender: { kind: SenderKind.API, id: "zyra-api", capabilityRefs: ["control.request"] } }) }),
  rule(SourceRecordKind.CONTROL_ACCEPTED, SourceDomain.CONTROL, "runtime.control.accepted", "control", "transition", (r) => ["control", compactId(r, "control_id"), "status"], controlInline, { validate: requireCause, value: () => "accepted" }),
  rule(SourceRecordKind.CONTROL_REJECTED, SourceDomain.CONTROL, "runtime.control.rejected", "control", "transition", (r) => ["control", compactId(r, "control_id"), "status"], controlInline, { validate: requireCause, value: () => "rejected" }),
  rule(SourceRecordKind.CONTROL_COMPLETED, SourceDomain.CONTROL, "runtime.control.completed", "control", "transition", (r) => ["control", compactId(r, "control_id"), "status"], controlInline, { validate: requireCause, value: () => "completed", override: () => ({ sender: { kind: SenderKind.RUNTIME, id: "control-executor", capabilityRefs: ["control.apply"] } }) }),

  rule(SourceRecordKind.COMPACT_STARTED, SourceDomain.COMPACT, "runtime.compact.started", "compact", "append", (r) => ["compact", compactId(r, "compact_id")], compactInline, { value: () => "started" }),
  rule(SourceRecordKind.COMPACT_COMPLETED, SourceDomain.COMPACT, "runtime.compact.completed", "compact", "transition", (r) => ["compact", compactId(r, "compact_id"), "status"], compactInline, { validate: requireCause, value: () => "completed" }),
  rule(SourceRecordKind.COMPACT_RESTORE, SourceDomain.COMPACT, "runtime.compact.restore", "compact", "set", (r) => ["compact", compactId(r, "checkpoint_id"), "restore"], compactInline, { value: (r) => optionalText(r.payload, "restore_digest") ?? digestJson(r.payload) }),

  rule(SourceRecordKind.API_STREAM_RETRY, SourceDomain.API_TRANSPORT, "runtime.api.stream.retry", "transport", "none", (r) => ["transport", compactId(r, "stream_id"), "retry"], transportInline, { validate: requireCause, override: () => ({ effect: EventEffect.NON_EFFECTIVE }) }),
  rule(SourceRecordKind.API_STREAM_DISCONNECTED, SourceDomain.API_TRANSPORT, "runtime.api.stream.disconnected", "transport", "transition", (r) => ["transport", compactId(r, "stream_id"), "status"], transportInline, { value: () => "disconnected" }),
  rule(SourceRecordKind.BACKEND_DISPATCH_REQUESTED, SourceDomain.BACKEND, "runtime.backend.dispatch.requested", "dispatch", "append", (r) => ["dispatch", compactId(r, "dispatch_id")], backendInline, { value: () => "requested", override: () => ({ sender: { kind: SenderKind.SCHEDULER, id: "backend-scheduler", capabilityRefs: ["scheduler.route"] } }) }),
  rule(SourceRecordKind.BACKEND_DISPATCH_ACCEPTED, SourceDomain.BACKEND, "runtime.backend.dispatch.accepted", "dispatch", "transition", (r) => ["dispatch", compactId(r, "dispatch_id"), "status"], backendInline, { validate: requireCause, value: () => "accepted", override: (r) => ({ sender: { kind: SenderKind.WORKER, id: r.identity.workerId ?? "backend-worker", capabilityRefs: ["worker.execute"] } }) }),
  rule(SourceRecordKind.BACKEND_DISPATCH_FAILED, SourceDomain.BACKEND, "runtime.backend.dispatch.failed", "dispatch", "transition", (r) => ["dispatch", compactId(r, "dispatch_id"), "status"], backendInline, { validate: requireCause, value: () => "failed", override: (r) => ({ sender: { kind: SenderKind.WORKER, id: r.identity.workerId ?? "backend-worker", capabilityRefs: ["worker.execute"] } }) }),
  rule(SourceRecordKind.BACKEND_FAILOVER, SourceDomain.BACKEND, "runtime.backend.failover", "dispatch", "transition", (r) => ["dispatch", compactId(r, "dispatch_id"), "backend"], backendInline, { validate: requireCause, value: (r) => text(r.payload, "backend_id", "unknown"), override: () => ({ sender: { kind: SenderKind.RECOVERY, id: "backend-recovery", capabilityRefs: ["recovery.plan"] } }) }),

  rule(SourceRecordKind.RECOVERY_REQUESTED, SourceDomain.RECOVERY, "runtime.recovery.requested", "recovery", "append", (r) => ["recovery", compactId(r, "recovery_id")], recoveryInline, { value: () => "requested", override: () => ({ sender: { kind: SenderKind.RUNTIME, id: "runtime-recovery", capabilityRefs: ["recovery.request"] } }) }),
  rule(SourceRecordKind.RECOVERY_PLANNED, SourceDomain.RECOVERY, "runtime.recovery.planned", "recovery", "transition", (r) => ["recovery", compactId(r, "recovery_id"), "status"], recoveryInline, { validate: requireCause, value: () => "planned" }),
  rule(SourceRecordKind.RECOVERY_COMPLETED, SourceDomain.RECOVERY, "runtime.recovery.completed", "recovery", "transition", (r) => ["recovery", compactId(r, "recovery_id"), "status"], recoveryInline, { validate: requireCause, value: () => "completed" }),
];

const ruleByKind = new Map<SourceRecordKindValue, MappingRule>(rules.map((item) => [item.kind, item]));

function normalizeMcpResultInline(record: SourceRecord): Readonly<Record<string, JsonValue>> {
  const payload = record.payload;
  const rawError = payload.is_error;
  const malformedIsError = typeof rawError !== "boolean" && rawError !== undefined;
  const content = payload.content;
  const contentValid = Array.isArray(content) && content.every((item) => {
    if (!isPlainObject(item)) return false;
    const type = item.type;
    return type === "text" || type === "image" || type === "resource" || type === "resource_link";
  });
  const malformed = malformedIsError || (content !== undefined && !contentValid);
  return {
    ...mcpInline(record),
    result_digest: optionalText(payload, "result_digest") ?? digestJson(payload),
    is_error: typeof rawError === "boolean" ? rawError : malformed,
    malformed,
    normalization_warning: malformed ? "malformed_mcp_result_normalized" : "",
    content_item_count: Array.isArray(content) ? content.length : 0,
  };
}

function validateDomain(record: SourceRecord, ruleValue: MappingRule): void {
  if (record.domain !== ruleValue.domain) {
    throw new EnvelopeValidationError("source kind is emitted by the wrong domain owner", {
      source_id: record.sourceId,
      source_kind: record.kind,
      expected_domain: ruleValue.domain,
      actual_domain: record.domain,
    });
  }
}

function validateDelivery(record: SourceRecord, eventType: string): void {
  const streamDelta = eventType === "runtime.text.delta"
    || eventType === "runtime.reasoning.delta"
    || eventType === "runtime.tool.input.delta";
  if (streamDelta && record.delivery.mode !== DeliveryMode.LIVE_ONLY) {
    throw new EnvelopeValidationError("stream delta must use live-only delivery", {
      source_id: record.sourceId,
      event_type: eventType,
      mode: record.delivery.mode,
    });
  }
  if (!streamDelta && record.delivery.mode === DeliveryMode.LIVE_ONLY) {
    throw new EnvelopeValidationError("durable state transition cannot use live-only delivery", {
      source_id: record.sourceId,
      event_type: eventType,
    });
  }
  if (record.delivery.mode === DeliveryMode.CRITICAL_BROADCAST) {
    const allowlist = new Set([
      "runtime.task.failed",
      "runtime.node.failed",
      "runtime.api.stream.disconnected",
      "runtime.backend.dispatch.failed",
      "runtime.backend.failover",
      "runtime.artifact.quarantined",
    ]);
    if (!allowlist.has(eventType)) {
      throw new EnvelopeValidationError("event is not allowlisted for critical broadcast", {
        source_id: record.sourceId,
        event_type: eventType,
      });
    }
  }
}

export class RuntimeSourceMapper {
  readonly schema = RUNTIME_INTEGRATION_SCHEMA;

  map(value: SourceRecord | unknown): MappedSourceEvent {
    const record = parseSourceRecord(value);
    const selected = ruleByKind.get(record.kind);
    if (!selected) {
      throw new EnvelopeValidationError("source kind has no canonical mapping", {
        source_id: record.sourceId,
        source_kind: record.kind,
      });
    }
    validateDomain(record, selected);
    validateDelivery(record, selected.eventType);
    selected.validate?.(record);
    const inline = selected.inline(record);
    const valueForDelta = selected.value?.(record);
    const delta = sourceStateDelta(
      record,
      selected.stateDomain,
      selected.operation,
      selected.path(record),
      valueForDelta,
    );
    const overrides = selected.override?.(record) ?? {};
    const draft = buildSourceDraft(record, selected.eventType, inline, delta, overrides);
    const warnings: string[] = [];
    if (selected.eventType === "runtime.mcp.tool.result" && inline.malformed === true) {
      warnings.push("malformed_mcp_result_normalized");
    }
    if (record.artifactRefs.length > 0) warnings.push("artifact_reference_preserved");
    if (record.delivery.mode === DeliveryMode.STORE_ONLY) warnings.push("delivery_deferred_to_catch_up");
    const mapped: MappedSourceEvent = {
      schema: RUNTIME_INTEGRATION_SCHEMA,
      source: record,
      draft,
      options: deliveryOptions(record),
      warnings,
    };
    return assertMappedSource(mapped);
  }

  mapBatch(value: SourceBatch | unknown): readonly MappedSourceEvent[] {
    const batch = parseSourceBatch(value);
    const mapped = batch.records.map((record) => this.map(record));
    if (batch.atomic && mapped.some((item) => item.draft.durability === EventDurability.LIVE_ONLY)) {
      throw new EnvelopeValidationError("atomic batch cannot contain live-only frames", { batch_id: batch.batchId });
    }
    const sourceIds = new Set<string>();
    for (const item of mapped) {
      if (sourceIds.has(item.source.sourceId)) {
        throw new EnvelopeValidationError("source batch contains duplicate source id", {
          batch_id: batch.batchId,
          source_id: item.source.sourceId,
        });
      }
      sourceIds.add(item.source.sourceId);
    }
    return mapped;
  }

  describe(): Record<string, JsonValue> {
    return {
      schema: this.schema,
      mapper: "RuntimeSourceMapper",
      rule_count: rules.length,
      source_kinds: rules.map((item) => item.kind).sort(),
      event_types: [...new Set(rules.map((item) => item.eventType))].sort(),
      domains: [...new Set(rules.map((item) => item.domain))].sort(),
      generic_fallback: false,
      typed_omp_frames_required: true,
      interrupt_semantics: "only-not-started-tool-calls-may-be-cancelled",
      mcp_malformed_result_policy: "normalize-with-request-correlation",
    };
  }
}

export interface OmpAgentFrame {
  type: string;
  id: string;
  runId: string;
  sessionId?: string;
  taskId: string;
  nodeId?: string;
  workerId: string;
  requestId: string;
  parentId?: string;
  toolCallId?: string;
  sequence: number;
  timestamp?: string;
  payload: Readonly<Record<string, JsonValue>>;
}

const OMP_KIND: Readonly<Record<string, SourceRecordKindValue>> = Object.freeze({
  query_admitted: SourceRecordKind.QUERY_ADMITTED,
  query_queued: SourceRecordKind.QUERY_QUEUED,
  query_promoted: SourceRecordKind.QUERY_PROMOTED,
  agent_start: SourceRecordKind.TURN_STARTED,
  agent_end: SourceRecordKind.TURN_COMPLETED,
  agent_error: SourceRecordKind.TURN_FAILED,
  message_start: SourceRecordKind.TEXT_STARTED,
  message_delta: SourceRecordKind.TEXT_DELTA,
  message_end: SourceRecordKind.TEXT_ENDED,
  reasoning_start: SourceRecordKind.REASONING_STARTED,
  reasoning_delta: SourceRecordKind.REASONING_DELTA,
  reasoning_end: SourceRecordKind.REASONING_ENDED,
  tool_input_start: SourceRecordKind.TOOL_INPUT_STARTED,
  tool_input_delta: SourceRecordKind.TOOL_INPUT_DELTA,
  tool_input_end: SourceRecordKind.TOOL_INPUT_ENDED,
  tool_execution_start: SourceRecordKind.TOOL_CALLED,
  tool_execution_update: SourceRecordKind.TOOL_PROGRESS,
  tool_execution_end: SourceRecordKind.TOOL_SUCCEEDED,
  tool_execution_error: SourceRecordKind.TOOL_FAILED,
  tool_execution_skipped: SourceRecordKind.TOOL_CANCELLED,
  subagent_created: SourceRecordKind.SUBAGENT_CREATED,
  subagent_dispatched: SourceRecordKind.SUBAGENT_DISPATCHED,
  subagent_progress: SourceRecordKind.SUBAGENT_PROGRESS,
  subagent_yield: SourceRecordKind.SUBAGENT_YIELD,
  subagent_completed: SourceRecordKind.SUBAGENT_COMPLETED,
  subagent_failed: SourceRecordKind.SUBAGENT_FAILED,
  subagent_cancelled: SourceRecordKind.SUBAGENT_CANCELLED,
  subagent_late_result: SourceRecordKind.SUBAGENT_LATE_RESULT,
});

export function parseOmpAgentFrame(value: unknown): OmpAgentFrame {
  const input = requireRecord(value, "OMP agent frame");
  const payloadInput = input.payload ?? {};
  const payload = requireRecord(payloadInput, "OMP agent frame payload");
  const type = requireString(input.type ?? input.frame_type, "OMP agent frame type", 128);
  if (!OMP_KIND[type]) {
    throw new EnvelopeValidationError("unsupported OMP agent frame", { frame_type: type });
  }
  assertJsonValue(payload, "OMP agent frame payload");
  return {
    type,
    id: requireString(input.id, "OMP agent frame id", 256),
    runId: requireString(input.runId ?? input.run_id, "OMP agent frame runId", 256),
    sessionId: optionalString(input.sessionId ?? input.session_id, "OMP agent frame sessionId", 256),
    taskId: requireString(input.taskId ?? input.task_id, "OMP agent frame taskId", 256),
    nodeId: optionalString(input.nodeId ?? input.node_id, "OMP agent frame nodeId", 256),
    workerId: requireString(input.workerId ?? input.worker_id ?? "codeworker", "OMP agent frame workerId", 256),
    requestId: requireString(input.requestId ?? input.request_id, "OMP agent frame requestId", 256),
    parentId: optionalString(input.parentId ?? input.parent_id, "OMP agent frame parentId", 256),
    toolCallId: optionalString(input.toolCallId ?? input.tool_call_id, "OMP agent frame toolCallId", 256),
    sequence: requireInteger(input.sequence ?? input.producer_sequence ?? 0, "OMP agent frame sequence"),
    timestamp: optionalString(input.timestamp ?? input.created_at, "OMP agent frame timestamp", 128),
    payload: cloneJson(payload),
  };
}

export function ompFrameToSourceRecord(value: OmpAgentFrame | unknown): SourceRecord {
  const frame = parseOmpAgentFrame(value);
  const kind = OMP_KIND[frame.type]!;
  const isLive = kind === SourceRecordKind.TEXT_DELTA
    || kind === SourceRecordKind.REASONING_DELTA
    || kind === SourceRecordKind.TOOL_INPUT_DELTA;
  const terminal = frame.type.endsWith("_end")
    || frame.type.endsWith("_error")
    || frame.type.endsWith("_completed")
    || frame.type.endsWith("_failed")
    || frame.type.endsWith("_cancelled")
    || frame.type === "tool_execution_skipped";
  const payload: Record<string, JsonValue> = {
    ...frame.payload,
    request_id: frame.requestId,
    tool_call_id: frame.toolCallId ?? (typeof frame.payload.tool_call_id === "string" ? frame.payload.tool_call_id : ""),
  };
  if (kind === SourceRecordKind.TOOL_SUCCEEDED && frame.payload.isError === true) {
    return parseSourceRecord({
      schema: "zyra.runtime-source-record/v1",
      sourceId: frame.id,
      kind: SourceRecordKind.TOOL_FAILED,
      domain: SourceDomain.CODEWORKER,
      occurredAt: frame.timestamp,
      identity: { runId: frame.runId, sessionId: frame.sessionId, taskId: frame.taskId, nodeId: frame.nodeId, workerId: frame.workerId, toolCallId: frame.toolCallId },
      owner: { domain: SourceDomain.CODEWORKER, ownerId: "CodeWorkerRuntime", committed: true, committedAt: frame.timestamp },
      causality: { correlationId: frame.requestId, causationEventId: frame.parentId, requestId: frame.requestId, parentId: frame.parentId, producerSequence: frame.sequence },
      delivery: { mode: DeliveryMode.TARGETED, dependencyRecipients: [], topK: 4 },
      subjectId: frame.workerId,
      summary: `tool ${frame.toolCallId ?? "unknown"} failed`,
      effective: true,
      payload: { ...payload, error_code: text(payload, "error_code", "tool_error") },
      evidenceRefs: [],
      artifactRefs: [],
      metadata: { omp_frame_type: frame.type },
    });
  }
  return parseSourceRecord({
    schema: "zyra.runtime-source-record/v1",
    sourceId: frame.id,
    kind,
    domain: SourceDomain.CODEWORKER,
    occurredAt: frame.timestamp,
    identity: {
      runId: frame.runId,
      sessionId: frame.sessionId,
      taskId: frame.taskId,
      nodeId: frame.nodeId,
      workerId: frame.workerId,
      toolCallId: frame.toolCallId,
    },
    owner: {
      domain: SourceDomain.CODEWORKER,
      ownerId: "CodeWorkerRuntime",
      transactionId: `omp:${frame.requestId}:${frame.sequence}`,
      storeRef: `runtime://codeworker/${frame.runId}/${frame.taskId}`,
      committed: !isLive,
      committedAt: frame.timestamp,
    },
    causality: {
      correlationId: frame.requestId,
      causationEventId: frame.parentId,
      requestId: frame.requestId,
      parentId: frame.parentId,
      producerSequence: frame.sequence,
    },
    delivery: {
      mode: isLive ? DeliveryMode.LIVE_ONLY : DeliveryMode.TARGETED,
      dependencyRecipients: [],
      topK: 4,
    },
    subjectId: frame.workerId,
    summary: boundedText(`${frame.type} for ${frame.toolCallId ?? frame.taskId}`, 1024),
    effective: isLive ? false : terminal || frame.type.endsWith("_start") || frame.type === "query_admitted",
    payload,
    evidenceRefs: [],
    artifactRefs: Array.isArray(frame.payload.artifact_refs) ? frame.payload.artifact_refs : [],
    metadata: {
      omp_frame_type: frame.type,
      omp_request_id: frame.requestId,
      omp_parent_id: frame.parentId ?? null,
    },
  });
}

export class OmpRuntimeFrameMapper {
  readonly sourceMapper: RuntimeSourceMapper;

  constructor(sourceMapper = new RuntimeSourceMapper()) {
    this.sourceMapper = sourceMapper;
  }

  map(value: OmpAgentFrame | unknown): MappedSourceEvent {
    return this.sourceMapper.map(ompFrameToSourceRecord(value));
  }

  mapMany(values: readonly unknown[]): readonly MappedSourceEvent[] {
    return values.map((value) => this.map(value));
  }

  describe(): Record<string, JsonValue> {
    return {
      schema: RUNTIME_INTEGRATION_SCHEMA,
      mapper: "OmpRuntimeFrameMapper",
      source_repository: "oh-my-pi",
      source_modules: ["packages/agent/src/agent-loop.ts", "packages/wire/src/index.ts"],
      frame_types: Object.keys(OMP_KIND).sort(),
      partial_updates_are_live_only: true,
      final_tool_results_are_durable: true,
      generic_fallback: false,
    };
  }
}

export function mappingCatalog(): readonly Readonly<Record<string, JsonValue>>[] {
  return rules.map((item) => ({
    source_kind: item.kind,
    source_domain: item.domain,
    event_type: item.eventType,
    state_domain: item.stateDomain,
    state_operation: item.operation,
  }));
}
