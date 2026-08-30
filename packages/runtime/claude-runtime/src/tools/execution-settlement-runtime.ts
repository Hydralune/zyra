import { createHash, randomUUID } from "node:crypto";

import type { JsonObject, JsonValue } from "../contracts.ts";

export const EXECUTION_SETTLEMENT_SNAPSHOT_VERSION = "zyra.execution-settlement/v2";
const LEGACY_EXECUTION_SETTLEMENT_SNAPSHOT_VERSION = "zyra.execution-settlement/v1";

export type SettlementPermissionEffect = "unknown" | "allow" | "deny" | "ask";
export type SettlementCallState =
  | "planned"
  | "permission_blocked"
  | "delegating"
  | "gateway_accepted"
  | "local_running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "outcome_unknown"
  | "protocol_failed";
export type ToolTransactionState =
  | "receiving_arguments"
  | "arguments_complete"
  | "dispatch_intent_recorded"
  | "dispatched"
  | "result_acknowledged"
  | "outcome_unknown"
  | "completed";
export type SettlementBatchState =
  | "planned"
  | "permission_evaluated"
  | "delegating"
  | "settling"
  | "completed"
  | "failed"
  | "cancelled";

export interface SettlementIdentity {
  runtimeId: string;
  sessionId: string;
  runId: string;
  workerRequestId: string;
}

export interface PlannedSettlementCall {
  callId: string;
  toolName: string;
  arguments: JsonObject;
  localCapability: boolean;
  executionOwner: string;
  position: number;
  metadata?: JsonObject;
}

export interface PlanSettlementBatchInput {
  batchId: string;
  executionMode: string;
  calls: readonly PlannedSettlementCall[];
}

export interface GatewaySettlementInput {
  callId: string;
  ok: boolean;
  summary: string;
  output: JsonObject;
  error: string | null;
  receiptMetadata?: JsonObject;
  intermediate?: boolean;
}

export interface LocalSettlementInput {
  callId: string;
  ok: boolean;
  summary: string;
  output: JsonObject;
  error: string | null;
  metadata?: JsonObject;
}

export interface SettlementProgressChunk {
  chunkId: string;
  sequence: number;
  kind: "stdout" | "stderr" | "progress" | "artifact" | "diagnostic";
  content: JsonValue;
  contentDigest: string;
  characters: number;
  truncated: boolean;
  observedAt: string;
}

export interface SettlementCallRecord {
  callId: string;
  batchId: string;
  toolName: string;
  position: number;
  arguments: JsonObject;
  argumentsDigest: string;
  localCapability: boolean;
  executionOwner: string;
  permissionEffect: SettlementPermissionEffect;
  permissionReason: string;
  permissionRuleId: string | null;
  state: SettlementCallState;
  transactionState: ToolTransactionState;
  idempotencyKey: string;
  dispatchCredential: string | null;
  dispatchAttempts: number;
  recoveryAttempts: number;
  failureHistory: JsonObject[];
  dispatchIntentAt: string | null;
  dispatchedAt: string | null;
  resultAcknowledgedAt: string | null;
  delegated: boolean;
  gatewayAccepted: boolean;
  summary: string;
  output: JsonObject;
  outputDigest: string | null;
  error: string | null;
  progress: SettlementProgressChunk[];
  nextProgressSequence: number;
  progressCharacters: number;
  maximumProgressCharacters: number;
  createdAt: string;
  delegatedAt: string | null;
  gatewaySettledAt: string | null;
  completedAt: string | null;
  metadata: JsonObject;
  revision: number;
}

export interface SettlementBatchRecord {
  batchId: string;
  executionMode: string;
  state: SettlementBatchState;
  callIds: string[];
  delegatedCallIds: string[];
  blockedCallIds: string[];
  settledCallIds: string[];
  createdAt: string;
  completedAt: string | null;
  error: string | null;
  revision: number;
  digest: string;
}

export interface SettlementTransition {
  transitionId: string;
  sequence: number;
  restartEpoch: number;
  batchId: string;
  callId: string | null;
  operation: string;
  from: string;
  to: string;
  payload: JsonObject;
  payloadDigest: string;
  observedAt: string;
}

export interface SettlementAudit {
  ok: boolean;
  failures: string[];
  batchCount: number;
  callCount: number;
  delegatedCount: number;
  blockedCount: number;
  succeededCount: number;
  failedCount: number;
  activeCount: number;
  localCapabilityCount: number;
  progressChunkCount: number;
  transitionCount: number;
  uniqueTransitionCount: number;
  restartEpoch: number;
}

export interface ExecutionSettlementSnapshot {
  version: typeof EXECUTION_SETTLEMENT_SNAPSHOT_VERSION;
  identity: SettlementIdentity;
  restartEpoch: number;
  sequence: number;
  batches: SettlementBatchRecord[];
  calls: SettlementCallRecord[];
  transitions: SettlementTransition[];
  checksum: string;
}

export class ExecutionSettlementError extends Error {
  readonly code: string;
  readonly details: JsonObject;

  constructor(code: string, message: string, details: JsonObject = {}) {
    super(message);
    this.name = "ExecutionSettlementError";
    this.code = code;
    this.details = details;
  }
}

function stable(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map((entry) => stable(entry)).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${stable(record[key])}`).join(",")}}`;
}

function hash(value: unknown): string {
  return createHash("sha256").update(stable(value)).digest("hex");
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

const SENSITIVE_CREDENTIAL_FIELDS = new Set([
  "access_token",
  "api_key",
  "apikey",
  "authorization",
  "cookie",
  "password",
  "passwd",
  "proxy_authorization",
  "refresh_token",
  "secret",
  "set_cookie",
  "x_api_key",
]);

function normalizedCredentialField(value: string): string {
  return value.trim().toLowerCase().replaceAll("-", "_");
}

function isRedactedCredentialValue(value: unknown): boolean {
  if (value === null || value === undefined || value === "") return true;
  if (typeof value !== "string") return false;
  const normalized = value.trim().toLowerCase();
  return normalized === ""
    || normalized === "redacted"
    || normalized === "[redacted]"
    || normalized === "<redacted>"
    || normalized === "***";
}

function credentialMaterial(value: unknown): string | null {
  if (typeof value === "string") {
    const explicit = value.match(
      /\b(authorization|proxy-authorization)\s*[:=]\s*(bearer|basic)\s+[a-z0-9+/_=.-]{8,}/i,
    );
    if (explicit) return explicit[1]?.toLowerCase() ?? "authorization";
    const named = value.match(
      /\b(x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[:=]\s*["']?[a-z0-9+/_=.-]{8,}/i,
    );
    return named?.[1]?.toLowerCase() ?? null;
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = credentialMaterial(item);
      if (found) return found;
    }
    return null;
  }
  if (value === null || typeof value !== "object") return null;
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    const normalized = normalizedCredentialField(key);
    if (SENSITIVE_CREDENTIAL_FIELDS.has(normalized) && !isRedactedCredentialValue(item)) {
      return normalized;
    }
    const found = credentialMaterial(item);
    if (found) return found;
  }
  return null;
}

function now(): string {
  return new Date().toISOString();
}

function required(value: string, label: string): string {
  const normalized = value.trim();
  if (!normalized) throw new ExecutionSettlementError("settlement_required_value", `${label} must not be empty`, { label });
  return normalized;
}

function nonnegative(value: number, label: string): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new ExecutionSettlementError("settlement_invalid_number", `${label} must be a nonnegative safe integer`, {
      label,
      value,
    });
  }
  return value;
}

function bounded(value: number, minimum: number, maximum: number, label: string): number {
  if (!Number.isFinite(value)) {
    throw new ExecutionSettlementError("settlement_invalid_number", `${label} must be finite`, { label });
  }
  return Math.max(minimum, Math.min(maximum, Math.trunc(value)));
}

function normalizeIdentity(identity: SettlementIdentity): SettlementIdentity {
  return {
    runtimeId: required(identity.runtimeId, "settlement runtime id"),
    sessionId: required(identity.sessionId, "settlement session id"),
    runId: required(identity.runId, "settlement run id"),
    workerRequestId: required(identity.workerRequestId, "settlement worker request id"),
  };
}

function normalizeEffect(effect: SettlementPermissionEffect): SettlementPermissionEffect {
  return effect === "allow" || effect === "deny" || effect === "ask" ? effect : "unknown";
}

function terminalCall(state: SettlementCallState): boolean {
  return state === "permission_blocked"
    || state === "succeeded"
    || state === "failed"
    || state === "cancelled"
    || state === "outcome_unknown"
    || state === "protocol_failed";
}

function terminalBatch(state: SettlementBatchState): boolean {
  return state === "completed" || state === "failed" || state === "cancelled";
}

function textCharacters(value: JsonValue): number {
  if (typeof value === "string") return value.length;
  return stable(value).length;
}

function normalizeMetadata(value: JsonObject | undefined): JsonObject {
  return value ? clone(value) : {};
}

function batchDigest(batch: SettlementBatchRecord): string {
  const { digest: _digest, ...payload } = batch;
  return hash(payload);
}

function snapshotChecksum(snapshot: Omit<ExecutionSettlementSnapshot, "checksum">): string {
  return hash(snapshot);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export class ToolExecutionSettlementRuntime {
  private identity: SettlementIdentity;
  private restartEpoch = 0;
  private sequence = 0;
  private readonly batches = new Map<string, SettlementBatchRecord>();
  private readonly calls = new Map<string, SettlementCallRecord>();
  private readonly transitions: SettlementTransition[] = [];

  constructor(identity: SettlementIdentity) {
    this.identity = normalizeIdentity(identity);
  }

  planBatch(input: PlanSettlementBatchInput): SettlementBatchRecord {
    const batchId = required(input.batchId, "settlement batch id");
    const executionMode = required(input.executionMode, "settlement execution mode");
    const existing = this.batches.get(batchId);
    if (existing) {
      const candidate = hash({
        executionMode,
        calls: input.calls.map((call) => this.planDigest(call)),
      });
      const actual = hash({
        executionMode: existing.executionMode,
        calls: existing.callIds.map((callId) => this.callPlanDigest(this.requireCall(callId))),
      });
      if (candidate !== actual) {
        throw new ExecutionSettlementError(
          "settlement_batch_reuse_conflict",
          `settlement batch ${batchId} was reused with different calls`,
          { batch_id: batchId },
        );
      }
      return clone(existing);
    }
    if (input.calls.length === 0) {
      throw new ExecutionSettlementError("settlement_empty_batch", "settlement batch requires at least one call", {
        batch_id: batchId,
      });
    }
    const callIds = input.calls.map((call) => required(call.callId, "settlement call id"));
    if (new Set(callIds).size !== callIds.length) {
      throw new ExecutionSettlementError("settlement_duplicate_batch_call", `batch ${batchId} contains duplicate call ids`);
    }
    for (const callId of callIds) {
      if (this.calls.has(callId)) {
        throw new ExecutionSettlementError(
          "settlement_call_reuse",
          `tool call ${callId} already belongs to another settlement batch`,
          { call_id: callId, batch_id: batchId },
        );
      }
    }
    const createdAt = now();
    const batch: SettlementBatchRecord = {
      batchId,
      executionMode,
      state: "planned",
      callIds,
      delegatedCallIds: [],
      blockedCallIds: [],
      settledCallIds: [],
      createdAt,
      completedAt: null,
      error: null,
      revision: 1,
      digest: "",
    };
    for (const source of input.calls) {
      const callId = required(source.callId, "settlement call id");
      const toolName = required(source.toolName, "settlement tool name");
      const position = nonnegative(source.position, "settlement call position");
      const call: SettlementCallRecord = {
        callId,
        batchId,
        toolName,
        position,
        arguments: clone(source.arguments),
        argumentsDigest: hash(source.arguments),
        localCapability: source.localCapability,
        executionOwner: required(source.executionOwner, "settlement execution owner"),
        permissionEffect: "unknown",
        permissionReason: "",
        permissionRuleId: null,
        state: "planned",
        transactionState: "arguments_complete",
        idempotencyKey: this.effectKey(source),
        dispatchCredential: null,
        dispatchAttempts: 0,
        recoveryAttempts: 0,
        failureHistory: [],
        dispatchIntentAt: null,
        dispatchedAt: null,
        resultAcknowledgedAt: null,
        delegated: false,
        gatewayAccepted: false,
        summary: "",
        output: {},
        outputDigest: null,
        error: null,
        progress: [],
        nextProgressSequence: 0,
        progressCharacters: 0,
        maximumProgressCharacters: 1_000_000,
        createdAt,
        delegatedAt: null,
        gatewaySettledAt: null,
        completedAt: null,
        metadata: normalizeMetadata(source.metadata),
        revision: 1,
      };
      this.calls.set(callId, call);
      this.transition(batch, call, "call.planned", "none", "planned", {
        tool_name: toolName,
        position,
        arguments_digest: call.argumentsDigest,
        local_capability: call.localCapability,
        execution_owner: call.executionOwner,
      });
    }
    batch.digest = batchDigest(batch);
    this.batches.set(batchId, batch);
    this.transition(batch, null, "batch.planned", "none", "planned", {
      execution_mode: executionMode,
      call_ids: callIds,
    });
    return clone(batch);
  }

  recordPermission(
    callId: string,
    effect: SettlementPermissionEffect,
    reason: string,
    ruleId: string | null = null,
  ): SettlementCallRecord {
    const call = this.requireCall(callId);
    const batch = this.requireBatch(call.batchId);
    const normalized = normalizeEffect(effect);
    if (normalized === "unknown") {
      throw new ExecutionSettlementError(
        "settlement_unknown_permission",
        `permission effect for ${callId} must be allow, deny, or ask`,
        { call_id: callId },
      );
    }
    if (call.permissionEffect === "ask" && normalized !== "ask") {
      const from = call.state;
      call.permissionEffect = "unknown";
      call.permissionReason = "";
      call.permissionRuleId = null;
      call.state = "planned";
      call.summary = "";
      call.output = {};
      call.outputDigest = null;
      call.error = null;
      call.completedAt = null;
      call.revision += 1;
      batch.blockedCallIds = batch.blockedCallIds.filter((item) => item !== callId);
      batch.settledCallIds = batch.settledCallIds.filter((item) => item !== callId);
      batch.state = "planned";
      batch.completedAt = null;
      batch.error = null;
      batch.revision += 1;
      batch.digest = batchDigest(batch);
      this.transition(batch, call, "permission.resumed", from, "planned", {
        effect: normalized,
        reason: reason.trim(),
        rule_id: ruleId?.trim() || null,
      });
    } else if (call.permissionEffect !== "unknown") {
      if (
        call.permissionEffect === normalized
        && call.permissionReason === reason.trim()
        && call.permissionRuleId === (ruleId?.trim() || null)
      ) {
        return clone(call);
      }
      throw new ExecutionSettlementError(
        "settlement_permission_conflict",
        `permission for ${callId} was already committed`,
        { call_id: callId, existing: call.permissionEffect, candidate: normalized },
      );
    }
    if (call.state !== "planned") {
      throw new ExecutionSettlementError(
        "settlement_permission_state",
        `permission for ${callId} cannot be recorded from ${call.state}`,
        { call_id: callId, state: call.state },
      );
    }
    call.permissionEffect = normalized;
    call.permissionReason = reason.trim() || `permission_${normalized}`;
    call.permissionRuleId = ruleId?.trim() || null;
    call.revision += 1;
    if (normalized === "deny" || normalized === "ask") {
      call.state = "permission_blocked";
      call.summary = normalized === "ask" ? "Tool approval is required." : "Tool execution was denied.";
      call.error = normalized === "ask" ? "permission_approval_required" : "permission_denied";
      call.output = {
        permission_effect: normalized,
        permission_reason: call.permissionReason,
        permission_rule_id: call.permissionRuleId,
      };
      call.outputDigest = hash(call.output);
      call.completedAt = now();
      batch.blockedCallIds.push(callId);
      batch.settledCallIds.push(callId);
      this.transition(batch, call, "permission.blocked", "planned", "permission_blocked", {
        effect: normalized,
        reason: call.permissionReason,
        rule_id: call.permissionRuleId,
      });
    } else {
      this.transition(batch, call, "permission.allowed", "planned", "planned", {
        effect: normalized,
        reason: call.permissionReason,
        rule_id: call.permissionRuleId,
      });
    }
    this.refreshBatchAfterPermission(batch);
    return clone(call);
  }

  beginDelegation(batchId: string, callIds: readonly string[]): SettlementBatchRecord {
    const batch = this.requireBatch(batchId);
    if (terminalBatch(batch.state)) {
      throw new ExecutionSettlementError(
        "settlement_terminal_batch",
        `cannot delegate terminal batch ${batchId}`,
        { batch_id: batchId, state: batch.state },
      );
    }
    if (batch.state === "planned") this.refreshBatchAfterPermission(batch);
    const unique = [...new Set(callIds.map((callId) => required(callId, "delegated call id")))];
    if (unique.length !== callIds.length) {
      throw new ExecutionSettlementError("settlement_duplicate_delegation", `batch ${batchId} repeats delegated call ids`);
    }
    const expected = batch.callIds.filter((callId) => this.requireCall(callId).permissionEffect === "allow");
    if (hash([...unique].sort()) !== hash([...expected].sort())) {
      throw new ExecutionSettlementError(
        "settlement_delegation_set",
        `delegated call set for ${batchId} does not equal the allowed call set`,
        { batch_id: batchId, delegated: unique, expected },
      );
    }
    if (batch.delegatedCallIds.length > 0) {
      if (hash(batch.delegatedCallIds) === hash(unique)) return clone(batch);
      throw new ExecutionSettlementError("settlement_delegation_replay", `batch ${batchId} delegation changed on replay`);
    }
    batch.delegatedCallIds.push(...unique);
    const delegatedAt = now();
    for (const callId of unique) {
      const call = this.requireCall(callId);
      if (call.state !== "planned" || call.permissionEffect !== "allow") {
        throw new ExecutionSettlementError(
          "settlement_call_not_delegable",
          `call ${callId} cannot be delegated from ${call.state}/${call.permissionEffect}`,
          { call_id: callId, state: call.state, effect: call.permissionEffect },
        );
      }
      call.state = "delegating";
      call.transactionState = "dispatch_intent_recorded";
      call.dispatchCredential = `tool-dispatch:${hash({
        idempotencyKey: call.idempotencyKey,
        argumentsDigest: call.argumentsDigest,
      })}`;
      call.dispatchAttempts += 1;
      call.dispatchIntentAt = delegatedAt;
      call.delegated = true;
      call.delegatedAt = delegatedAt;
      call.revision += 1;
      this.transition(batch, call, "call.delegated", "planned", "delegating", {
        execution_owner: call.executionOwner,
        local_capability: call.localCapability,
        transaction_state: call.transactionState,
        idempotency_key: call.idempotencyKey,
        dispatch_credential: call.dispatchCredential,
      });
    }
    const from = batch.state;
    batch.state = unique.length > 0 ? "delegating" : "settling";
    batch.revision += 1;
    batch.digest = batchDigest(batch);
    this.transition(batch, null, "batch.delegated", from, batch.state, { call_ids: unique });
    return clone(batch);
  }

  appendProgress(
    callId: string,
    sequence: number,
    kind: SettlementProgressChunk["kind"],
    content: JsonValue,
    maximumCharacters?: number,
  ): SettlementProgressChunk {
    const call = this.requireCall(callId);
    const batch = this.requireBatch(call.batchId);
    if (terminalCall(call.state)) {
      throw new ExecutionSettlementError(
        "settlement_progress_terminal",
        `cannot append progress to terminal call ${callId}`,
        { call_id: callId, state: call.state },
      );
    }
    const expected = call.nextProgressSequence;
    const selectedSequence = nonnegative(sequence, "progress sequence");
    const existing = call.progress.find((chunk) => chunk.sequence === selectedSequence);
    if (existing) {
      if (existing.contentDigest !== hash(content) || existing.kind !== kind) {
        throw new ExecutionSettlementError(
          "settlement_progress_conflict",
          `progress sequence ${selectedSequence} conflicts for ${callId}`,
          { call_id: callId, sequence: selectedSequence },
        );
      }
      return clone(existing);
    }
    if (selectedSequence !== expected) {
      throw new ExecutionSettlementError(
        "settlement_progress_gap",
        `progress sequence gap for ${callId}: expected ${expected}, received ${selectedSequence}`,
        { call_id: callId, expected, actual: selectedSequence },
      );
    }
    if (maximumCharacters !== undefined) {
      call.maximumProgressCharacters = bounded(maximumCharacters, 1, 100_000_000, "progress character limit");
    }
    const characters = textCharacters(content);
    const available = Math.max(0, call.maximumProgressCharacters - call.progressCharacters);
    let stored: JsonValue = clone(content);
    let storedCharacters = characters;
    let truncated = false;
    if (characters > available) {
      truncated = true;
      if (typeof content === "string") {
        stored = content.slice(0, available);
        storedCharacters = Math.min(content.length, available);
      } else {
        stored = {
          withheld: true,
          original_digest: hash(content),
          original_characters: characters,
          available_characters: available,
        };
        storedCharacters = textCharacters(stored);
      }
    }
    const chunk: SettlementProgressChunk = {
      chunkId: `settlement-progress:${callId}:${selectedSequence}:${hash(stored).slice(0, 16)}`,
      sequence: selectedSequence,
      kind,
      content: stored,
      contentDigest: hash(stored),
      characters: storedCharacters,
      truncated,
      observedAt: now(),
    };
    call.progress.push(chunk);
    call.nextProgressSequence += 1;
    call.progressCharacters += storedCharacters;
    call.revision += 1;
    this.transition(batch, call, "call.progress", call.state, call.state, {
      chunk_id: chunk.chunkId,
      sequence: selectedSequence,
      kind,
      characters: storedCharacters,
      truncated,
    });
    return clone(chunk);
  }

  recordGatewayReceipt(input: GatewaySettlementInput): SettlementCallRecord {
    const call = this.requireCall(input.callId);
    const batch = this.requireBatch(call.batchId);
    if (call.permissionEffect !== "allow" || !call.delegated) {
      throw new ExecutionSettlementError(
        "settlement_unauthorized_receipt",
        `gateway returned a receipt for non-delegated call ${call.callId}`,
        { call_id: call.callId, effect: call.permissionEffect, delegated: call.delegated },
      );
    }
    if (terminalCall(call.state)) {
      const candidate = hash({ ok: input.ok, summary: input.summary, output: input.output, error: input.error });
      if (call.outputDigest === candidate) return clone(call);
      throw new ExecutionSettlementError(
        "settlement_gateway_replay_conflict",
        `gateway receipt conflicts with terminal call ${call.callId}`,
        { call_id: call.callId },
      );
    }
    if (call.state !== "delegating") {
      throw new ExecutionSettlementError(
        "settlement_gateway_state",
        `gateway receipt for ${call.callId} cannot settle from ${call.state}`,
        { call_id: call.callId, state: call.state },
      );
    }
    const from = call.state;
    const receiptTransaction = String(input.receiptMetadata?.tool_transaction_state ?? "");
    if (receiptTransaction === "outcome_unknown") {
      call.transactionState = "outcome_unknown";
      call.state = "outcome_unknown";
      call.error = input.error ?? "tool_outcome_unknown";
      call.summary = input.summary.trim() || "Tool dispatch outcome could not be confirmed.";
      call.output = clone(input.output);
      call.outputDigest = hash(call.output);
      call.metadata = { ...call.metadata, ...normalizeMetadata(input.receiptMetadata) };
      call.completedAt = now();
      call.failureHistory.push({
        stage: "result_acknowledgement",
        error: call.error,
        recoverable: false,
        dispatch_credential: call.dispatchCredential,
      });
      call.revision += 1;
      this.addSettled(batch, call.callId);
      this.transition(batch, call, "gateway.outcome_unknown", from, call.state, {
        dispatch_credential: call.dispatchCredential,
        error: call.error,
      });
      this.refreshBatchSettlement(batch);
      this.reconcileEquivalentOpenCalls(call);
      return clone(call);
    }
    call.transactionState = "result_acknowledged";
    call.dispatchedAt ??= call.gatewaySettledAt ?? now();
    call.resultAcknowledgedAt = now();
    call.gatewayAccepted = input.ok;
    call.gatewaySettledAt = now();
    call.summary = input.summary.trim();
    call.output = clone(input.output);
    call.error = input.ok ? null : required(input.error ?? "gateway_execution_failed", "gateway error");
    call.metadata = { ...call.metadata, ...normalizeMetadata(input.receiptMetadata) };
    call.outputDigest = hash({
      ok: input.ok,
      summary: call.summary,
      output: call.output,
      error: call.error,
      intermediate: input.intermediate === true,
    });
    if (input.intermediate && input.ok && call.localCapability) {
      call.state = "gateway_accepted";
      call.transactionState = "dispatch_intent_recorded";
      call.revision += 1;
      this.transition(batch, call, "gateway.permission_commit", from, call.state, {
        receipt_digest: call.outputDigest,
        execution_owner: call.executionOwner,
      });
      return clone(call);
    }
    call.state = input.ok ? "succeeded" : "failed";
    call.transactionState = "completed";
    call.completedAt = now();
    call.revision += 1;
    this.addSettled(batch, call.callId);
    this.transition(batch, call, "gateway.settled", from, call.state, {
      receipt_digest: call.outputDigest,
      error: call.error,
    });
    this.refreshBatchSettlement(batch);
    this.reconcileEquivalentOpenCalls(call);
    return clone(call);
  }

  beginLocalCapability(callId: string): SettlementCallRecord {
    const call = this.requireCall(callId);
    const batch = this.requireBatch(call.batchId);
    if (!call.localCapability) {
      throw new ExecutionSettlementError(
        "settlement_not_local",
        `call ${callId} is not owned by a local TypeScript capability`,
        { call_id: callId },
      );
    }
    if (call.state === "local_running") return clone(call);
    if (call.state !== "gateway_accepted") {
      throw new ExecutionSettlementError(
        "settlement_local_state",
        `local capability ${callId} cannot start from ${call.state}`,
        { call_id: callId, state: call.state },
      );
    }
    call.state = "local_running";
    call.transactionState = "dispatched";
    call.dispatchedAt = now();
    call.revision += 1;
    this.transition(batch, call, "local.started", "gateway_accepted", "local_running", {
      execution_owner: call.executionOwner,
    });
    return clone(call);
  }

  recordLocalSettlement(input: LocalSettlementInput): SettlementCallRecord {
    const call = this.requireCall(input.callId);
    const batch = this.requireBatch(call.batchId);
    if (!call.localCapability) {
      throw new ExecutionSettlementError(
        "settlement_not_local",
        `call ${call.callId} cannot receive a local settlement`,
        { call_id: call.callId },
      );
    }
    if (call.state === "gateway_accepted") this.beginLocalCapability(call.callId);
    if (terminalCall(call.state)) {
      const candidate = hash({ ok: input.ok, summary: input.summary, output: input.output, error: input.error });
      if (call.outputDigest === candidate) return clone(call);
      throw new ExecutionSettlementError(
        "settlement_local_replay_conflict",
        `local settlement conflicts for ${call.callId}`,
        { call_id: call.callId },
      );
    }
    if (call.state !== "local_running") {
      throw new ExecutionSettlementError(
        "settlement_local_state",
        `local settlement ${call.callId} cannot commit from ${call.state}`,
        { call_id: call.callId, state: call.state },
      );
    }
    const from = call.state;
    call.transactionState = "result_acknowledged";
    call.resultAcknowledgedAt = now();
    call.summary = input.summary.trim();
    call.output = clone(input.output);
    call.error = input.ok ? null : required(input.error ?? "typescript_capability_error", "local capability error");
    call.metadata = { ...call.metadata, ...normalizeMetadata(input.metadata) };
    call.outputDigest = hash({ ok: input.ok, summary: call.summary, output: call.output, error: call.error });
    call.state = input.ok ? "succeeded" : "failed";
    call.transactionState = "completed";
    call.completedAt = now();
    call.revision += 1;
    this.addSettled(batch, call.callId);
    this.transition(batch, call, "local.settled", from, call.state, {
      receipt_digest: call.outputDigest,
      error: call.error,
    });
    this.refreshBatchSettlement(batch);
    return clone(call);
  }

  protocolFailure(callId: string, code: string, message: string): SettlementCallRecord {
    const call = this.requireCall(callId);
    const batch = this.requireBatch(call.batchId);
    if (terminalCall(call.state)) return clone(call);
    const from = call.state;
    call.state = "protocol_failed";
    call.error = required(code, "settlement protocol failure code");
    call.summary = message.trim() || call.error;
    call.output = { protocol_error: call.error, message: call.summary };
    call.outputDigest = hash(call.output);
    call.completedAt = now();
    call.revision += 1;
    this.addSettled(batch, call.callId);
    this.transition(batch, call, "call.protocol_failed", from, call.state, {
      code: call.error,
      message: call.summary,
    });
    this.refreshBatchSettlement(batch);
    return clone(call);
  }

  cancelCall(callId: string, reason: string): SettlementCallRecord {
    const call = this.requireCall(callId);
    const batch = this.requireBatch(call.batchId);
    if (terminalCall(call.state)) return clone(call);
    const from = call.state;
    call.state = "cancelled";
    call.error = required(reason, "settlement cancellation reason");
    call.summary = `Tool execution cancelled: ${call.error}`;
    call.output = { cancelled: true, reason: call.error };
    call.outputDigest = hash(call.output);
    call.completedAt = now();
    call.revision += 1;
    this.addSettled(batch, call.callId);
    this.transition(batch, call, "call.cancelled", from, call.state, { reason: call.error });
    this.refreshBatchSettlement(batch);
    return clone(call);
  }

  completeBatch(batchId: string): SettlementBatchRecord {
    const batch = this.requireBatch(batchId);
    if (batch.state === "completed") return clone(batch);
    if (batch.state === "failed" || batch.state === "cancelled") {
      throw new ExecutionSettlementError(
        "settlement_terminal_batch",
        `cannot complete ${batch.state} batch ${batchId}`,
        { batch_id: batchId, state: batch.state },
      );
    }
    const unsettled = batch.callIds.filter((callId) => !terminalCall(this.requireCall(callId).state));
    if (unsettled.length > 0) {
      throw new ExecutionSettlementError(
        "settlement_unsettled_batch",
        `batch ${batchId} has unsettled calls`,
        { batch_id: batchId, call_ids: unsettled },
      );
    }
    const from = batch.state;
    batch.state = "completed";
    batch.completedAt = now();
    batch.settledCallIds = [...batch.callIds];
    batch.revision += 1;
    batch.digest = batchDigest(batch);
    this.transition(batch, null, "batch.completed", from, "completed", {
      succeeded_call_ids: batch.callIds.filter((callId) => this.requireCall(callId).state === "succeeded"),
      failed_call_ids: batch.callIds.filter((callId) => this.requireCall(callId).state !== "succeeded"),
    });
    return clone(batch);
  }

  failBatch(batchId: string, error: unknown): SettlementBatchRecord {
    const batch = this.requireBatch(batchId);
    if (terminalBatch(batch.state)) return clone(batch);
    for (const callId of batch.callIds) {
      const call = this.requireCall(callId);
      if (!terminalCall(call.state)) this.protocolFailure(callId, "settlement_batch_failed", errorMessage(error));
    }
    const from = batch.state;
    batch.state = "failed";
    batch.error = errorMessage(error);
    batch.completedAt = now();
    batch.revision += 1;
    batch.digest = batchDigest(batch);
    this.transition(batch, null, "batch.failed", from, "failed", { error: batch.error });
    return clone(batch);
  }

  cancelBatch(batchId: string, reason: string): SettlementBatchRecord {
    const batch = this.requireBatch(batchId);
    if (terminalBatch(batch.state)) return clone(batch);
    for (const callId of batch.callIds) {
      const call = this.requireCall(callId);
      if (!terminalCall(call.state)) this.cancelCall(callId, reason);
    }
    const from = batch.state;
    batch.state = "cancelled";
    batch.error = required(reason, "settlement batch cancellation reason");
    batch.completedAt = now();
    batch.revision += 1;
    batch.digest = batchDigest(batch);
    this.transition(batch, null, "batch.cancelled", from, "cancelled", { reason: batch.error });
    return clone(batch);
  }

  call(callId: string): SettlementCallRecord | null {
    const record = this.calls.get(callId);
    return record ? clone(record) : null;
  }

  batch(batchId: string): SettlementBatchRecord | null {
    const record = this.batches.get(batchId);
    return record ? clone(record) : null;
  }

  orderedReceipts(batchId: string): SettlementCallRecord[] {
    const batch = this.requireBatch(batchId);
    return batch.callIds.map((callId) => clone(this.requireCall(callId))).sort((left, right) => left.position - right.position);
  }

  activeCalls(): SettlementCallRecord[] {
    return [...this.calls.values()].filter((call) => !terminalCall(call.state)).map(clone);
  }

  audit(): SettlementAudit {
    const failures: string[] = [];
    const transitionIds = this.transitions.map((transition) => transition.transitionId);
    if (new Set(transitionIds).size !== transitionIds.length) failures.push("transition ids are not unique");
    let priorSequence = 0;
    for (const transition of this.transitions) {
      if (transition.sequence <= priorSequence) failures.push(`transition sequence regressed at ${transition.transitionId}`);
      priorSequence = transition.sequence;
      if (transition.payloadDigest !== hash(transition.payload)) {
        failures.push(`transition payload digest mismatch: ${transition.transitionId}`);
      }
      if (!this.batches.has(transition.batchId)) {
        failures.push(`transition references unknown batch: ${transition.transitionId}`);
      }
      if (transition.callId && !this.calls.has(transition.callId)) {
        failures.push(`transition references unknown call: ${transition.transitionId}`);
      }
    }
    for (const batch of this.batches.values()) {
      if (batch.digest !== batchDigest(batch)) failures.push(`batch digest mismatch: ${batch.batchId}`);
      if (new Set(batch.callIds).size !== batch.callIds.length) failures.push(`batch repeats call ids: ${batch.batchId}`);
      for (const callId of batch.callIds) {
        const call = this.calls.get(callId);
        if (!call) {
          failures.push(`batch references unknown call: ${batch.batchId}:${callId}`);
          continue;
        }
        if (call.batchId !== batch.batchId) failures.push(`call batch mismatch: ${callId}`);
        if (call.argumentsDigest !== hash(call.arguments)) failures.push(`call arguments digest mismatch: ${callId}`);
        if (call.delegated && call.permissionEffect !== "allow") failures.push(`blocked call delegated: ${callId}`);
        if (!call.delegated && (call.state === "gateway_accepted" || call.state === "local_running")) {
          failures.push(`non-delegated call reached execution: ${callId}`);
        }
        if (call.permissionEffect === "deny" || call.permissionEffect === "ask") {
          if (call.state !== "permission_blocked") failures.push(`blocked call has invalid state: ${callId}:${call.state}`);
        }
        if (terminalCall(call.state) && !call.completedAt) failures.push(`terminal call lacks completion time: ${callId}`);
        if (call.outputDigest && terminalCall(call.state)) {
          const direct = hash(call.output);
          const receipt = hash({ ok: call.state === "succeeded", summary: call.summary, output: call.output, error: call.error });
          if (call.outputDigest !== direct && call.outputDigest !== receipt) {
            const intermediate = hash({
              ok: call.state === "succeeded",
              summary: call.summary,
              output: call.output,
              error: call.error,
              intermediate: false,
            });
            if (call.outputDigest !== intermediate) failures.push(`call output digest mismatch: ${callId}`);
          }
        }
        let expectedProgress = 0;
        for (const chunk of call.progress) {
          if (chunk.sequence !== expectedProgress) failures.push(`progress sequence gap: ${callId}:${chunk.sequence}`);
          if (chunk.contentDigest !== hash(chunk.content)) failures.push(`progress digest mismatch: ${callId}:${chunk.sequence}`);
          expectedProgress += 1;
        }
        if (call.nextProgressSequence !== expectedProgress) failures.push(`next progress sequence mismatch: ${callId}`);
      }
      if (terminalBatch(batch.state) && !batch.completedAt) failures.push(`terminal batch lacks completion time: ${batch.batchId}`);
      if (batch.state === "completed") {
        const unsettled = batch.callIds.filter((callId) => !terminalCall(this.requireCall(callId).state));
        if (unsettled.length > 0) failures.push(`completed batch has active calls: ${batch.batchId}`);
      }
    }
    const calls = [...this.calls.values()];
    return {
      ok: failures.length === 0,
      failures,
      batchCount: this.batches.size,
      callCount: this.calls.size,
      delegatedCount: calls.filter((call) => call.delegated).length,
      blockedCount: calls.filter((call) => call.state === "permission_blocked").length,
      succeededCount: calls.filter((call) => call.state === "succeeded").length,
      failedCount: calls.filter((call) => call.state === "failed" || call.state === "protocol_failed").length,
      activeCount: calls.filter((call) => !terminalCall(call.state)).length,
      localCapabilityCount: calls.filter((call) => call.localCapability).length,
      progressChunkCount: calls.reduce((total, call) => total + call.progress.length, 0),
      transitionCount: this.transitions.length,
      uniqueTransitionCount: new Set(transitionIds).size,
      restartEpoch: this.restartEpoch,
    };
  }

  snapshot(): ExecutionSettlementSnapshot {
    const audit = this.audit();
    if (!audit.ok) {
      throw new ExecutionSettlementError("settlement_snapshot_invariant", audit.failures.join("; "));
    }
    const unsigned: Omit<ExecutionSettlementSnapshot, "checksum"> = {
      version: EXECUTION_SETTLEMENT_SNAPSHOT_VERSION,
      identity: clone(this.identity),
      restartEpoch: this.restartEpoch,
      sequence: this.sequence,
      batches: [...this.batches.values()].map(clone).sort((left, right) => left.batchId.localeCompare(right.batchId)),
      calls: [...this.calls.values()].map(clone).sort((left, right) => left.callId.localeCompare(right.callId)),
      transitions: this.transitions.map(clone),
    };
    this.assertSecretFree(unsigned);
    return { ...unsigned, checksum: snapshotChecksum(unsigned) };
  }

  restore(snapshot: ExecutionSettlementSnapshot, allowRunRebind = false): void {
    if (this.batches.size > 0 || this.calls.size > 0 || this.transitions.length > 0) {
      throw new ExecutionSettlementError("settlement_restore_dirty", "settlement restore requires a fresh runtime");
    }
    if (
      snapshot.version !== EXECUTION_SETTLEMENT_SNAPSHOT_VERSION
      && String(snapshot.version) !== LEGACY_EXECUTION_SETTLEMENT_SNAPSHOT_VERSION
    ) {
      throw new ExecutionSettlementError(
        "settlement_snapshot_version",
        `unsupported execution settlement snapshot: ${String(snapshot.version)}`,
      );
    }
    const { checksum, ...unsigned } = snapshot;
    if (snapshotChecksum(unsigned) !== checksum) {
      throw new ExecutionSettlementError("settlement_snapshot_checksum", "execution settlement snapshot checksum mismatch");
    }
    if (
      snapshot.identity.sessionId !== this.identity.sessionId
      || (!allowRunRebind && snapshot.identity.workerRequestId !== this.identity.workerRequestId)
    ) {
      throw new ExecutionSettlementError(
        "settlement_snapshot_identity",
        "execution settlement snapshot identity mismatch",
        {
          expected_session_id: this.identity.sessionId,
          actual_session_id: snapshot.identity.sessionId,
          expected_worker_request_id: this.identity.workerRequestId,
          actual_worker_request_id: snapshot.identity.workerRequestId,
        },
      );
    }
    if (!allowRunRebind && snapshot.identity.runId !== this.identity.runId) {
      throw new ExecutionSettlementError("settlement_snapshot_run", "execution settlement run identity mismatch");
    }
    this.identity = {
      ...normalizeIdentity(snapshot.identity),
      runId: allowRunRebind ? this.identity.runId : snapshot.identity.runId,
      workerRequestId: allowRunRebind
        ? this.identity.workerRequestId
        : snapshot.identity.workerRequestId,
    };
    this.restartEpoch = nonnegative(snapshot.restartEpoch, "settlement restart epoch") + 1;
    this.sequence = nonnegative(snapshot.sequence, "settlement transition sequence");
    for (const batch of snapshot.batches) {
      if (this.batches.has(batch.batchId)) {
        throw new ExecutionSettlementError("settlement_snapshot_duplicate_batch", `duplicate restored batch ${batch.batchId}`);
      }
      this.batches.set(batch.batchId, clone(batch));
    }
    for (const call of snapshot.calls) {
      if (this.calls.has(call.callId)) {
        throw new ExecutionSettlementError("settlement_snapshot_duplicate_call", `duplicate restored call ${call.callId}`);
      }
      const restored = clone(call);
      restored.transactionState ??= restored.state === "planned"
        ? "arguments_complete"
        : restored.state === "delegating"
          ? "dispatch_intent_recorded"
          : terminalCall(restored.state) ? "completed" : "dispatched";
      restored.idempotencyKey ??= this.effectKey(restored);
      restored.dispatchCredential ??= null;
      restored.dispatchAttempts ??= restored.delegated ? 1 : 0;
      restored.recoveryAttempts ??= 0;
      restored.failureHistory ??= [];
      restored.dispatchIntentAt ??= restored.delegatedAt;
      restored.dispatchedAt ??= null;
      restored.resultAcknowledgedAt ??= restored.gatewaySettledAt;
      this.calls.set(restored.callId, restored);
    }
    this.transitions.push(...snapshot.transitions.map(clone));
    for (const call of this.calls.values()) {
      if (call.state === "delegating" && call.transactionState === "dispatch_intent_recorded") {
        call.recoveryAttempts += 1;
        call.failureHistory.push({
          stage: "dispatch_intent",
          error: "runtime_restarted_before_dispatch_confirmation",
          recoverable: true,
          restart_epoch: this.restartEpoch,
        });
        continue;
      }
      if (call.state === "delegating" || call.state === "gateway_accepted" || call.state === "local_running") {
        const batch = this.requireBatch(call.batchId);
        const from = call.state;
        call.state = "outcome_unknown";
        call.transactionState = "outcome_unknown";
        call.error = "tool_outcome_unknown_after_restart";
        call.summary = "Tool was dispatched but no durable final receipt was acknowledged.";
        call.output = {
          interrupted: true,
          prior_state: from,
          restart_epoch: this.restartEpoch,
          dispatch_credential: call.dispatchCredential,
          duplicate_effect_fenced: true,
        };
        call.outputDigest = hash(call.output);
        call.failureHistory.push({
          stage: "result_acknowledgement",
          error: call.error,
          recoverable: false,
          restart_epoch: this.restartEpoch,
        });
        call.completedAt = now();
        call.revision += 1;
        this.addSettled(batch, call.callId);
        this.transition(batch, call, "call.restart_fenced", from, call.state, {
          restart_epoch: this.restartEpoch,
          prior_run_id: snapshot.identity.runId,
          current_run_id: this.identity.runId,
        });
      }
    }
    for (const batch of this.batches.values()) {
      if (!terminalBatch(batch.state)) this.refreshBatchSettlement(batch);
    }
    const audit = this.audit();
    if (!audit.ok) {
      throw new ExecutionSettlementError("settlement_restore_invariant", audit.failures.join("; "));
    }
  }

  private refreshBatchAfterPermission(batch: SettlementBatchRecord): void {
    const calls = batch.callIds.map((callId) => this.requireCall(callId));
    if (calls.some((call) => call.permissionEffect === "unknown")) return;
    if (batch.state !== "planned" && batch.state !== "permission_evaluated") return;
    const from = batch.state;
    batch.state = "permission_evaluated";
    batch.revision += 1;
    batch.digest = batchDigest(batch);
    if (from !== batch.state) {
      this.transition(batch, null, "batch.permission_evaluated", from, batch.state, {
        allowed_call_ids: calls.filter((call) => call.permissionEffect === "allow").map((call) => call.callId),
        blocked_call_ids: calls.filter((call) => call.permissionEffect !== "allow").map((call) => call.callId),
      });
    }
  }

  private effectKey(source: PlannedSettlementCall): string {
    const metadata = normalizeMetadata(source.metadata);
    const logicalCoordinates = {
      turnIndex: metadata.tool_transaction_turn_index ?? null,
      stepIndex: metadata.tool_transaction_step_index ?? null,
      batchIndex: metadata.tool_transaction_batch_index ?? source.position,
    };
    const hasStableCoordinates = typeof logicalCoordinates.turnIndex === "number"
      && typeof logicalCoordinates.stepIndex === "number";
    return `tool-effect:${hash({
      sessionId: this.identity.sessionId,
      logicalCoordinates: hasStableCoordinates ? logicalCoordinates : null,
      legacyCallId: hasStableCoordinates ? null : source.callId,
      toolName: source.toolName,
      argumentsDigest: hash(source.arguments),
    })}`;
  }

  private refreshBatchSettlement(batch: SettlementBatchRecord): void {
    if (terminalBatch(batch.state)) return;
    const calls = batch.callIds.map((callId) => this.requireCall(callId));
    const settled = calls.filter((call) => terminalCall(call.state));
    batch.settledCallIds = settled.map((call) => call.callId);
    const from = batch.state;
    batch.state = settled.length === calls.length ? "settling" : "settling";
    batch.revision += 1;
    batch.digest = batchDigest(batch);
    if (from !== batch.state) {
      this.transition(batch, null, "batch.settling", from, batch.state, {
        settled_call_ids: batch.settledCallIds,
        pending_call_ids: calls.filter((call) => !terminalCall(call.state)).map((call) => call.callId),
      });
    }
  }

  private addSettled(batch: SettlementBatchRecord, callId: string): void {
    if (!batch.settledCallIds.includes(callId)) batch.settledCallIds.push(callId);
    batch.revision += 1;
    batch.digest = batchDigest(batch);
  }

  private reconcileEquivalentOpenCalls(source: SettlementCallRecord): void {
    if (!terminalCall(source.state)) return;
    const affectedBatches = new Set<string>();
    for (const call of this.calls.values()) {
      if (
        call.callId === source.callId
        || call.idempotencyKey !== source.idempotencyKey
        || terminalCall(call.state)
        || !call.delegated
      ) {
        continue;
      }
      const batch = this.requireBatch(call.batchId);
      const from = call.state;
      call.state = source.state;
      call.transactionState = source.transactionState;
      call.gatewayAccepted = source.gatewayAccepted;
      call.summary = source.summary;
      call.output = clone(source.output);
      call.outputDigest = source.outputDigest;
      call.error = source.error;
      call.metadata = {
        ...call.metadata,
        ...clone(source.metadata),
        equivalent_effect_reconciled_by_call_id: source.callId,
      };
      call.dispatchedAt ??= source.dispatchedAt ?? source.gatewaySettledAt ?? now();
      call.gatewaySettledAt = source.gatewaySettledAt ?? now();
      call.resultAcknowledgedAt = source.resultAcknowledgedAt ?? call.gatewaySettledAt;
      call.completedAt = source.completedAt ?? now();
      if (source.state === "outcome_unknown") {
        call.failureHistory.push({
          stage: "result_acknowledgement",
          error: source.error ?? "tool_outcome_unknown",
          recoverable: false,
          dispatch_credential: call.dispatchCredential,
          equivalent_effect_call_id: source.callId,
        });
      }
      call.revision += 1;
      this.addSettled(batch, call.callId);
      this.transition(batch, call, "call.equivalent_effect_reconciled", from, call.state, {
        source_call_id: source.callId,
        idempotency_key: source.idempotencyKey,
        transaction_state: source.transactionState,
      });
      this.refreshBatchSettlement(batch);
      affectedBatches.add(batch.batchId);
    }
    for (const batchId of affectedBatches) {
      const batch = this.requireBatch(batchId);
      if (
        !terminalBatch(batch.state)
        && batch.callIds.every((callId) => terminalCall(this.requireCall(callId).state))
      ) {
        this.completeBatch(batchId);
      }
    }
  }

  private transition(
    batch: SettlementBatchRecord,
    call: SettlementCallRecord | null,
    operation: string,
    from: string,
    to: string,
    payload: JsonObject,
  ): void {
    const transition: SettlementTransition = {
      transitionId: `settlement:${this.identity.runtimeId}:${this.restartEpoch}:${this.sequence + 1}:${randomUUID()}`,
      sequence: ++this.sequence,
      restartEpoch: this.restartEpoch,
      batchId: batch.batchId,
      callId: call?.callId ?? null,
      operation,
      from,
      to,
      payload: clone(payload),
      payloadDigest: hash(payload),
      observedAt: now(),
    };
    this.transitions.push(transition);
  }

  private requireBatch(batchId: string): SettlementBatchRecord {
    const normalized = required(batchId, "settlement batch id");
    const batch = this.batches.get(normalized);
    if (!batch) {
      throw new ExecutionSettlementError("settlement_unknown_batch", `unknown settlement batch: ${normalized}`, {
        batch_id: normalized,
      });
    }
    return batch;
  }

  private requireCall(callId: string): SettlementCallRecord {
    const normalized = required(callId, "settlement call id");
    const call = this.calls.get(normalized);
    if (!call) {
      throw new ExecutionSettlementError("settlement_unknown_call", `unknown settlement call: ${normalized}`, {
        call_id: normalized,
      });
    }
    return call;
  }

  private planDigest(call: PlannedSettlementCall): JsonObject {
    return {
      call_id: call.callId,
      tool_name: call.toolName,
      arguments_digest: hash(call.arguments),
      local_capability: call.localCapability,
      execution_owner: call.executionOwner,
      position: call.position,
    };
  }

  private callPlanDigest(call: SettlementCallRecord): JsonObject {
    return {
      call_id: call.callId,
      tool_name: call.toolName,
      arguments_digest: call.argumentsDigest,
      local_capability: call.localCapability,
      execution_owner: call.executionOwner,
      position: call.position,
    };
  }

  private assertSecretFree(value: unknown): void {
    const found = credentialMaterial(value);
    if (found) {
      throw new ExecutionSettlementError(
        "settlement_snapshot_secret",
        `execution settlement snapshot contains forbidden credential material: ${found}`,
      );
    }
  }
}
