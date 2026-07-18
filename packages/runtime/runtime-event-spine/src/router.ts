import {
  MessageIntent,
  RecipientKind,
  type RecipientKindValue,
  type RecipientRef,
  type RouteCandidate,
  type RouteDecision,
  type RuntimeEventEnvelope,
  type SubscriptionSpec,
} from "./contracts.ts";
import { defaultRecipientKinds, eventDefinition, systemCriticalBroadcastTypes } from "./event-catalog.ts";
import { stableId, utcNow } from "./canonical.ts";
import { EventSpineErrorCode, RoutingError } from "./errors.ts";

export interface RouterPolicy {
  policyId: string;
  maximumRecipients: number;
  allowExplicitTargetWithoutSubscription: boolean;
  requireCapabilityIntersection: boolean;
  systemCriticalBroadcastTypes: ReadonlySet<string>;
  intentWeights: Readonly<Record<string, number>>;
  kindWeights: Readonly<Record<string, number>>;
  capabilityWeight: number;
  explicitTargetWeight: number;
  taskAffinityWeight: number;
  aggregateAffinityWeight: number;
  subscriptionPriorityWeight: number;
}

export interface RouterContext {
  subscriptions: readonly SubscriptionSpec[];
  pendingBySubscription: ReadonlyMap<string, number>;
  dependencyRecipients?: readonly string[];
  topK?: number;
  broadcast?: boolean;
  fanoutReason?: string;
}

export interface RecipientRegistration {
  recipient: RecipientRef;
  subscriptionIds: readonly string[];
  capabilities: readonly string[];
  taskIds: readonly string[];
  aggregatePrefixes: readonly string[];
  priority: number;
  enabled: boolean;
}

const DEFAULT_INTENT_WEIGHTS: Record<string, number> = {
  [MessageIntent.QUERY]: 8,
  [MessageIntent.PLAN]: 7,
  [MessageIntent.TOOL_CALL]: 10,
  [MessageIntent.TOOL_RESULT]: 10,
  [MessageIntent.OBSERVATION]: 5,
  [MessageIntent.PERMISSION]: 11,
  [MessageIntent.CONTROL]: 11,
  [MessageIntent.MCP]: 9,
  [MessageIntent.SKILL]: 8,
  [MessageIntent.SUBAGENT]: 9,
  [MessageIntent.COMPACT]: 8,
  [MessageIntent.DISPATCH]: 10,
  [MessageIntent.ARTIFACT]: 8,
  [MessageIntent.RECOVERY]: 12,
  [MessageIntent.STATUS]: 4,
  [MessageIntent.AUDIT]: 3,
};

const DEFAULT_KIND_WEIGHTS: Record<string, number> = {
  [RecipientKind.API]: 2,
  [RecipientKind.WORKER]: 8,
  [RecipientKind.SCHEDULER]: 8,
  [RecipientKind.ARTIFACT_STORE]: 7,
  [RecipientKind.RECOVERY]: 9,
  [RecipientKind.CONTROL]: 9,
  [RecipientKind.MEMORY]: 5,
  [RecipientKind.UI_PROJECTOR]: 3,
  [RecipientKind.AUDITOR]: 2,
};

export function defaultRouterPolicy(): RouterPolicy {
  return {
    policyId: "zyra.targeted-router/v1",
    maximumRecipients: 8,
    allowExplicitTargetWithoutSubscription: false,
    requireCapabilityIntersection: true,
    systemCriticalBroadcastTypes: systemCriticalBroadcastTypes(),
    intentWeights: DEFAULT_INTENT_WEIGHTS,
    kindWeights: DEFAULT_KIND_WEIGHTS,
    capabilityWeight: 4,
    explicitTargetWeight: 100,
    taskAffinityWeight: 12,
    aggregateAffinityWeight: 8,
    subscriptionPriorityWeight: 0.01,
  };
}

function recipientKey(recipient: RecipientRef): string {
  return `${recipient.kind}:${recipient.id}`;
}

function matchesPattern(value: string, pattern: string): boolean {
  if (pattern === "*") return true;
  if (pattern.endsWith("*")) return value.startsWith(pattern.slice(0, -1));
  return value === pattern;
}

function matchesSubscription(event: RuntimeEventEnvelope, subscription: SubscriptionSpec): boolean {
  if (!subscription.enabled) return false;
  if (subscription.eventTypes.length > 0 && !subscription.eventTypes.some((pattern) => matchesPattern(event.eventType, pattern))) return false;
  if (subscription.intents.length > 0 && !subscription.intents.includes(event.intent)) return false;
  if (subscription.aggregatePrefixes.length > 0 && !subscription.aggregatePrefixes.some((prefix) => event.aggregateId.startsWith(prefix))) return false;
  if (subscription.taskIds.length > 0 && !subscription.taskIds.includes(event.identity.taskId)) return false;
  return true;
}

function registrationFor(subscriptions: readonly SubscriptionSpec[]): readonly RecipientRegistration[] {
  const grouped = new Map<string, RecipientRegistration>();
  for (const subscription of subscriptions) {
    const key = recipientKey(subscription.recipient);
    const existing = grouped.get(key);
    if (!existing) {
      grouped.set(key, {
        recipient: subscription.recipient,
        subscriptionIds: [subscription.subscriptionId],
        capabilities: [...new Set([...subscription.capabilityRefs, ...subscription.recipient.requiredCapabilities])],
        taskIds: [...subscription.taskIds],
        aggregatePrefixes: [...subscription.aggregatePrefixes],
        priority: subscription.priority,
        enabled: subscription.enabled,
      });
      continue;
    }
    grouped.set(key, {
      recipient: existing.recipient,
      subscriptionIds: [...existing.subscriptionIds, subscription.subscriptionId],
      capabilities: [...new Set([...existing.capabilities, ...subscription.capabilityRefs, ...subscription.recipient.requiredCapabilities])],
      taskIds: [...new Set([...existing.taskIds, ...subscription.taskIds])],
      aggregatePrefixes: [...new Set([...existing.aggregatePrefixes, ...subscription.aggregatePrefixes])],
      priority: Math.min(existing.priority, subscription.priority),
      enabled: existing.enabled || subscription.enabled,
    });
  }
  return [...grouped.values()];
}

export class AgentMessageRouter {
  readonly policy: RouterPolicy;

  constructor(policy: RouterPolicy = defaultRouterPolicy()) {
    this.policy = policy;
  }

  route(event: RuntimeEventEnvelope, context: RouterContext): RouteDecision {
    eventDefinition(event.eventType);
    const subscriptions = context.subscriptions.filter((subscription) => matchesSubscription(event, subscription));
    const registrations = registrationFor(subscriptions);
    const requestedBroadcast = context.broadcast === true;
    if (requestedBroadcast && !this.policy.systemCriticalBroadcastTypes.has(event.eventType)) {
      throw new RoutingError(EventSpineErrorCode.BROADCAST_FORBIDDEN, "event type is not allowed to broadcast", {
        event_id: event.eventId,
        event_type: event.eventType,
      });
    }
    const candidates = registrations.map((registration) => this.score(event, registration, context));
    const eligible = candidates
      .filter((candidate) => candidate.rejectedReasons.length === 0 && candidate.score > 0)
      .sort((left, right) => right.score - left.score || recipientKey(left.recipient).localeCompare(recipientKey(right.recipient)));
    let recipients: RecipientRef[];
    let explicitTarget = false;
    if (requestedBroadcast) {
      recipients = eligible.map((candidate) => candidate.recipient);
    } else if (event.target) {
      explicitTarget = true;
      const key = recipientKey(event.target);
      const target = eligible.find((candidate) => recipientKey(candidate.recipient) === key);
      if (!target && !this.policy.allowExplicitTargetWithoutSubscription) {
        throw new RoutingError(EventSpineErrorCode.ROUTE_NOT_FOUND, "explicit target has no matching subscription", {
          event_id: event.eventId,
          target: key,
        });
      }
      recipients = [target?.recipient ?? event.target];
    } else if (event.topKRecipients.length > 0) {
      const requested = new Set(event.topKRecipients.map(recipientKey));
      recipients = eligible.filter((candidate) => requested.has(recipientKey(candidate.recipient))).map((candidate) => candidate.recipient);
    } else {
      const defaultKinds = new Set(defaultRecipientKinds(event.eventType));
      const topK = Math.max(1, Math.min(context.topK ?? this.defaultTopK(event), this.policy.maximumRecipients));
      recipients = eligible.filter((candidate) => defaultKinds.has(candidate.recipient.kind)).slice(0, topK).map((candidate) => candidate.recipient);
    }
    recipients = [...new Map(recipients.map((recipient) => [recipientKey(recipient), recipient])).values()];
    if (recipients.length === 0) {
      throw new RoutingError(EventSpineErrorCode.ROUTE_NOT_FOUND, "no targeted recipient matched event", {
        event_id: event.eventId,
        event_type: event.eventType,
        intent: event.intent,
        subscription_count: context.subscriptions.length,
        candidate_count: candidates.length,
      });
    }
    if (!requestedBroadcast && recipients.length > this.policy.maximumRecipients) recipients = recipients.slice(0, this.policy.maximumRecipients);
    const availableRecipientCount = Math.max(1, registrations.filter((item) => item.enabled).length);
    return {
      eventId: event.eventId,
      policyId: this.policy.policyId,
      explicitTarget,
      broadcast: requestedBroadcast,
      fanoutReason: requestedBroadcast ? context.fanoutReason ?? "system_critical_allowlist" : undefined,
      recipients,
      candidates,
      routeDensity: recipients.length / availableRecipientCount,
      availableRecipientCount,
      createdAt: utcNow(),
    };
  }

  private score(event: RuntimeEventEnvelope, registration: RecipientRegistration, context: RouterContext): RouteCandidate {
    const reasons: string[] = [];
    const rejected: string[] = [];
    let score = 0;
    const key = recipientKey(registration.recipient);
    if (!registration.enabled) rejected.push("recipient_disabled");
    if (event.target && recipientKey(event.target) === key) {
      score += this.policy.explicitTargetWeight;
      reasons.push("explicit_target");
    }
    const defaultKinds = defaultRecipientKinds(event.eventType);
    if (defaultKinds.includes(registration.recipient.kind)) {
      score += this.policy.kindWeights[registration.recipient.kind] ?? 1;
      reasons.push("event_kind_target");
    }
    score += this.policy.intentWeights[event.intent] ?? 1;
    reasons.push(`intent:${event.intent}`);
    const required = new Set(registration.recipient.requiredCapabilities);
    const provided = new Set(registration.capabilities);
    const senderCapabilities = new Set(event.sender.capabilityRefs);
    const requiredMatches = [...required].filter((capability) => provided.has(capability) || senderCapabilities.has(capability));
    if (required.size > 0 && requiredMatches.length === 0 && this.policy.requireCapabilityIntersection) {
      rejected.push("required_capability_missing");
    } else if (requiredMatches.length > 0) {
      score += requiredMatches.length * this.policy.capabilityWeight;
      reasons.push(`capability_matches:${requiredMatches.length}`);
    }
    if (registration.taskIds.includes(event.identity.taskId)) {
      score += this.policy.taskAffinityWeight;
      reasons.push("task_affinity");
    }
    if (registration.aggregatePrefixes.some((prefix) => event.aggregateId.startsWith(prefix))) {
      score += this.policy.aggregateAffinityWeight;
      reasons.push("aggregate_affinity");
    }
    if (context.dependencyRecipients?.includes(key) || context.dependencyRecipients?.includes(registration.recipient.id)) {
      score += this.policy.taskAffinityWeight;
      reasons.push("task_dependency");
    }
    const pending = registration.subscriptionIds.reduce((total, id) => total + (context.pendingBySubscription.get(id) ?? 0), 0);
    const matchingSubscriptions = context.subscriptions.filter((item) => recipientKey(item.recipient) === key);
    const totalCapacity = matchingSubscriptions.reduce((total, item) => total + item.capacity, 0);
    if (totalCapacity > 0 && pending >= totalCapacity) rejected.push("recipient_backpressure");
    else if (totalCapacity > 0) {
      const availability = 1 - pending / totalCapacity;
      score += availability * 5;
      reasons.push(`capacity:${availability.toFixed(3)}`);
    }
    score += Math.max(0, 1000 - registration.priority) * this.policy.subscriptionPriorityWeight;
    return {
      recipient: registration.recipient,
      score: Number(score.toFixed(6)),
      reasons,
      rejectedReasons: rejected,
    };
  }

  private defaultTopK(event: RuntimeEventEnvelope): number {
    if (event.intent === MessageIntent.RECOVERY) return 3;
    if (event.intent === MessageIntent.ARTIFACT || event.intent === MessageIntent.TOOL_RESULT) return 2;
    return 1;
  }
}

export function builtInSubscriptions(): readonly SubscriptionSpec[] {
  const create = (
    id: string,
    kind: RecipientKindValue,
    intents: SubscriptionSpec["intents"],
    eventTypes: readonly string[],
    capabilities: readonly string[],
    priority: number,
  ): SubscriptionSpec => ({
    subscriptionId: `builtin-${id}`,
    recipient: { kind, id, requiredCapabilities: [] },
    eventTypes,
    intents,
    aggregatePrefixes: [],
    taskIds: [],
    capabilityRefs: capabilities,
    capacity: 4096,
    maxAttempts: 5,
    ackTimeoutMs: 30_000,
    backpressureMode: "reject",
    enabled: true,
    priority,
    metadata: { built_in: true, canonical_owner: false },
  });
  return [
    create("api-projection", RecipientKind.API, [MessageIntent.STATUS, MessageIntent.OBSERVATION, MessageIntent.CONTROL, MessageIntent.ARTIFACT], ["runtime.task.*", "runtime.turn.*", "runtime.agent.*", "runtime.browser.*", "runtime.control.*", "runtime.artifact.*"], ["api.read-model"], 50),
    create("worker-runtime", RecipientKind.WORKER, [MessageIntent.QUERY, MessageIntent.PLAN, MessageIntent.OBSERVATION, MessageIntent.STATUS, MessageIntent.CONTROL, MessageIntent.TOOL_CALL, MessageIntent.TOOL_RESULT, MessageIntent.PERMISSION, MessageIntent.MCP, MessageIntent.SKILL, MessageIntent.SUBAGENT, MessageIntent.COMPACT, MessageIntent.DISPATCH, MessageIntent.RECOVERY], ["runtime.query.*", "runtime.node.*", "runtime.topology.*", "runtime.agent.*", "runtime.tool.*", "runtime.permission.*", "runtime.mcp.*", "runtime.skill.*", "runtime.subagent.*", "runtime.compact.*", "runtime.backend.*", "runtime.recovery.*"], ["worker.execute"], 10),
    create("resource-scheduler", RecipientKind.SCHEDULER, [MessageIntent.PLAN, MessageIntent.DISPATCH, MessageIntent.RECOVERY, MessageIntent.STATUS, MessageIntent.SUBAGENT], ["runtime.task.*", "runtime.node.*", "runtime.topology.*", "runtime.backend.*", "runtime.subagent.*"], ["scheduler.route"], 10),
    create("artifact-store", RecipientKind.ARTIFACT_STORE, [MessageIntent.ARTIFACT, MessageIntent.TOOL_RESULT, MessageIntent.MCP, MessageIntent.SUBAGENT], ["runtime.artifact.*", "runtime.tool.succeeded", "runtime.mcp.tool.result", "runtime.subagent.yield"], ["artifact.commit"], 20),
    create("recovery-planner", RecipientKind.RECOVERY, [MessageIntent.RECOVERY, MessageIntent.TOOL_RESULT, MessageIntent.PERMISSION, MessageIntent.STATUS], ["runtime.*.failed", "runtime.node.failed", "runtime.backend.*", "runtime.permission.denied", "runtime.artifact.quarantined", "runtime.api.stream.disconnected", "runtime.recovery.*"], ["recovery.plan"], 5),
    create("control-runtime", RecipientKind.CONTROL, [MessageIntent.CONTROL, MessageIntent.PERMISSION, MessageIntent.MCP], ["runtime.control.*", "runtime.permission.*", "runtime.mcp.auth.*", "runtime.mcp.elicitation.*"], ["control.decide"], 5),
    create("memory-projector", RecipientKind.MEMORY, [MessageIntent.STATUS, MessageIntent.TOOL_RESULT, MessageIntent.SKILL, MessageIntent.SUBAGENT, MessageIntent.COMPACT, MessageIntent.ARTIFACT], ["runtime.task.completed", "runtime.turn.completed", "runtime.tool.succeeded", "runtime.skill.invocation.completed", "runtime.subagent.completed", "runtime.compact.completed", "runtime.artifact.committed"], ["memory.project"], 80),
    create("ui-projector", RecipientKind.UI_PROJECTOR, [MessageIntent.PLAN, MessageIntent.STATUS, MessageIntent.OBSERVATION, MessageIntent.PERMISSION, MessageIntent.CONTROL, MessageIntent.SUBAGENT, MessageIntent.DISPATCH, MessageIntent.RECOVERY], ["runtime.task.*", "runtime.node.*", "runtime.turn.*", "runtime.text.*", "runtime.reasoning.*", "runtime.tool.*", "runtime.permission.*", "runtime.control.*", "runtime.subagent.*", "runtime.backend.*", "runtime.recovery.*"], ["ui.project"], 100),
    create("runtime-auditor", RecipientKind.AUDITOR, [MessageIntent.AUDIT, MessageIntent.PERMISSION, MessageIntent.DISPATCH, MessageIntent.RECOVERY], ["runtime.audit.*", "runtime.permission.*", "runtime.topology.route", "runtime.artifact.quarantined", "runtime.subagent.late_result"], ["audit.consume"], 200),
  ];
}

export function routeDecisionDigest(decision: RouteDecision): string {
  return stableId("route", decision.policyId, decision.eventId, decision.recipients.map(recipientKey));
}
