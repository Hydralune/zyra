import type { JsonObject, JsonRpcMessage, JsonValue } from "../contracts.ts";
import {
  canonicalJson,
  cloneJson,
  deterministicMcpId,
  hashChain,
  monotonicNow,
  sha256,
  verifyHashChain,
} from "../core/canonical.ts";
import { McpRuntimeError, type McpFailureRecord } from "../core/failure.ts";

export type McpRequestJournalStatus =
  | "prepared"
  | "sent"
  | "effect_observed"
  | "committed"
  | "failed"
  | "cancelled"
  | "indeterminate";

export interface McpRequestIdentity {
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
  method: string;
}

export interface McpPreparedRequest {
  journalId: string;
  transitionId: string;
  sequence: number;
  identity: McpRequestIdentity;
  status: McpRequestJournalStatus;
  message: JsonRpcMessage;
  messageDigest: string;
  argumentsDigest: string;
  idempotent: boolean;
  idempotencyKey: string | null;
  attempt: number;
  preparedAt: string;
  sentAt: string | null;
  effectObservedAt: string | null;
  committedAt: string | null;
  failure: McpFailureRecord | null;
  response: JsonRpcMessage | null;
  responseDigest: string | null;
  effectReceiptIds: string[];
  metadata: JsonObject;
  previousHash: string;
  recordHash: string;
}

export interface McpEffectReceipt {
  receiptId: string;
  journalId: string;
  transitionId: string;
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  requestId: string;
  method: string;
  effectKind: string;
  effectKey: string;
  effectDigest: string;
  payload: JsonValue;
  reversible: boolean;
  observedAt: string;
  metadata: JsonObject;
}

export interface McpRequestCommit {
  journalId: string;
  transitionId: string;
  status: "committed" | "failed" | "cancelled";
  response: JsonRpcMessage | null;
  failure: McpFailureRecord | null;
  committedAt?: string;
  metadata?: JsonObject;
}

export interface McpRequestJournalSnapshot {
  version: "zyra.mcp-request-journal/v1";
  revision: number;
  sequence: number;
  headHash: string;
  records: McpPreparedRequest[];
  effects: McpEffectReceipt[];
  idempotencyIndex: Record<string, string>;
  transitionIndex: Record<string, string>;
  digest: string;
  capturedAt: string;
}

export interface McpRestoreClassification {
  journalId: string;
  transitionId: string;
  priorStatus: McpRequestJournalStatus;
  restoredStatus: McpRequestJournalStatus;
  replayAllowed: boolean;
  effectKnown: boolean;
  requiresReconciliation: boolean;
  reason: string;
}

const journalGenesis = "sha256:zyra-mcp-request-journal-genesis";

export class McpRequestJournal {
  private readonly records = new Map<string, McpPreparedRequest>();
  private readonly effects = new Map<string, McpEffectReceipt>();
  private readonly idempotencyIndex = new Map<string, string>();
  private readonly transitionIndex = new Map<string, string>();
  private readonly now: () => Date;
  private readonly maximumRecords: number;
  private revision = 0;
  private sequence = 0;
  private headHash = journalGenesis;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumRecords?: number; snapshot?: McpRequestJournalSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumRecords = options.maximumRecords ?? 100_000;
    if (options.snapshot) this.restore(options.snapshot);
  }

  prepare(input: {
    identity: McpRequestIdentity;
    message: JsonRpcMessage;
    arguments: JsonObject;
    idempotent: boolean;
    idempotencyKey: string | null;
    metadata?: JsonObject;
  }): McpPreparedRequest {
    validateIdentity(input.identity);
    const messageDigest = sha256(input.message);
    const argumentsDigest = sha256(input.arguments);
    const transitionId = deterministicMcpId("mcp-request-transition", {
      run_id: input.identity.runId,
      task_id: input.identity.taskId,
      session_id: input.identity.sessionId,
      session_revision: input.identity.sessionRevision,
      worker_request_id: input.identity.workerRequestId,
      tool_call_id: input.identity.toolCallId,
      server_id: input.identity.serverId,
      connection_id: input.identity.connectionId,
      connection_epoch: input.identity.connectionEpoch,
      request_id: input.identity.requestId,
      method: input.identity.method,
      message_digest: messageDigest,
    }, 40);
    const existingTransition = this.transitionIndex.get(transitionId);
    if (existingTransition) {
      const record = this.records.get(existingTransition)!;
      if (record.messageDigest !== messageDigest || record.argumentsDigest !== argumentsDigest) {
        throw journalError(input.identity.serverId, "transition_payload_conflict", `transition ${transitionId} is bound to another payload`);
      }
      return cloneJson(record);
    }
    if (input.idempotencyKey) {
      const existingId = this.idempotencyIndex.get(input.idempotencyKey);
      if (existingId) {
        const record = this.records.get(existingId)!;
        if (record.messageDigest !== messageDigest) throw journalError(input.identity.serverId, "idempotency_payload_conflict", `idempotency key ${input.idempotencyKey} is bound to another message`);
        return cloneJson(record);
      }
    }
    this.sequence += 1;
    const journalId = deterministicMcpId("mcp-request-journal", {
      transition_id: transitionId,
      sequence: this.sequence,
    }, 40);
    const base = {
      journalId,
      transitionId,
      sequence: this.sequence,
      identity: cloneJson(input.identity),
      status: "prepared" as const,
      message: cloneJson(input.message),
      messageDigest,
      argumentsDigest,
      idempotent: input.idempotent,
      idempotencyKey: input.idempotencyKey,
      attempt: 1,
      preparedAt: this.timestamp(),
      sentAt: null,
      effectObservedAt: null,
      committedAt: null,
      failure: null,
      response: null,
      responseDigest: null,
      effectReceiptIds: [],
      metadata: cloneJson(input.metadata ?? {}),
      previousHash: this.headHash,
    };
    const record: McpPreparedRequest = {
      ...base,
      recordHash: hashChain(this.headHash, recordPayload(base)),
    };
    this.headHash = record.recordHash;
    this.records.set(journalId, record);
    this.transitionIndex.set(transitionId, journalId);
    if (input.idempotencyKey) this.idempotencyIndex.set(input.idempotencyKey, journalId);
    this.revision += 1;
    this.trim();
    return cloneJson(record);
  }

  markSent(journalId: string, transitionId: string, connectionEpoch: number): McpPreparedRequest {
    const record = this.requireBound(journalId, transitionId);
    if (record.identity.connectionEpoch !== connectionEpoch) {
      throw journalError(record.identity.serverId, "send_epoch_mismatch", `request ${journalId} belongs to epoch ${record.identity.connectionEpoch}, not ${connectionEpoch}`);
    }
    if (record.status !== "prepared" && record.status !== "sent") {
      throw journalError(record.identity.serverId, "request_not_sendable", `request ${journalId} is ${record.status}`);
    }
    if (record.status === "prepared") {
      this.rewrite(record, {
        status: "sent",
        sentAt: this.timestamp(),
      });
    }
    return cloneJson(record);
  }

  recordEffect(input: {
    journalId: string;
    transitionId: string;
    effectKind: string;
    effectKey: string;
    payload: JsonValue;
    reversible: boolean;
    metadata?: JsonObject;
  }): McpEffectReceipt {
    const record = this.requireBound(input.journalId, input.transitionId);
    const effectDigest = sha256(input.payload);
    const receiptId = deterministicMcpId("mcp-effect-receipt", {
      transition_id: record.transitionId,
      effect_kind: input.effectKind,
      effect_key: input.effectKey,
      effect_digest: effectDigest,
    }, 40);
    const existing = this.effects.get(receiptId);
    if (existing) return cloneJson(existing);
    if (record.status === "committed" || record.status === "failed" || record.status === "cancelled") {
      throw journalError(record.identity.serverId, "effect_after_terminal_request", `cannot record a new effect for ${record.status} request`);
    }
    const receipt: McpEffectReceipt = {
      receiptId,
      journalId: record.journalId,
      transitionId: record.transitionId,
      serverId: record.identity.serverId,
      connectionId: record.identity.connectionId,
      connectionEpoch: record.identity.connectionEpoch,
      requestId: record.identity.requestId,
      method: record.identity.method,
      effectKind: input.effectKind,
      effectKey: input.effectKey,
      effectDigest,
      payload: canonicalJson(input.payload),
      reversible: input.reversible,
      observedAt: this.timestamp(),
      metadata: cloneJson(input.metadata ?? {}),
    };
    this.effects.set(receiptId, receipt);
    this.rewrite(record, {
      status: "effect_observed",
      effectObservedAt: receipt.observedAt,
      effectReceiptIds: [...record.effectReceiptIds, receiptId],
    });
    return cloneJson(receipt);
  }

  commit(input: McpRequestCommit): McpPreparedRequest {
    const record = this.requireBound(input.journalId, input.transitionId);
    if (record.status === "committed" || record.status === "failed" || record.status === "cancelled") {
      if (record.status !== input.status) throw journalError(record.identity.serverId, "terminal_status_conflict", `request ${record.journalId} is already ${record.status}`);
      const responseDigest = input.response ? sha256(input.response) : null;
      if (responseDigest !== record.responseDigest) throw journalError(record.identity.serverId, "terminal_response_conflict", `request ${record.journalId} response differs from committed value`);
      return cloneJson(record);
    }
    if (input.status === "committed" && !input.response) throw journalError(record.identity.serverId, "committed_response_missing", "committed MCP request requires a response");
    if (input.status !== "committed" && !input.failure) throw journalError(record.identity.serverId, "terminal_failure_missing", `${input.status} MCP request requires a failure record`);
    const committedAt = input.committedAt ?? this.timestamp();
    this.rewrite(record, {
      status: input.status,
      response: input.response ? cloneJson(input.response) : null,
      responseDigest: input.response ? sha256(input.response) : null,
      failure: input.failure ? cloneJson(input.failure) : null,
      committedAt,
      metadata: { ...record.metadata, ...(input.metadata ?? {}) },
    });
    return cloneJson(record);
  }

  classifyRestore(): McpRestoreClassification[] {
    return [...this.records.values()]
      .filter((record) => record.status === "prepared" || record.status === "sent" || record.status === "effect_observed" || record.status === "indeterminate")
      .map((record) => {
        const effectKnown = record.effectReceiptIds.length > 0;
        const replayAllowed = record.idempotent && !effectKnown;
        const requiresReconciliation = !replayAllowed;
        let restoredStatus: McpRequestJournalStatus = record.status;
        let reason: string;
        if (record.status === "prepared") {
          restoredStatus = "prepared";
          reason = "request was prepared but never sent";
        } else if (replayAllowed) {
          restoredStatus = "prepared";
          reason = "idempotent request has no effect receipt and may replay";
        } else {
          restoredStatus = "indeterminate";
          reason = effectKnown
            ? "request has effect receipts and requires response reconciliation"
            : "non-idempotent sent request has unknown effect outcome";
        }
        return {
          journalId: record.journalId,
          transitionId: record.transitionId,
          priorStatus: record.status,
          restoredStatus,
          replayAllowed,
          effectKnown,
          requiresReconciliation,
          reason,
        };
      });
  }

  get(journalId: string): McpPreparedRequest | null {
    const value = this.records.get(journalId);
    return value ? cloneJson(value) : null;
  }

  findByTransition(transitionId: string): McpPreparedRequest | null {
    const journalId = this.transitionIndex.get(transitionId);
    return journalId ? this.get(journalId) : null;
  }

  findByIdempotencyKey(key: string): McpPreparedRequest | null {
    const journalId = this.idempotencyIndex.get(key);
    return journalId ? this.get(journalId) : null;
  }

  effectsFor(journalId: string): McpEffectReceipt[] {
    const record = this.records.get(journalId);
    if (!record) return [];
    return record.effectReceiptIds.map((id) => cloneJson(this.effects.get(id)!));
  }

  snapshot(): McpRequestJournalSnapshot {
    const records = [...this.records.values()]
      .sort((left, right) => left.sequence - right.sequence)
      .map(cloneJson);
    let snapshotHead = journalGenesis;
    for (const record of records) {
      record.previousHash = snapshotHead;
      record.recordHash = hashChain(snapshotHead, recordPayload(record));
      snapshotHead = record.recordHash;
    }
    const withoutDigest = {
      version: "zyra.mcp-request-journal/v1" as const,
      revision: this.revision,
      sequence: this.sequence,
      headHash: snapshotHead,
      records,
      effects: [...this.effects.values()].sort((left, right) => left.receiptId.localeCompare(right.receiptId)).map(cloneJson),
      idempotencyIndex: Object.fromEntries([...this.idempotencyIndex.entries()].sort(([left], [right]) => left.localeCompare(right))),
      transitionIndex: Object.fromEntries([...this.transitionIndex.entries()].sort(([left], [right]) => left.localeCompare(right))),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  restore(snapshot: McpRequestJournalSnapshot): void {
    if (snapshot.version !== "zyra.mcp-request-journal/v1") throw journalError("", "unsupported_journal_snapshot", "unsupported MCP request journal snapshot version");
    const { digest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== digest) throw journalError("", "journal_snapshot_digest_mismatch", "MCP request journal snapshot digest mismatch");
    const ordered = [...snapshot.records].sort((left, right) => left.sequence - right.sequence);
    const head = verifyHashChain(ordered, journalGenesis, (record) => record.previousHash, (record) => record.recordHash, recordPayload);
    if (head !== snapshot.headHash) throw journalError("", "journal_head_mismatch", "MCP request journal head hash mismatch");
    this.records.clear();
    this.effects.clear();
    this.idempotencyIndex.clear();
    this.transitionIndex.clear();
    this.revision = snapshot.revision;
    this.sequence = snapshot.sequence;
    this.headHash = snapshot.headHash;
    for (const record of ordered) this.records.set(record.journalId, cloneJson(record));
    for (const effect of snapshot.effects) this.effects.set(effect.receiptId, cloneJson(effect));
    for (const [key, journalId] of Object.entries(snapshot.idempotencyIndex)) this.idempotencyIndex.set(key, journalId);
    for (const [key, journalId] of Object.entries(snapshot.transitionIndex)) this.transitionIndex.set(key, journalId);
    for (const classification of this.classifyRestore()) {
      const record = this.records.get(classification.journalId)!;
      record.status = classification.restoredStatus;
      record.metadata = {
        ...record.metadata,
        restore_classification: classification.reason,
        replay_allowed: classification.replayAllowed,
        requires_reconciliation: classification.requiresReconciliation,
      };
    }
  }

  private requireBound(journalId: string, transitionId: string): McpPreparedRequest {
    const record = this.records.get(journalId);
    if (!record) throw journalError("", "journal_record_not_found", `MCP request journal record ${journalId} was not found`);
    if (record.transitionId !== transitionId) throw journalError(record.identity.serverId, "transition_binding_mismatch", `transition ${transitionId} does not own journal record ${journalId}`);
    return record;
  }

  private rewrite(record: McpPreparedRequest, patch: Partial<McpPreparedRequest>): void {
    const next = { ...record, ...cloneJson(patch) };
    const previousHash = this.headHash;
    next.previousHash = previousHash;
    next.recordHash = hashChain(previousHash, recordPayload(next));
    this.headHash = next.recordHash;
    this.records.set(record.journalId, next);
    Object.assign(record, next);
    this.revision += 1;
  }

  private trim(): void {
    if (this.records.size <= this.maximumRecords) return;
    for (const record of [...this.records.values()].sort((left, right) => left.sequence - right.sequence)) {
      if (this.records.size <= this.maximumRecords) break;
      if (record.status === "prepared" || record.status === "sent" || record.status === "effect_observed" || record.status === "indeterminate") continue;
      this.records.delete(record.journalId);
      this.transitionIndex.delete(record.transitionId);
      if (record.idempotencyKey) this.idempotencyIndex.delete(record.idempotencyKey);
      for (const effectId of record.effectReceiptIds) this.effects.delete(effectId);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function validateIdentity(identity: McpRequestIdentity): void {
  for (const [key, value] of Object.entries(identity)) {
    if (key === "sessionRevision" || key === "connectionEpoch") {
      if (!Number.isSafeInteger(value) || (value as number) < 0) throw journalError(identity.serverId, "invalid_request_identity", `${key} must be a non-negative integer`);
    } else if (typeof value !== "string" || !value) {
      throw journalError(identity.serverId, "invalid_request_identity", `${key} is required`);
    }
  }
}

function recordPayload(record: Omit<McpPreparedRequest, "recordHash"> | McpPreparedRequest): JsonObject {
  const { recordHash: _ignored, ...payload } = record as McpPreparedRequest;
  return canonicalJson(payload) as JsonObject;
}

function journalError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-request-journal", { server_id: serverId, code, message }),
    category: code.includes("conflict") || code.includes("binding") ? "conflict" : "restore",
    code,
    message,
    serverId,
    retryable: code.includes("conflict"),
    disposition: code.includes("conflict") ? "retry_same_connection" : "terminal",
  });
}
