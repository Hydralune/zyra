import { EventDurability, EventEffect, MessageIntent, RecipientKind, SenderKind, type EventDurabilityValue, type EventEffectValue, type MessageIntentValue, type RecipientKindValue, type SenderKindValue } from "./contracts.ts";
import { EnvelopeValidationError } from "./errors.ts";

export interface EventKindDefinition {
  type: string;
  version: number;
  intent: MessageIntentValue;
  durability: EventDurabilityValue;
  defaultEffect: EventEffectValue;
  senderKind: SenderKindValue;
  recipientKinds: readonly RecipientKindValue[];
  requiredInlineKeys: readonly string[];
  optionalInlineKeys: readonly string[];
  systemCriticalBroadcast: boolean;
  requiresCausation: boolean;
  isStreamDelta: boolean;
  terminalFor?: string;
  stateDomain: string;
}

function define(
  type: string,
  intent: MessageIntentValue,
  senderKind: SenderKindValue,
  recipientKinds: readonly RecipientKindValue[],
  options: Partial<Omit<EventKindDefinition, "type" | "intent" | "senderKind" | "recipientKinds">> = {},
): EventKindDefinition {
  return Object.freeze({
    type,
    version: options.version ?? 1,
    intent,
    durability: options.durability ?? EventDurability.DURABLE,
    defaultEffect: options.defaultEffect ?? EventEffect.EFFECTIVE,
    senderKind,
    recipientKinds: Object.freeze([...recipientKinds]),
    requiredInlineKeys: Object.freeze([...(options.requiredInlineKeys ?? [])]),
    optionalInlineKeys: Object.freeze([...(options.optionalInlineKeys ?? [])]),
    systemCriticalBroadcast: options.systemCriticalBroadcast ?? false,
    requiresCausation: options.requiresCausation ?? false,
    isStreamDelta: options.isStreamDelta ?? false,
    terminalFor: options.terminalFor,
    stateDomain: options.stateDomain ?? "runtime",
  });
}

const API = RecipientKind.API;
const WORKER = RecipientKind.WORKER;
const SCHEDULER = RecipientKind.SCHEDULER;
const ARTIFACT = RecipientKind.ARTIFACT_STORE;
const RECOVERY = RecipientKind.RECOVERY;
const MCP = RecipientKind.MCP;
const CONTROL = RecipientKind.CONTROL;
const MEMORY = RecipientKind.MEMORY;
const UI = RecipientKind.UI_PROJECTOR;
const AUDITOR = RecipientKind.AUDITOR;

const definitions: EventKindDefinition[] = [
  define("runtime.task.created", MessageIntent.PLAN, SenderKind.API, [SCHEDULER, UI], { requiredInlineKeys: ["legacy_event_type"], stateDomain: "task" }),
  define("runtime.task.updated", MessageIntent.STATUS, SenderKind.RUNTIME, [SCHEDULER, UI], { requiresCausation: true, stateDomain: "task" }),
  define("runtime.task.completed", MessageIntent.STATUS, SenderKind.RUNTIME, [API, UI, MEMORY], { requiresCausation: true, stateDomain: "task" }),
  define("runtime.task.failed", MessageIntent.RECOVERY, SenderKind.RUNTIME, [RECOVERY, SCHEDULER, UI], { requiresCausation: true, systemCriticalBroadcast: true, stateDomain: "task" }),
  define("runtime.node.created", MessageIntent.PLAN, SenderKind.SCHEDULER, [WORKER, UI], { stateDomain: "topology" }),
  define("runtime.node.updated", MessageIntent.STATUS, SenderKind.SCHEDULER, [WORKER, UI], { requiresCausation: true, stateDomain: "topology" }),
  define("runtime.node.failed", MessageIntent.RECOVERY, SenderKind.WORKER, [RECOVERY, SCHEDULER, UI], { requiresCausation: true, systemCriticalBroadcast: true, stateDomain: "topology" }),
  define("runtime.topology.route", MessageIntent.DISPATCH, SenderKind.SCHEDULER, [WORKER, AUDITOR], { stateDomain: "topology" }),
  define("runtime.agent.message", MessageIntent.OBSERVATION, SenderKind.RUNTIME, [WORKER, API, UI], { stateDomain: "message" }),
  define("runtime.worker.health", MessageIntent.STATUS, SenderKind.WORKER, [SCHEDULER], { defaultEffect: EventEffect.NON_EFFECTIVE, stateDomain: "liveness" }),
  define("runtime.browser.observation", MessageIntent.OBSERVATION, SenderKind.WORKER, [API, UI, ARTIFACT], { defaultEffect: EventEffect.NON_EFFECTIVE, stateDomain: "browser" }),
  define("runtime.browser.failure", MessageIntent.RECOVERY, SenderKind.WORKER, [RECOVERY, UI], { stateDomain: "browser" }),
  define("runtime.query.admitted", MessageIntent.QUERY, SenderKind.API, [WORKER], { requiredInlineKeys: ["delivery"], stateDomain: "session" }),
  define("runtime.query.queued", MessageIntent.QUERY, SenderKind.RUNTIME, [WORKER, UI], { requiresCausation: true, stateDomain: "session" }),
  define("runtime.query.promoted", MessageIntent.QUERY, SenderKind.RUNTIME, [WORKER], { requiresCausation: true, stateDomain: "session" }),
  define("runtime.turn.started", MessageIntent.STATUS, SenderKind.WORKER, [API, UI], { stateDomain: "session" }),
  define("runtime.turn.completed", MessageIntent.STATUS, SenderKind.WORKER, [API, UI, MEMORY], { requiresCausation: true, stateDomain: "session" }),
  define("runtime.turn.failed", MessageIntent.RECOVERY, SenderKind.WORKER, [RECOVERY, UI], { requiresCausation: true, stateDomain: "session" }),
  define("runtime.text.started", MessageIntent.OBSERVATION, SenderKind.WORKER, [API, UI], { defaultEffect: EventEffect.NON_EFFECTIVE, stateDomain: "stream" }),
  define("runtime.text.delta", MessageIntent.OBSERVATION, SenderKind.WORKER, [API, UI], { durability: EventDurability.LIVE_ONLY, defaultEffect: EventEffect.NON_EFFECTIVE, isStreamDelta: true, stateDomain: "stream" }),
  define("runtime.text.ended", MessageIntent.OBSERVATION, SenderKind.WORKER, [API, UI, MEMORY], { terminalFor: "runtime.text.started", stateDomain: "stream" }),
  define("runtime.reasoning.started", MessageIntent.OBSERVATION, SenderKind.WORKER, [API, UI], { defaultEffect: EventEffect.NON_EFFECTIVE, stateDomain: "stream" }),
  define("runtime.reasoning.delta", MessageIntent.OBSERVATION, SenderKind.WORKER, [API, UI], { durability: EventDurability.LIVE_ONLY, defaultEffect: EventEffect.NON_EFFECTIVE, isStreamDelta: true, stateDomain: "stream" }),
  define("runtime.reasoning.ended", MessageIntent.OBSERVATION, SenderKind.WORKER, [API, UI], { terminalFor: "runtime.reasoning.started", defaultEffect: EventEffect.NON_EFFECTIVE, stateDomain: "stream" }),
  define("runtime.tool.input.started", MessageIntent.TOOL_CALL, SenderKind.WORKER, [WORKER, UI], { requiredInlineKeys: ["tool_name"], stateDomain: "tool" }),
  define("runtime.tool.input.delta", MessageIntent.TOOL_CALL, SenderKind.WORKER, [UI], { durability: EventDurability.LIVE_ONLY, defaultEffect: EventEffect.NON_EFFECTIVE, isStreamDelta: true, stateDomain: "tool" }),
  define("runtime.tool.input.ended", MessageIntent.TOOL_CALL, SenderKind.WORKER, [WORKER, UI], { terminalFor: "runtime.tool.input.started", stateDomain: "tool" }),
  define("runtime.tool.called", MessageIntent.TOOL_CALL, SenderKind.WORKER, [WORKER, CONTROL, UI], { requiredInlineKeys: ["tool_name", "input_digest"], stateDomain: "tool" }),
  define("runtime.tool.progress", MessageIntent.STATUS, SenderKind.TOOL, [WORKER, UI], { requiresCausation: true, defaultEffect: EventEffect.NON_EFFECTIVE, stateDomain: "tool" }),
  define("runtime.tool.succeeded", MessageIntent.TOOL_RESULT, SenderKind.TOOL, [WORKER, ARTIFACT, UI, MEMORY], { requiresCausation: true, requiredInlineKeys: ["tool_name", "result_digest"], terminalFor: "runtime.tool.called", stateDomain: "tool" }),
  define("runtime.tool.failed", MessageIntent.TOOL_RESULT, SenderKind.TOOL, [WORKER, RECOVERY, UI], { requiresCausation: true, requiredInlineKeys: ["tool_name", "error_code"], terminalFor: "runtime.tool.called", stateDomain: "tool" }),
  define("runtime.tool.cancelled", MessageIntent.CONTROL, SenderKind.RUNTIME, [WORKER, UI], { requiresCausation: true, terminalFor: "runtime.tool.called", stateDomain: "tool" }),
  define("runtime.permission.requested", MessageIntent.PERMISSION, SenderKind.RUNTIME, [CONTROL, UI], { requiredInlineKeys: ["permission_id", "action_digest"], stateDomain: "permission" }),
  define("runtime.permission.allowed", MessageIntent.PERMISSION, SenderKind.CONTROL, [WORKER, AUDITOR], { requiresCausation: true, requiredInlineKeys: ["permission_id", "decision"], terminalFor: "runtime.permission.requested", stateDomain: "permission" }),
  define("runtime.permission.denied", MessageIntent.PERMISSION, SenderKind.CONTROL, [WORKER, RECOVERY, AUDITOR], { requiresCausation: true, requiredInlineKeys: ["permission_id", "decision"], terminalFor: "runtime.permission.requested", stateDomain: "permission" }),
  define("runtime.permission.pending", MessageIntent.PERMISSION, SenderKind.CONTROL, [API, UI], { requiresCausation: true, requiredInlineKeys: ["permission_id"], stateDomain: "permission" }),
  define("runtime.permission.grant.consumed", MessageIntent.PERMISSION, SenderKind.RUNTIME, [AUDITOR], { requiresCausation: true, requiredInlineKeys: ["permission_id", "grant_digest"], stateDomain: "permission" }),
  define("runtime.mcp.connection", MessageIntent.MCP, SenderKind.MCP, [WORKER, UI], { stateDomain: "mcp" }),
  define("runtime.mcp.auth.requested", MessageIntent.MCP, SenderKind.MCP, [CONTROL, UI], { stateDomain: "mcp" }),
  define("runtime.mcp.auth.resolved", MessageIntent.MCP, SenderKind.CONTROL, [MCP, AUDITOR], { requiresCausation: true, stateDomain: "mcp" }),
  define("runtime.mcp.elicitation.requested", MessageIntent.MCP, SenderKind.MCP, [CONTROL, UI], { stateDomain: "mcp" }),
  define("runtime.mcp.elicitation.resolved", MessageIntent.MCP, SenderKind.CONTROL, [MCP, AUDITOR], { requiresCausation: true, stateDomain: "mcp" }),
  define("runtime.mcp.tool.called", MessageIntent.MCP, SenderKind.WORKER, [MCP, UI], { stateDomain: "mcp" }),
  define("runtime.mcp.tool.result", MessageIntent.MCP, SenderKind.MCP, [WORKER, ARTIFACT, UI], { requiresCausation: true, stateDomain: "mcp" }),
  define("runtime.mcp.resource.read", MessageIntent.MCP, SenderKind.MCP, [WORKER, ARTIFACT], { stateDomain: "mcp" }),
  define("runtime.mcp.prompt.loaded", MessageIntent.MCP, SenderKind.MCP, [WORKER], { stateDomain: "mcp" }),
  define("runtime.skill.invocation.started", MessageIntent.SKILL, SenderKind.WORKER, [WORKER, UI], { stateDomain: "skill" }),
  define("runtime.skill.invocation.completed", MessageIntent.SKILL, SenderKind.SKILL, [WORKER, MEMORY, UI], { requiresCausation: true, stateDomain: "skill" }),
  define("runtime.skill.invocation.failed", MessageIntent.SKILL, SenderKind.SKILL, [WORKER, RECOVERY, UI], { requiresCausation: true, stateDomain: "skill" }),
  define("runtime.subagent.created", MessageIntent.SUBAGENT, SenderKind.WORKER, [SCHEDULER, UI], { stateDomain: "subagent" }),
  define("runtime.subagent.dispatched", MessageIntent.SUBAGENT, SenderKind.SCHEDULER, [WORKER, UI], { requiresCausation: true, stateDomain: "subagent" }),
  define("runtime.subagent.progress", MessageIntent.SUBAGENT, SenderKind.SUBAGENT, [WORKER, UI], { requiresCausation: true, defaultEffect: EventEffect.NON_EFFECTIVE, stateDomain: "subagent" }),
  define("runtime.subagent.yield", MessageIntent.SUBAGENT, SenderKind.SUBAGENT, [WORKER, ARTIFACT, UI], { requiresCausation: true, stateDomain: "subagent" }),
  define("runtime.subagent.completed", MessageIntent.SUBAGENT, SenderKind.SUBAGENT, [WORKER, SCHEDULER, MEMORY, UI], { requiresCausation: true, stateDomain: "subagent" }),
  define("runtime.subagent.failed", MessageIntent.SUBAGENT, SenderKind.SUBAGENT, [WORKER, RECOVERY, SCHEDULER, UI], { requiresCausation: true, stateDomain: "subagent" }),
  define("runtime.subagent.cancelled", MessageIntent.SUBAGENT, SenderKind.RUNTIME, [WORKER, SCHEDULER, UI], { requiresCausation: true, stateDomain: "subagent" }),
  define("runtime.subagent.late_result", MessageIntent.SUBAGENT, SenderKind.SUBAGENT, [WORKER, AUDITOR], { requiresCausation: true, stateDomain: "subagent" }),
  define("runtime.compact.started", MessageIntent.COMPACT, SenderKind.RUNTIME, [WORKER, UI], { stateDomain: "compact" }),
  define("runtime.compact.completed", MessageIntent.COMPACT, SenderKind.RUNTIME, [WORKER, MEMORY, UI], { requiresCausation: true, stateDomain: "compact" }),
  define("runtime.compact.restore", MessageIntent.COMPACT, SenderKind.RUNTIME, [WORKER, AUDITOR], { stateDomain: "compact" }),
  define("runtime.api.stream.retry", MessageIntent.STATUS, SenderKind.API, [WORKER, UI], { requiresCausation: true, defaultEffect: EventEffect.NON_EFFECTIVE, stateDomain: "transport" }),
  define("runtime.api.stream.disconnected", MessageIntent.RECOVERY, SenderKind.API, [RECOVERY, UI], { systemCriticalBroadcast: true, stateDomain: "transport" }),
  define("runtime.backend.dispatch.requested", MessageIntent.DISPATCH, SenderKind.SCHEDULER, [WORKER], { stateDomain: "dispatch" }),
  define("runtime.backend.dispatch.accepted", MessageIntent.DISPATCH, SenderKind.WORKER, [SCHEDULER, UI], { requiresCausation: true, stateDomain: "dispatch" }),
  define("runtime.backend.dispatch.failed", MessageIntent.RECOVERY, SenderKind.WORKER, [RECOVERY, SCHEDULER, UI], { requiresCausation: true, systemCriticalBroadcast: true, stateDomain: "dispatch" }),
  define("runtime.backend.failover", MessageIntent.RECOVERY, SenderKind.RECOVERY, [SCHEDULER, WORKER, UI], { requiresCausation: true, systemCriticalBroadcast: true, stateDomain: "dispatch" }),
  define("runtime.artifact.committed", MessageIntent.ARTIFACT, SenderKind.ARTIFACT_STORE, [WORKER, API, UI, MEMORY], { requiredInlineKeys: ["artifact_count"], stateDomain: "artifact" }),
  define("runtime.artifact.quarantined", MessageIntent.RECOVERY, SenderKind.ARTIFACT_STORE, [RECOVERY, AUDITOR], { systemCriticalBroadcast: true, stateDomain: "artifact" }),
  define("runtime.control.requested", MessageIntent.CONTROL, SenderKind.API, [CONTROL], { requiredInlineKeys: ["control_id", "control_kind"], stateDomain: "control" }),
  define("runtime.control.accepted", MessageIntent.CONTROL, SenderKind.CONTROL, [WORKER, UI], { requiresCausation: true, stateDomain: "control" }),
  define("runtime.control.rejected", MessageIntent.CONTROL, SenderKind.CONTROL, [API, UI, AUDITOR], { requiresCausation: true, stateDomain: "control" }),
  define("runtime.control.completed", MessageIntent.CONTROL, SenderKind.RUNTIME, [API, UI], { requiresCausation: true, stateDomain: "control" }),
  define("runtime.recovery.requested", MessageIntent.RECOVERY, SenderKind.RUNTIME, [RECOVERY], { stateDomain: "recovery" }),
  define("runtime.recovery.planned", MessageIntent.RECOVERY, SenderKind.RECOVERY, [SCHEDULER, WORKER, UI], { requiresCausation: true, stateDomain: "recovery" }),
  define("runtime.recovery.completed", MessageIntent.RECOVERY, SenderKind.RECOVERY, [API, UI, MEMORY], { requiresCausation: true, stateDomain: "recovery" }),
  define("runtime.audit.finding", MessageIntent.AUDIT, SenderKind.SYSTEM, [AUDITOR, API], { defaultEffect: EventEffect.NON_EFFECTIVE, stateDomain: "audit" }),
  define("runtime.heartbeat", MessageIntent.STATUS, SenderKind.WORKER, [SCHEDULER], { durability: EventDurability.LIVE_ONLY, defaultEffect: EventEffect.NON_EFFECTIVE, stateDomain: "liveness" }),
];

const catalog = new Map(definitions.map((definition) => [definition.type, definition]));

export function eventDefinition(type: string): EventKindDefinition {
  const definition = catalog.get(type);
  if (!definition) throw new EnvelopeValidationError("unknown runtime event type", { event_type: type });
  return definition;
}

export function maybeEventDefinition(type: string): EventKindDefinition | undefined {
  return catalog.get(type);
}

export function eventDefinitions(): readonly EventKindDefinition[] {
  return definitions;
}

export function durableEventTypes(): readonly string[] {
  return definitions.filter((item) => item.durability === EventDurability.DURABLE).map((item) => item.type);
}

export function liveOnlyEventTypes(): readonly string[] {
  return definitions.filter((item) => item.durability === EventDurability.LIVE_ONLY).map((item) => item.type);
}

export function systemCriticalBroadcastTypes(): ReadonlySet<string> {
  return new Set(definitions.filter((item) => item.systemCriticalBroadcast).map((item) => item.type));
}

export function validateEventKind(
  type: string,
  inline: Readonly<Record<string, unknown>>,
  durability: EventDurabilityValue,
  effect: EventEffectValue,
  causationId?: string,
): EventKindDefinition {
  const definition = eventDefinition(type);
  if (durability !== definition.durability) {
    throw new EnvelopeValidationError("event durability does not match event catalog", {
      event_type: type,
      expected: definition.durability,
      actual: durability,
    });
  }
  if (definition.isStreamDelta && effect !== EventEffect.NON_EFFECTIVE) {
    throw new EnvelopeValidationError("stream delta must be non-effective", { event_type: type, effect });
  }
  if (definition.requiresCausation && !causationId) {
    throw new EnvelopeValidationError("event requires causation id", { event_type: type });
  }
  const allowed = new Set([...definition.requiredInlineKeys, ...definition.optionalInlineKeys]);
  for (const key of definition.requiredInlineKeys) {
    if (!(key in inline)) throw new EnvelopeValidationError("required inline field missing", { event_type: type, field: key });
  }
  for (const key of Object.keys(inline)) {
    if (key.startsWith("_") || allowed.size === 0 || allowed.has(key)) continue;
    if (["legacy_event_type", "legacy_payload_digest", "normalization_warning", "source_event_id"].includes(key)) continue;
  }
  return definition;
}

export function defaultRecipientKinds(type: string): readonly RecipientKindValue[] {
  return eventDefinition(type).recipientKinds;
}

export function catalogSummary(): Record<string, unknown> {
  return {
    schema: "zyra.runtime-event-catalog/v1",
    event_type_count: definitions.length,
    durable_count: durableEventTypes().length,
    live_only_count: liveOnlyEventTypes().length,
    critical_broadcast_count: systemCriticalBroadcastTypes().size,
    state_domains: [...new Set(definitions.map((item) => item.stateDomain))].sort(),
  };
}
