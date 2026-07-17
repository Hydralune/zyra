import type { JsonObject } from "../contracts.ts";
import {
  canonicalJson,
  cloneJson,
  deterministicMcpId,
  monotonicNow,
  sha256,
} from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";

export type McpAuditKind =
  | "config_committed"
  | "oauth_started"
  | "oauth_completed"
  | "server_connected"
  | "server_disconnected"
  | "server_failed"
  | "catalog_changed"
  | "request_started"
  | "request_completed"
  | "request_failed"
  | "request_cancelled"
  | "recovery_planned"
  | "recovery_resolved"
  | "resource_subscribed"
  | "resource_updated"
  | "resource_unsubscribed"
  | "elicitation_started"
  | "elicitation_resumed"
  | "sampling_started"
  | "sampling_completed"
  | "task_changed";

export interface McpAuditRecord {
  auditId: string;
  sequence: number;
  kind: McpAuditKind;
  serverId: string;
  sessionId: string;
  taskId: string | null;
  workerRequestId: string | null;
  toolCallId: string | null;
  requestId: string | null;
  connectionId: string | null;
  connectionEpoch: number | null;
  operation: string;
  outcome: "started" | "committed" | "failed" | "cancelled" | "observation";
  stateDigest: string;
  details: JsonObject;
  occurredAt: string;
  previousHash: string;
  auditHash: string;
}

export interface McpAuditSnapshot {
  version: "zyra.mcp-audit-runtime/v1";
  sequence: number;
  headHash: string;
  records: McpAuditRecord[];
  digest: string;
  capturedAt: string;
}

const auditGenesis = "sha256:zyra-mcp-audit-genesis";

export class McpAuditRuntime {
  private readonly records: McpAuditRecord[] = [];
  private readonly now: () => Date;
  private readonly maximumRecords: number;
  private sequence = 0;
  private headHash = auditGenesis;
  private lastTimestamp: string | null = null;

  constructor(options: {
    now?: () => Date;
    maximumRecords?: number;
    snapshot?: McpAuditSnapshot | null;
  } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumRecords = options.maximumRecords ?? 200_000;
    if (options.snapshot) {
      this.restore(options.snapshot);
    }
  }

  append(input: {
    kind: McpAuditKind;
    serverId?: string;
    sessionId: string;
    taskId?: string | null;
    workerRequestId?: string | null;
    toolCallId?: string | null;
    requestId?: string | null;
    connectionId?: string | null;
    connectionEpoch?: number | null;
    operation: string;
    outcome: McpAuditRecord["outcome"];
    stateDigest: string;
    details?: JsonObject;
  }): McpAuditRecord {
    if (!input.sessionId || !input.operation || !input.stateDigest) {
      throw auditError(
        input.serverId ?? "",
        "audit_identity_incomplete",
        "MCP audit record requires session, operation, and state digest",
      );
    }
    if (
      input.connectionEpoch !== null
      && input.connectionEpoch !== undefined
      && (!Number.isSafeInteger(input.connectionEpoch) || input.connectionEpoch < 0)
    ) {
      throw auditError(
        input.serverId ?? "",
        "audit_connection_epoch_invalid",
        "MCP audit connection epoch must be a non-negative safe integer",
      );
    }
    this.sequence += 1;
    const base = {
      auditId: deterministicMcpId("mcp-audit", {
        sequence: this.sequence,
        kind: input.kind,
        server_id: input.serverId ?? "",
        session_id: input.sessionId,
        task_id: input.taskId ?? null,
        tool_call_id: input.toolCallId ?? null,
        request_id: input.requestId ?? null,
        operation: input.operation,
        state_digest: input.stateDigest,
      }, 40),
      sequence: this.sequence,
      kind: input.kind,
      serverId: input.serverId ?? "",
      sessionId: input.sessionId,
      taskId: input.taskId ?? null,
      workerRequestId: input.workerRequestId ?? null,
      toolCallId: input.toolCallId ?? null,
      requestId: input.requestId ?? null,
      connectionId: input.connectionId ?? null,
      connectionEpoch: input.connectionEpoch ?? null,
      operation: input.operation,
      outcome: input.outcome,
      stateDigest: input.stateDigest,
      details: canonicalJson(input.details ?? {}) as JsonObject,
      occurredAt: this.timestamp(),
      previousHash: this.headHash,
    };
    const record: McpAuditRecord = {
      ...base,
      auditHash: sha256({
        previous_hash: this.headHash,
        record: base,
      }),
    };
    this.headHash = record.auditHash;
    this.records.push(record);
    while (this.records.length > this.maximumRecords) {
      this.records.shift();
    }
    return cloneJson(record);
  }

  requestStarted(input: {
    serverId: string;
    sessionId: string;
    taskId: string;
    workerRequestId: string;
    toolCallId: string;
    requestId: string;
    connectionId: string;
    connectionEpoch: number;
    operation: string;
    arguments: JsonObject;
    metadata?: JsonObject;
  }): McpAuditRecord {
    return this.append({
      kind: "request_started",
      serverId: input.serverId,
      sessionId: input.sessionId,
      taskId: input.taskId,
      workerRequestId: input.workerRequestId,
      toolCallId: input.toolCallId,
      requestId: input.requestId,
      connectionId: input.connectionId,
      connectionEpoch: input.connectionEpoch,
      operation: input.operation,
      outcome: "started",
      stateDigest: sha256({
        arguments: input.arguments,
        metadata: input.metadata ?? {},
      }),
      details: {
        arguments_digest: sha256(input.arguments),
        metadata: input.metadata ?? {},
      },
    });
  }

  requestSettled(input: {
    kind?: "request_completed" | "request_failed" | "request_cancelled";
    serverId: string;
    sessionId: string;
    taskId: string;
    workerRequestId: string;
    toolCallId: string;
    requestId: string;
    connectionId: string;
    connectionEpoch: number;
    operation: string;
    output?: JsonObject | null;
    failure?: JsonObject | null;
    metadata?: JsonObject;
  }): McpAuditRecord {
    const kind = input.kind ?? (input.failure ? "request_failed" : "request_completed");
    const outcome = kind === "request_completed"
      ? "committed"
      : kind === "request_cancelled"
        ? "cancelled"
        : "failed";
    const state = {
      output: input.output ?? null,
      failure: input.failure ?? null,
      metadata: input.metadata ?? {},
    };
    return this.append({
      kind,
      serverId: input.serverId,
      sessionId: input.sessionId,
      taskId: input.taskId,
      workerRequestId: input.workerRequestId,
      toolCallId: input.toolCallId,
      requestId: input.requestId,
      connectionId: input.connectionId,
      connectionEpoch: input.connectionEpoch,
      operation: input.operation,
      outcome,
      stateDigest: sha256(state),
      details: canonicalJson(state) as JsonObject,
    });
  }

  query(options: {
    serverId?: string;
    sessionId?: string;
    taskId?: string;
    toolCallId?: string;
    requestId?: string;
    kind?: McpAuditKind;
    outcome?: McpAuditRecord["outcome"];
    afterSequence?: number;
    limit?: number;
  } = {}): McpAuditRecord[] {
    const limit = Math.max(1, Math.min(options.limit ?? 1_000, 10_000));
    return this.records
      .filter((record) => !options.serverId || record.serverId === options.serverId)
      .filter((record) => !options.sessionId || record.sessionId === options.sessionId)
      .filter((record) => !options.taskId || record.taskId === options.taskId)
      .filter((record) => !options.toolCallId || record.toolCallId === options.toolCallId)
      .filter((record) => !options.requestId || record.requestId === options.requestId)
      .filter((record) => !options.kind || record.kind === options.kind)
      .filter((record) => !options.outcome || record.outcome === options.outcome)
      .filter((record) => record.sequence > (options.afterSequence ?? 0))
      .slice(0, limit)
      .map(cloneJson);
  }

  snapshot(): McpAuditSnapshot {
    const records = this.records.map(cloneJson);
    const withoutDigest = {
      version: "zyra.mcp-audit-runtime/v1" as const,
      sequence: this.sequence,
      headHash: this.headHash,
      records,
      capturedAt: this.timestamp(),
    };
    return {
      ...withoutDigest,
      digest: sha256(withoutDigest),
    };
  }

  restore(snapshot: McpAuditSnapshot): void {
    if (snapshot.version !== "zyra.mcp-audit-runtime/v1") {
      throw auditError(
        "",
        "unsupported_audit_snapshot",
        `unsupported MCP audit snapshot ${snapshot.version}`,
      );
    }
    const { digest: expectedDigest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== expectedDigest) {
      throw auditError(
        "",
        "audit_snapshot_digest_mismatch",
        "MCP audit snapshot digest does not match its payload",
      );
    }
    let head = auditGenesis;
    let previousSequence = 0;
    for (const record of snapshot.records) {
      if (record.sequence <= previousSequence) {
        throw auditError(
          record.serverId,
          "audit_sequence_regression",
          "MCP audit sequence is not strictly increasing",
        );
      }
      if (record.previousHash !== head) {
        throw auditError(
          record.serverId,
          "audit_previous_hash_mismatch",
          `MCP audit record ${record.auditId} has an invalid previous hash`,
        );
      }
      const {
        auditHash: _ignored,
        ...base
      } = record;
      const expected = sha256({
        previous_hash: head,
        record: base,
      });
      if (record.auditHash !== expected) {
        throw auditError(
          record.serverId,
          "audit_hash_mismatch",
          `MCP audit record ${record.auditId} has an invalid hash`,
        );
      }
      head = record.auditHash;
      previousSequence = record.sequence;
    }
    if (head !== snapshot.headHash) {
      throw auditError(
        "",
        "audit_head_hash_mismatch",
        "MCP audit head hash does not match its records",
      );
    }
    if (snapshot.sequence < previousSequence) {
      throw auditError(
        "",
        "audit_snapshot_sequence_regression",
        "MCP audit snapshot sequence is behind its records",
      );
    }
    this.records.length = 0;
    this.records.push(...snapshot.records.map(cloneJson));
    this.sequence = snapshot.sequence;
    this.headHash = snapshot.headHash;
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function auditError(
  serverId: string,
  code: string,
  message: string,
): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-audit-runtime", {
      server_id: serverId,
      code,
      message,
    }),
    category: code.includes("hash") || code.includes("snapshot")
      ? "restore"
      : "protocol",
    code,
    message,
    serverId,
    retryable: false,
    disposition: "terminal",
  });
}
