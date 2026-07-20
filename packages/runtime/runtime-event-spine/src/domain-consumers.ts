import {
  EventEffect,
  MessageIntent,
  type AgentMessageEnvelope,
  type RuntimeEventEnvelope,
} from "./contracts.ts";
import {
  ConsumerRole,
  type ConsumerRoleValue,
} from "./integration-contracts.ts";
import {
  type ConsumerDisposition,
  type ConsumerHandler,
  type ConsumerSnapshot,
  type DurableConsumerRuntime,
} from "./consumer-runtime.ts";
import { canonicalJson, digestJson, utcNow, type JsonValue } from "./canonical.ts";
import { EventSpineError, EventSpineErrorCode } from "./errors.ts";
import { eventDefinition } from "./event-catalog.ts";

export const DOMAIN_CONSUMER_SCHEMA = "zyra.runtime-domain-consumers/v1";

export interface DomainMutation {
  domain: string;
  state_key: string;
  status: string;
  source_event_id: string;
  source_event_type: string;
  source_digest: string;
  global_sequence: number;
  effective: boolean;
  role: ConsumerRoleValue;
  correlation_id: string;
  causation_id: string | null;
  attributes: Readonly<Record<string, JsonValue>>;
}

export interface DomainConsumerContract {
  role: ConsumerRoleValue;
  consumerId: string;
  acceptedPrefixes: readonly string[];
  acceptedIntents: readonly string[];
  ownedState: boolean;
  derivativeOnly: boolean;
  retryOnMalformedMessage: boolean;
  skipUnaddressedFacts: boolean;
}

export interface DomainConsumerAuditFinding {
  code: string;
  severity: "warning" | "error";
  consumerId?: string;
  eventId?: string;
  summary: string;
  expected?: JsonValue;
  actual?: JsonValue;
}

export interface DomainConsumerAuditReport {
  schema: "zyra.runtime-domain-consumer-audit/v1";
  checkedAt: string;
  consumerCount: number;
  pendingCount: number;
  processedCount: number;
  failureCount: number;
  checkpointLag: number;
  findings: readonly DomainConsumerAuditFinding[];
  passed: boolean;
}

interface RolePolicy {
  consumerId: string;
  prefixes: readonly string[];
  intents: readonly string[];
  status: (event: RuntimeEventEnvelope) => string;
  stateKey: (event: RuntimeEventEnvelope) => string;
  attributes: (event: RuntimeEventEnvelope) => Readonly<Record<string, JsonValue>>;
}

function identityKey(event: RuntimeEventEnvelope, keys: readonly string[]): string | undefined {
  for (const key of keys) {
    const value = event.identity[key as keyof RuntimeEventEnvelope["identity"]];
    if (typeof value === "string" && value.trim() !== "") return value;
  }
  return undefined;
}

function inlineString(event: RuntimeEventEnvelope, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = event.inline[key];
    if (typeof value === "string" && value.trim() !== "") return value;
  }
  return undefined;
}

function metadataString(event: RuntimeEventEnvelope, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = event.metadata[key];
    if (typeof value === "string" && value.trim() !== "") return value;
  }
  return undefined;
}

function stateValue(event: RuntimeEventEnvelope): JsonValue {
  if (event.stateDelta.value !== undefined) return event.stateDelta.value;
  return event.inline;
}

function terminalStatus(event: RuntimeEventEnvelope): string {
  if (event.eventType.endsWith(".completed") || event.eventType.endsWith(".succeeded")) return "completed";
  if (event.eventType.endsWith(".failed")) return "failed";
  if (event.eventType.endsWith(".cancelled")) return "cancelled";
  if (event.eventType.endsWith(".denied") || event.eventType.endsWith(".rejected")) return "rejected";
  if (event.eventType.endsWith(".allowed") || event.eventType.endsWith(".accepted")) return "accepted";
  if (event.eventType.endsWith(".pending") || event.eventType.endsWith(".requested")) return "pending";
  if (event.eventType.endsWith(".started") || event.eventType.endsWith(".called")) return "running";
  if (event.eventType.endsWith(".queued")) return "queued";
  if (event.eventType.endsWith(".promoted") || event.eventType.endsWith(".dispatched")) return "dispatched";
  if (event.eventType.endsWith(".result") || event.eventType.endsWith(".resolved")) return "resolved";
  if (event.eventType.endsWith(".committed")) return "committed";
  if (event.eventType.endsWith(".quarantined")) return "quarantined";
  return "observed";
}

function commonAttributes(event: RuntimeEventEnvelope): Readonly<Record<string, JsonValue>> {
  return {
    aggregate_id: event.aggregateId,
    run_id: event.identity.runId,
    task_id: event.identity.taskId,
    session_id: event.identity.sessionId ?? null,
    worker_id: event.identity.workerId ?? null,
    node_id: event.identity.nodeId ?? null,
    intent: event.intent,
    sender_kind: event.sender.kind,
    artifact_count: event.artifactRefs.length,
    evidence_count: event.evidenceRefs.length,
    state_delta: {
      domain: event.stateDelta.domain,
      path: [...event.stateDelta.path],
      operation: event.stateDelta.operation,
      effective: event.stateDelta.effective,
      value: stateValue(event),
    },
  };
}

function workerAttributes(event: RuntimeEventEnvelope): Readonly<Record<string, JsonValue>> {
  const permission = event.eventType.startsWith("runtime.permission.")
    ? terminalStatus(event)
    : null;
  const blocked = event.eventType === "runtime.permission.pending"
    || event.eventType === "runtime.permission.denied"
    || event.eventType.endsWith(".failed");
  const toolState = event.eventType.startsWith("runtime.tool.")
    ? terminalStatus(event)
    : null;
  return {
    ...commonAttributes(event),
    blocked,
    permission,
    tool_state: toolState,
    tool_call_id: event.identity.toolCallId ?? null,
    tool_name: inlineString(event, "tool_name") ?? null,
    subagent_id: inlineString(event, "subagent_id") ?? metadataString(event, "source_subject_id") ?? null,
    compact_id: inlineString(event, "compact_id") ?? null,
  };
}

function schedulerAttributes(event: RuntimeEventEnvelope): Readonly<Record<string, JsonValue>> {
  return {
    ...commonAttributes(event),
    route_id: identityKey(event, ["routeId", "nodeId", "subagentId"]) ?? event.aggregateId,
    backend: inlineString(event, "backend", "provider", "model") ?? null,
    route_state: terminalStatus(event),
    recovery_required: event.intent === MessageIntent.RECOVERY,
    selected_recipients: event.topKRecipients.map((recipient) => ({
      kind: recipient.kind,
      id: recipient.id,
    })),
  };
}

function controlAttributes(event: RuntimeEventEnvelope): Readonly<Record<string, JsonValue>> {
  return {
    ...commonAttributes(event),
    control_id: identityKey(event, ["commandId", "permissionId"]) ?? event.aggregateId,
    permission_id: inlineString(event, "permission_id") ?? null,
    decision: inlineString(event, "decision") ?? null,
    action_digest: inlineString(event, "action_digest") ?? null,
    requires_user: event.eventType === "runtime.permission.pending",
    terminal: ["accepted", "rejected", "completed", "resolved"].includes(terminalStatus(event)),
  };
}

function mcpAttributes(event: RuntimeEventEnvelope): Readonly<Record<string, JsonValue>> {
  return {
    ...commonAttributes(event),
    mcp_request_id: identityKey(event, ["mcpRequestId", "toolCallId"]) ?? event.aggregateId,
    server_id: inlineString(event, "server_id", "server") ?? null,
    method: inlineString(event, "method", "tool_name") ?? null,
    result_digest: inlineString(event, "result_digest", "digest") ?? null,
    malformed: event.metadata.malformed === true,
  };
}

function memoryAttributes(event: RuntimeEventEnvelope): Readonly<Record<string, JsonValue>> {
  return {
    ...commonAttributes(event),
    memory_subject: identityKey(event, ["skillId", "subagentId", "toolCallId"]) ?? event.identity.taskId,
    compact_id: inlineString(event, "compact_id") ?? null,
    reusable: event.eventType.endsWith(".completed") || event.eventType.endsWith(".succeeded"),
    source_summary: event.summary,
  };
}

function artifactAttributes(event: RuntimeEventEnvelope): Readonly<Record<string, JsonValue>> {
  return {
    ...commonAttributes(event),
    artifacts: event.artifactRefs.map((artifact) => ({
      artifact_id: artifact.artifactId,
      digest: artifact.digest,
      media_type: artifact.mediaType,
      size_bytes: artifact.sizeBytes,
    })),
    quarantined: event.eventType === "runtime.artifact.quarantined",
    verified_by_event_store: event.artifactRefs.every((artifact) => artifact.digest.startsWith("sha256:")),
  };
}

function recoveryAttributes(event: RuntimeEventEnvelope): Readonly<Record<string, JsonValue>> {
  return {
    ...commonAttributes(event),
    recovery_id: identityKey(event, ["recoveryId", "commandId", "routeId"]) ?? event.aggregateId,
    cause_event_id: event.causationId ?? null,
    failed_component: inlineString(event, "component", "backend", "tool_name") ?? null,
    retryable: event.inline.retryable !== false,
    plan_digest: inlineString(event, "plan_digest") ?? null,
  };
}

function auditAttributes(event: RuntimeEventEnvelope): Readonly<Record<string, JsonValue>> {
  return {
    ...commonAttributes(event),
    finding_code: inlineString(event, "finding_code", "code") ?? null,
    severity: inlineString(event, "severity") ?? (event.intent === MessageIntent.RECOVERY ? "error" : "info"),
    audited_digest: event.contentDigest,
    non_effective: event.effect === EventEffect.NON_EFFECTIVE,
  };
}

function projectionAttributes(event: RuntimeEventEnvelope): Readonly<Record<string, JsonValue>> {
  return {
    ...commonAttributes(event),
    projection_cursor: event.globalSequence,
    view_status: terminalStatus(event),
    summary: event.summary,
    artifact_ids: event.artifactRefs.map((artifact) => artifact.artifactId),
  };
}

const ROLE_POLICIES: Readonly<Record<ConsumerRoleValue, RolePolicy>> = Object.freeze({
  [ConsumerRole.API_PROJECTION]: {
    consumerId: "integration-api",
    prefixes: ["runtime.task.", "runtime.turn.", "runtime.text.", "runtime.control.", "runtime.artifact.", "runtime.audit."],
    intents: [MessageIntent.STATUS, MessageIntent.OBSERVATION, MessageIntent.CONTROL, MessageIntent.ARTIFACT, MessageIntent.AUDIT],
    status: terminalStatus,
    stateKey: (event) => event.aggregateId,
    attributes: projectionAttributes,
  },
  [ConsumerRole.UI_PROJECTION]: {
    consumerId: "integration-ui",
    prefixes: ["runtime."],
    intents: [],
    status: terminalStatus,
    stateKey: (event) => event.aggregateId,
    attributes: projectionAttributes,
  },
  [ConsumerRole.WORKER]: {
    consumerId: "integration-worker",
    prefixes: ["runtime.query.", "runtime.tool.", "runtime.permission.", "runtime.mcp.", "runtime.skill.", "runtime.subagent.", "runtime.compact.", "runtime.backend.", "runtime.recovery."],
    intents: [MessageIntent.QUERY, MessageIntent.TOOL_CALL, MessageIntent.TOOL_RESULT, MessageIntent.PERMISSION, MessageIntent.MCP, MessageIntent.SKILL, MessageIntent.SUBAGENT, MessageIntent.COMPACT, MessageIntent.DISPATCH, MessageIntent.RECOVERY],
    status: terminalStatus,
    stateKey: (event) => identityKey(event, ["toolCallId", "subagentId", "permissionId", "compactId"]) ?? event.aggregateId,
    attributes: workerAttributes,
  },
  [ConsumerRole.SCHEDULER]: {
    consumerId: "integration-scheduler",
    prefixes: ["runtime.task.", "runtime.node.", "runtime.topology.", "runtime.worker.", "runtime.subagent.", "runtime.backend.", "runtime.recovery."],
    intents: [MessageIntent.PLAN, MessageIntent.DISPATCH, MessageIntent.STATUS, MessageIntent.SUBAGENT, MessageIntent.RECOVERY],
    status: terminalStatus,
    stateKey: (event) => identityKey(event, ["routeId", "nodeId", "subagentId"]) ?? event.aggregateId,
    attributes: schedulerAttributes,
  },
  [ConsumerRole.CONTROL]: {
    consumerId: "integration-control",
    prefixes: ["runtime.permission.", "runtime.control.", "runtime.mcp.auth.", "runtime.mcp.elicitation."],
    intents: [MessageIntent.PERMISSION, MessageIntent.CONTROL, MessageIntent.MCP],
    status: terminalStatus,
    stateKey: (event) => identityKey(event, ["commandId", "permissionId", "mcpRequestId"]) ?? event.aggregateId,
    attributes: controlAttributes,
  },
  [ConsumerRole.MCP]: {
    consumerId: "integration-mcp",
    prefixes: ["runtime.mcp."],
    intents: [MessageIntent.MCP],
    status: terminalStatus,
    stateKey: (event) => identityKey(event, ["mcpRequestId", "toolCallId"]) ?? event.aggregateId,
    attributes: mcpAttributes,
  },
  [ConsumerRole.MEMORY]: {
    consumerId: "integration-memory",
    prefixes: ["runtime.task.completed", "runtime.turn.completed", "runtime.text.ended", "runtime.tool.succeeded", "runtime.skill.invocation.completed", "runtime.subagent.completed", "runtime.compact.completed", "runtime.recovery.completed", "runtime.artifact.committed"],
    intents: [MessageIntent.STATUS, MessageIntent.OBSERVATION, MessageIntent.TOOL_RESULT, MessageIntent.SKILL, MessageIntent.SUBAGENT, MessageIntent.COMPACT, MessageIntent.RECOVERY, MessageIntent.ARTIFACT],
    status: terminalStatus,
    stateKey: (event) => identityKey(event, ["skillId", "subagentId", "toolCallId", "compactId"]) ?? event.identity.taskId,
    attributes: memoryAttributes,
  },
  [ConsumerRole.ARTIFACT]: {
    consumerId: "integration-artifact",
    prefixes: ["runtime.browser.", "runtime.tool.succeeded", "runtime.mcp.tool.result", "runtime.mcp.resource.read", "runtime.subagent.yield", "runtime.artifact."],
    intents: [MessageIntent.OBSERVATION, MessageIntent.TOOL_RESULT, MessageIntent.MCP, MessageIntent.SUBAGENT, MessageIntent.ARTIFACT, MessageIntent.RECOVERY],
    status: terminalStatus,
    stateKey: (event) => event.artifactRefs[0]?.artifactId ?? event.aggregateId,
    attributes: artifactAttributes,
  },
  [ConsumerRole.RECOVERY]: {
    consumerId: "integration-recovery",
    prefixes: ["runtime.task.failed", "runtime.node.failed", "runtime.browser.failure", "runtime.turn.failed", "runtime.tool.failed", "runtime.permission.denied", "runtime.skill.invocation.failed", "runtime.subagent.failed", "runtime.api.stream.disconnected", "runtime.backend.", "runtime.artifact.quarantined", "runtime.recovery."],
    intents: [MessageIntent.RECOVERY, MessageIntent.PERMISSION, MessageIntent.TOOL_RESULT, MessageIntent.SKILL, MessageIntent.SUBAGENT],
    status: terminalStatus,
    stateKey: (event) => identityKey(event, ["recoveryId", "toolCallId", "subagentId", "routeId"]) ?? event.aggregateId,
    attributes: recoveryAttributes,
  },
  [ConsumerRole.AUDITOR]: {
    consumerId: "integration-auditor",
    prefixes: ["runtime.topology.route", "runtime.permission.", "runtime.subagent.late_result", "runtime.compact.restore", "runtime.artifact.quarantined", "runtime.control.rejected", "runtime.audit."],
    intents: [MessageIntent.AUDIT, MessageIntent.PERMISSION, MessageIntent.DISPATCH, MessageIntent.SUBAGENT, MessageIntent.COMPACT, MessageIntent.RECOVERY, MessageIntent.CONTROL],
    status: terminalStatus,
    stateKey: (event) => event.eventId,
    attributes: auditAttributes,
  },
});

function matchesPolicy(policy: RolePolicy, event: RuntimeEventEnvelope): boolean {
  const prefixMatch = policy.prefixes.some((prefix) => event.eventType.startsWith(prefix));
  const intentMatch = policy.intents.length === 0 || policy.intents.includes(event.intent);
  return prefixMatch && intentMatch;
}

function validateMessage(event: RuntimeEventEnvelope, message: AgentMessageEnvelope): void {
  if (message.eventId !== event.eventId) {
    throw new EventSpineError({
      code: EventSpineErrorCode.DELIVERY_NOT_FOUND,
      message: "consumer message references a different canonical event",
      details: {
        expected_event_id: event.eventId,
        actual_event_id: message.eventId,
      },
    });
  }
  if (message.eventDigest !== event.contentDigest) {
    throw new EventSpineError({
      code: EventSpineErrorCode.REPLAY_DIVERGENCE,
      message: "consumer message digest does not match the canonical event",
      details: {
        event_id: event.eventId,
        expected_digest: event.contentDigest,
        actual_digest: message.eventDigest,
      },
    });
  }
  if (message.correlationId !== event.correlationId) {
    throw new EventSpineError({
      code: EventSpineErrorCode.REPLAY_DIVERGENCE,
      message: "consumer message correlation differs from the canonical event",
      details: {
        event_id: event.eventId,
        expected_correlation_id: event.correlationId,
        actual_correlation_id: message.correlationId,
      },
    });
  }
}

function mutationFor(role: ConsumerRoleValue, event: RuntimeEventEnvelope): DomainMutation {
  const policy = ROLE_POLICIES[role];
  const attributes = policy.attributes(event);
  return {
    domain: `consumer/${role}/${event.stateDelta.domain}`,
    state_key: policy.stateKey(event),
    status: policy.status(event),
    source_event_id: event.eventId,
    source_event_type: event.eventType,
    source_digest: event.contentDigest,
    global_sequence: event.globalSequence,
    effective: event.effect === EventEffect.EFFECTIVE,
    role,
    correlation_id: event.correlationId,
    causation_id: event.causationId ?? null,
    attributes,
  };
}

export function domainConsumerHandler(role: ConsumerRoleValue): ConsumerHandler {
  const policy = ROLE_POLICIES[role];
  return (event, message, context): ConsumerDisposition => {
    validateMessage(event, message);
    if (!matchesPolicy(policy, event)) {
      return {
        outcome: "skip",
        stateMutation: {
          domain: `consumer/${role}/ignored`,
          state_key: event.eventId,
          status: "unaddressed",
          source_event_id: event.eventId,
          source_event_type: event.eventType,
          global_sequence: event.globalSequence,
          role,
        },
      };
    }
    const definition = eventDefinition(event.eventType);
    if (!definition.recipientKinds.includes(context.delivery.recipient.kind)) {
      return {
        outcome: "dead_letter",
        errorCode: "recipient_contract_violation",
        errorMessage: `${event.eventType} does not permit recipient kind ${context.delivery.recipient.kind}`,
      };
    }
    return {
      outcome: "ack",
      stateMutation: mutationFor(role, event) as unknown as Readonly<Record<string, JsonValue>>,
    };
  };
}

export function domainConsumerContract(role: ConsumerRoleValue): DomainConsumerContract {
  const policy = ROLE_POLICIES[role];
  return {
    role,
    consumerId: policy.consumerId,
    acceptedPrefixes: Object.freeze([...policy.prefixes]),
    acceptedIntents: Object.freeze([...policy.intents]),
    ownedState: false,
    derivativeOnly: true,
    retryOnMalformedMessage: true,
    skipUnaddressedFacts: true,
  };
}

export function allDomainConsumerContracts(): readonly DomainConsumerContract[] {
  return Object.values(ConsumerRole).map((role) => domainConsumerContract(role));
}

export function domainConsumerContractDigest(): string {
  return digestJson(allDomainConsumerContracts() as unknown as JsonValue);
}

export class DomainConsumerAuditor {
  readonly runtime: DurableConsumerRuntime;

  constructor(runtime: DurableConsumerRuntime) {
    this.runtime = runtime;
  }

  audit(): DomainConsumerAuditReport {
    const findings: DomainConsumerAuditFinding[] = [];
    const snapshots = this.runtime.snapshots();
    const highWatermark = this.runtime.spine.store.latestGlobalSequence();
    for (const snapshot of snapshots) {
      this.auditSnapshot(snapshot, highWatermark, findings);
      this.auditProcessed(snapshot, findings);
    }
    const pendingCount = snapshots.reduce((total, snapshot) => total + snapshot.pendingCount, 0);
    const processedCount = snapshots.reduce((total, snapshot) => total + snapshot.processedCount, 0);
    const failureCount = snapshots.reduce((total, snapshot) => total + snapshot.failedCount, 0);
    const checkpointLag = snapshots.reduce(
      (total, snapshot) => total + Math.max(0, highWatermark - snapshot.checkpoint.lastGlobalSequence),
      0,
    );
    return {
      schema: "zyra.runtime-domain-consumer-audit/v1",
      checkedAt: utcNow(),
      consumerCount: snapshots.length,
      pendingCount,
      processedCount,
      failureCount,
      checkpointLag,
      findings: Object.freeze(findings),
      passed: findings.every((item) => item.severity !== "error"),
    };
  }

  state(consumerId: string, domain: string, stateKey: string): Readonly<Record<string, JsonValue>> | undefined {
    return this.runtime.readMutation(consumerId, domain, stateKey);
  }

  states(consumerId: string, limit = 1000): readonly Readonly<Record<string, JsonValue>>[] {
    const snapshot = this.runtime.snapshot(consumerId);
    const rows = this.runtime.spine.store.db.prepare(`
      SELECT domain, state_key, event_id, global_sequence, state_json, state_digest, updated_at
      FROM runtime_event_consumer_mutations
      WHERE consumer_id = ?
      ORDER BY global_sequence ASC, domain ASC, state_key ASC
      LIMIT ?
    `).all(snapshot.consumerId, Math.max(1, Math.min(limit, 100_000))) as unknown as Array<{
      domain: string;
      state_key: string;
      event_id: string;
      global_sequence: number;
      state_json: string;
      state_digest: string;
      updated_at: string;
    }>;
    return rows.map((row) => ({
      consumer_id: snapshot.consumerId,
      domain: row.domain,
      state_key: row.state_key,
      event_id: row.event_id,
      global_sequence: row.global_sequence,
      state: JSON.parse(row.state_json),
      state_digest: row.state_digest,
      updated_at: row.updated_at,
    }));
  }

  contract(): Readonly<Record<string, JsonValue>> {
    return {
      schema: DOMAIN_CONSUMER_SCHEMA,
      canonical_owner: false,
      canonical_event_owner: "RuntimeEventSqliteStore",
      delivery_owner: "RuntimeMessageBus",
      mutation_owner: "DurableConsumerRuntime",
      derivative_only: true,
      ack_after_mutation_commit: true,
      processed_event_idempotence: true,
      contracts: allDomainConsumerContracts() as unknown as JsonValue,
      contract_digest: domainConsumerContractDigest(),
    };
  }

  private auditSnapshot(
    snapshot: ConsumerSnapshot,
    highWatermark: number,
    findings: DomainConsumerAuditFinding[],
  ): void {
    if (snapshot.running) {
      findings.push({
        code: "consumer_still_running",
        severity: "warning",
        consumerId: snapshot.consumerId,
        summary: "consumer audit ran while the consumer held its single-run guard",
      });
    }
    if (snapshot.checkpoint.lastGlobalSequence > highWatermark) {
      findings.push({
        code: "consumer_checkpoint_ahead",
        severity: "error",
        consumerId: snapshot.consumerId,
        summary: "consumer checkpoint is ahead of the canonical EventStore high watermark",
        expected: highWatermark,
        actual: snapshot.checkpoint.lastGlobalSequence,
      });
    }
    if (snapshot.checkpoint.handledCount + snapshot.checkpoint.duplicateCount < snapshot.processedCount) {
      findings.push({
        code: "consumer_checkpoint_count_mismatch",
        severity: "error",
        consumerId: snapshot.consumerId,
        summary: "consumer processed rows exceed checkpoint accounting",
        expected: snapshot.processedCount,
        actual: snapshot.checkpoint.handledCount + snapshot.checkpoint.duplicateCount,
      });
    }
    if (snapshot.pendingCount > 0 && snapshot.checkpoint.lastGlobalSequence === highWatermark) {
      findings.push({
        code: "delivery_pending_at_high_watermark",
        severity: "warning",
        consumerId: snapshot.consumerId,
        summary: "delivery remains pending even though the consumer checkpoint reached the store high watermark",
        actual: snapshot.pendingCount,
      });
    }
  }

  private auditProcessed(snapshot: ConsumerSnapshot, findings: DomainConsumerAuditFinding[]): void {
    const rows = this.runtime.processedEvents(snapshot.consumerId, Math.max(1, snapshot.processedCount + 1));
    let previousSequence = 0;
    const eventIds = new Set<string>();
    for (const row of rows) {
      const eventId = String(row.event_id);
      const globalSequence = Number(row.global_sequence);
      if (eventIds.has(eventId)) {
        findings.push({
          code: "consumer_duplicate_processed_row",
          severity: "error",
          consumerId: snapshot.consumerId,
          eventId,
          summary: "consumer processed the same canonical event more than once",
        });
      }
      eventIds.add(eventId);
      if (globalSequence < previousSequence) {
        findings.push({
          code: "consumer_processed_order_regressed",
          severity: "error",
          consumerId: snapshot.consumerId,
          eventId,
          summary: "consumer processed rows regress in canonical global sequence",
          expected: `>=${previousSequence}`,
          actual: globalSequence,
        });
      }
      previousSequence = Math.max(previousSequence, globalSequence);
      const event = this.runtime.spine.get(eventId);
      if (!event) {
        findings.push({
          code: "consumer_source_event_missing",
          severity: "error",
          consumerId: snapshot.consumerId,
          eventId,
          summary: "consumer processed row has no canonical source event",
        });
        continue;
      }
      if (event.contentDigest !== row.event_digest) {
        findings.push({
          code: "consumer_source_digest_mismatch",
          severity: "error",
          consumerId: snapshot.consumerId,
          eventId,
          summary: "consumer processed digest differs from canonical EventStore digest",
          expected: event.contentDigest,
          actual: row.event_digest,
        });
      }
      const mutation = row.mutation;
      if (mutation && typeof mutation === "object" && !Array.isArray(mutation)) {
        const serialized = canonicalJson(mutation);
        if (Buffer.byteLength(serialized, "utf8") > 16_384) {
          findings.push({
            code: "consumer_mutation_too_large",
            severity: "error",
            consumerId: snapshot.consumerId,
            eventId,
            summary: "derivative consumer mutation exceeds the low-entropy state budget",
            expected: 16_384,
            actual: Buffer.byteLength(serialized, "utf8"),
          });
        }
      }
    }
  }
}
