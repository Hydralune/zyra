import type { JsonObject, JsonValue } from "../contracts.ts";
import {
  canonicalJson,
  cloneJson,
  deterministicMcpId,
  monotonicNow,
  sha256,
} from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import type {
  McpElicitationPrimitiveSchema,
  McpElicitationRequest,
  McpElicitationResult,
} from "../core/protocol.ts";

export interface McpElicitationIdentity {
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  requestId: string;
}

export interface McpElicitationRecord {
  elicitationId: string;
  continuationId: string;
  identity: McpElicitationIdentity;
  request: McpElicitationRequest;
  requestDigest: string;
  status: "pending" | "accepted" | "declined" | "cancelled" | "expired" | "rejected";
  response: McpElicitationResult | null;
  responseDigest: string | null;
  rejectionCode: string | null;
  createdAt: string;
  expiresAt: string;
  resumedAt: string | null;
  metadata: JsonObject;
}

export interface McpElicitationResumeInput {
  elicitationId: string;
  continuationId: string;
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  requestId: string;
  result: McpElicitationResult;
}

export interface McpElicitationSnapshot {
  version: "zyra.mcp-elicitation-runtime/v1";
  revision: number;
  records: McpElicitationRecord[];
  digest: string;
  capturedAt: string;
}

export class McpElicitationRuntime {
  private readonly records = new Map<string, McpElicitationRecord>();
  private readonly byContinuation = new Map<string, string>();
  private readonly listeners = new Set<(record: McpElicitationRecord) => void | Promise<void>>();
  private readonly now: () => Date;
  private readonly defaultTtlMs: number;
  private readonly maximumTtlMs: number;
  private readonly allowUrlMode: boolean;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: {
    now?: () => Date;
    defaultTtlMs?: number;
    maximumTtlMs?: number;
    allowUrlMode?: boolean;
    snapshot?: McpElicitationSnapshot | null;
  } = {}) {
    this.now = options.now ?? (() => new Date());
    this.defaultTtlMs = options.defaultTtlMs ?? 10 * 60_000;
    this.maximumTtlMs = options.maximumTtlMs ?? 60 * 60_000;
    this.allowUrlMode = options.allowUrlMode ?? false;
    if (options.snapshot) this.restore(options.snapshot);
  }

  request(
    identityValue: McpElicitationIdentity,
    requestValue: McpElicitationRequest,
    options: { ttlMs?: number; metadata?: JsonObject } = {},
  ): McpElicitationRecord {
    const identity = normalizeIdentity(identityValue);
    const request = cloneJson(requestValue);
    if (request.mode === "url" && !this.allowUrlMode) {
      throw elicitationError(identity.serverId, "url_elicitation_disabled", "URL-mode MCP elicitation is disabled");
    }
    const requestDigest = sha256({ identity, request });
    const elicitationId = deterministicMcpId("mcp-elicitation", {
      run_id: identity.runId,
      task_id: identity.taskId,
      session_id: identity.sessionId,
      session_revision: identity.sessionRevision,
      worker_request_id: identity.workerRequestId,
      tool_call_id: identity.toolCallId,
      server_id: identity.serverId,
      connection_id: identity.connectionId,
      connection_epoch: identity.connectionEpoch,
      request_id: identity.requestId,
      request_digest: requestDigest,
    }, 40);
    const existing = this.records.get(elicitationId);
    if (existing) {
      if (existing.requestDigest !== requestDigest) throw elicitationError(identity.serverId, "elicitation_identity_conflict", `elicitation ${elicitationId} is bound to another payload`);
      return cloneJson(existing);
    }
    const continuationId = deterministicMcpId("mcp-elicitation-continuation", {
      elicitation_id: elicitationId,
      session_id: identity.sessionId,
      session_revision: identity.sessionRevision,
      request_id: identity.requestId,
    }, 40);
    const ttlMs = Math.max(1_000, Math.min(options.ttlMs ?? this.defaultTtlMs, this.maximumTtlMs));
    const createdAt = this.timestamp();
    const record: McpElicitationRecord = {
      elicitationId,
      continuationId,
      identity,
      request,
      requestDigest,
      status: "pending",
      response: null,
      responseDigest: null,
      rejectionCode: null,
      createdAt,
      expiresAt: new Date(Date.parse(createdAt) + ttlMs).toISOString(),
      resumedAt: null,
      metadata: cloneJson(options.metadata ?? {}),
    };
    this.records.set(elicitationId, record);
    this.byContinuation.set(continuationId, elicitationId);
    this.revision += 1;
    this.emit(record);
    return cloneJson(record);
  }

  resume(inputValue: McpElicitationResumeInput): McpElicitationRecord {
    const input = cloneJson(inputValue);
    const record = this.records.get(input.elicitationId);
    if (!record) throw elicitationError(input.serverId, "elicitation_not_found", `elicitation ${input.elicitationId} was not found`);
    if (record.continuationId !== input.continuationId || this.byContinuation.get(input.continuationId) !== input.elicitationId) {
      return this.rejectResponse(record, "wrong_continuation_id");
    }
    if (record.status !== "pending") {
      const responseDigest = sha256(input.result);
      if (record.responseDigest === responseDigest) return cloneJson(record);
      return this.rejectResponse(record, record.status === "accepted" || record.status === "declined" || record.status === "cancelled" ? "late_or_duplicate_response" : "elicitation_not_pending");
    }
    if (Date.parse(record.expiresAt) <= this.now().getTime()) {
      record.status = "expired";
      record.rejectionCode = "elicitation_expired";
      record.resumedAt = this.timestamp();
      this.revision += 1;
      this.emit(record);
      throw elicitationError(record.identity.serverId, "elicitation_expired", `elicitation ${record.elicitationId} has expired`);
    }
    const mismatch = identityMismatch(record.identity, input);
    if (mismatch) return this.rejectResponse(record, mismatch);
    const result = validateResult(record.request, input.result);
    record.response = result;
    record.responseDigest = sha256(result);
    record.status = result.action === "accept" ? "accepted" : result.action === "decline" ? "declined" : "cancelled";
    record.resumedAt = this.timestamp();
    record.rejectionCode = null;
    this.revision += 1;
    this.emit(record);
    return cloneJson(record);
  }

  rejectResponse(recordValue: McpElicitationRecord, code: string): McpElicitationRecord {
    const record = this.records.get(recordValue.elicitationId) ?? recordValue;
    if (record.status === "pending") {
      record.status = "rejected";
      record.rejectionCode = code;
      record.resumedAt = this.timestamp();
      this.revision += 1;
      this.emit(record);
    }
    throw elicitationError(record.identity.serverId, code, `elicitation response rejected: ${code}`);
  }

  cancel(elicitationId: string, reason = "cancelled_by_runtime"): McpElicitationRecord {
    const record = this.records.get(elicitationId);
    if (!record) throw elicitationError("", "elicitation_not_found", `elicitation ${elicitationId} was not found`);
    if (record.status === "pending") {
      record.status = "cancelled";
      record.response = { action: "cancel", content: null, meta: { reason } };
      record.responseDigest = sha256(record.response);
      record.resumedAt = this.timestamp();
      this.revision += 1;
      this.emit(record);
    }
    return cloneJson(record);
  }

  expire(): McpElicitationRecord[] {
    const output: McpElicitationRecord[] = [];
    const now = this.now().getTime();
    for (const record of this.records.values()) {
      if (record.status !== "pending" || Date.parse(record.expiresAt) > now) continue;
      record.status = "expired";
      record.rejectionCode = "elicitation_expired";
      record.resumedAt = this.timestamp();
      this.revision += 1;
      output.push(cloneJson(record));
      this.emit(record);
    }
    return output;
  }

  get(elicitationId: string): McpElicitationRecord | null {
    const value = this.records.get(elicitationId);
    return value ? cloneJson(value) : null;
  }

  pending(sessionId?: string): McpElicitationRecord[] {
    return [...this.records.values()]
      .filter((record) => record.status === "pending")
      .filter((record) => !sessionId || record.identity.sessionId === sessionId)
      .sort((left, right) => left.createdAt.localeCompare(right.createdAt))
      .map(cloneJson);
  }

  onChange(listener: (record: McpElicitationRecord) => void | Promise<void>): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  snapshot(): McpElicitationSnapshot {
    const withoutDigest = {
      version: "zyra.mcp-elicitation-runtime/v1" as const,
      revision: this.revision,
      records: [...this.records.values()].sort((left, right) => left.elicitationId.localeCompare(right.elicitationId)).map(cloneJson),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  restore(snapshot: McpElicitationSnapshot): void {
    if (snapshot.version !== "zyra.mcp-elicitation-runtime/v1") throw elicitationError("", "unsupported_elicitation_snapshot", "unsupported elicitation snapshot version");
    const { digest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== digest) throw elicitationError("", "elicitation_snapshot_digest_mismatch", "elicitation snapshot digest mismatch");
    this.records.clear();
    this.byContinuation.clear();
    this.revision = snapshot.revision;
    for (const record of snapshot.records) {
      this.records.set(record.elicitationId, cloneJson(record));
      this.byContinuation.set(record.continuationId, record.elicitationId);
    }
    this.expire();
  }

  private emit(record: McpElicitationRecord): void {
    for (const listener of this.listeners) void Promise.resolve(listener(cloneJson(record)));
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function normalizeIdentity(identity: McpElicitationIdentity): McpElicitationIdentity {
  for (const [key, value] of Object.entries(identity)) {
    if (key === "sessionRevision" || key === "connectionEpoch") {
      if (!Number.isSafeInteger(value) || (value as number) < 0) throw elicitationError(identity.serverId, "invalid_elicitation_identity", `${key} must be non-negative integer`);
    } else if (typeof value !== "string" || !value) {
      throw elicitationError(identity.serverId, "invalid_elicitation_identity", `${key} is required`);
    }
  }
  return cloneJson(identity);
}

function identityMismatch(expected: McpElicitationIdentity, input: McpElicitationResumeInput): string | null {
  const bindings: [keyof McpElicitationIdentity, unknown][] = [
    ["runId", input.runId],
    ["taskId", input.taskId],
    ["sessionId", input.sessionId],
    ["sessionRevision", input.sessionRevision],
    ["workerRequestId", input.workerRequestId],
    ["toolCallId", input.toolCallId],
    ["serverId", input.serverId],
    ["connectionId", input.connectionId],
    ["connectionEpoch", input.connectionEpoch],
    ["requestId", input.requestId],
  ];
  for (const [key, value] of bindings) if (expected[key] !== value) return `wrong_${camelToSnake(String(key))}`;
  return null;
}

function validateResult(request: McpElicitationRequest, resultValue: McpElicitationResult): McpElicitationResult {
  const result = cloneJson(resultValue);
  if (result.action !== "accept" && result.action !== "decline" && result.action !== "cancel") throw elicitationError("", "invalid_elicitation_action", `invalid elicitation action ${String(result.action)}`);
  if (result.action !== "accept") return { action: result.action, content: null, meta: result.meta ?? {} };
  if (request.mode === "url") return { action: "accept", content: result.content ?? {}, meta: result.meta ?? {} };
  const content = result.content ?? {};
  const output: JsonObject = {};
  for (const name of request.requestedSchema.required) {
    if (content[name] === undefined || content[name] === null) throw elicitationError("", "required_elicitation_field_missing", `elicitation field ${name} is required`);
  }
  for (const [name, value] of Object.entries(content)) {
    const schema = request.requestedSchema.properties[name];
    if (!schema) throw elicitationError("", "unknown_elicitation_field", `elicitation field ${name} is not allowed`);
    output[name] = validatePrimitive(name, value, schema);
  }
  return { action: "accept", content: output, meta: result.meta ?? {} };
}

function validatePrimitive(name: string, value: JsonValue, schema: McpElicitationPrimitiveSchema): JsonValue {
  if (schema.type === "string") {
    if (typeof value !== "string") throw elicitationError("", "elicitation_type_mismatch", `${name} must be string`);
    if (schema.minLength !== null && value.length < schema.minLength) throw elicitationError("", "elicitation_string_too_short", `${name} is shorter than ${schema.minLength}`);
    if (schema.maxLength !== null && value.length > schema.maxLength) throw elicitationError("", "elicitation_string_too_long", `${name} is longer than ${schema.maxLength}`);
    if (schema.format === "email" && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value)) throw elicitationError("", "elicitation_invalid_email", `${name} is not an email address`);
    if (schema.format === "uri") {
      try { new URL(value); } catch { throw elicitationError("", "elicitation_invalid_uri", `${name} is not a URI`); }
    }
  } else if (schema.type === "number" || schema.type === "integer") {
    if (typeof value !== "number" || !Number.isFinite(value)) throw elicitationError("", "elicitation_type_mismatch", `${name} must be number`);
    if (schema.type === "integer" && !Number.isSafeInteger(value)) throw elicitationError("", "elicitation_type_mismatch", `${name} must be integer`);
    if (schema.minimum !== null && value < schema.minimum) throw elicitationError("", "elicitation_number_too_small", `${name} is below ${schema.minimum}`);
    if (schema.maximum !== null && value > schema.maximum) throw elicitationError("", "elicitation_number_too_large", `${name} is above ${schema.maximum}`);
  } else if (typeof value !== "boolean") {
    throw elicitationError("", "elicitation_type_mismatch", `${name} must be boolean`);
  }
  if (schema.enum.length && !schema.enum.some((candidate) => sha256(candidate) === sha256(value))) throw elicitationError("", "elicitation_value_not_allowed", `${name} is not an allowed value`);
  return canonicalJson(value);
}

function camelToSnake(value: string): string {
  return value.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`);
}

function elicitationError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-elicitation", { server_id: serverId, code, message }),
    category: code.includes("wrong") || code.includes("duplicate") ? "conflict" : "authorization",
    code,
    message,
    serverId,
    retryable: false,
    disposition: "terminal",
  });
}
