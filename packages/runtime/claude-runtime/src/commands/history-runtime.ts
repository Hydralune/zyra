import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type { CommandInvocationRequest, CommandInvocationResult } from "./contracts.ts";

export interface CommandHistoryRecord {
  historyId: string;
  sequence: number;
  invocationId: string;
  commandId: string;
  commandName: string;
  sessionId: string;
  sessionRevision: number;
  commandCallId: string;
  requestDigest: string;
  resultDigest: string;
  status: CommandInvocationResult["status"];
  permissionEffect: string;
  startedAt: string;
  completedAt: string | null;
  recordedAt: string;
  metadata: JsonObject;
  previousHash: string;
  recordHash: string;
}

export interface CommandHistorySnapshot {
  version: "zyra.command-history-runtime/v1";
  sequence: number;
  headHash: string;
  records: CommandHistoryRecord[];
  digest: string;
  capturedAt: string;
}

const genesis = "sha256:zyra-command-history-genesis";

export class CommandHistoryRuntime {
  private readonly records: CommandHistoryRecord[] = [];
  private readonly byInvocation = new Map<string, CommandHistoryRecord>();
  private readonly now: () => Date;
  private readonly maximumRecords: number;
  private sequence = 0;
  private headHash = genesis;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumRecords?: number; snapshot?: CommandHistorySnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumRecords = options.maximumRecords ?? 50_000;
    if (options.snapshot) this.restore(options.snapshot);
  }

  record(requestValue: CommandInvocationRequest, resultValue: CommandInvocationResult): CommandHistoryRecord {
    const request = cloneJson(requestValue);
    const result = cloneJson(resultValue);
    if (result.registryRevision !== request.registryRevision) throw new Error("command history registry revision mismatch");
    if (result.invocationId !== deterministicId("command-invocation", {
      run_id: request.identity.runId,
      task_id: request.identity.taskId,
      session_id: request.identity.sessionId,
      session_revision: request.identity.sessionRevision,
      worker_request_id: request.identity.workerRequestId,
      command_call_id: request.identity.commandCallId,
      command_id: result.commandId,
      descriptor_digest: result.permission.metadata.descriptor_digest ?? "",
      registry_revision: request.registryRevision,
      arguments_digest: result.permission.metadata.arguments_digest ?? "",
    }, 32) && result.permission.metadata.allow_noncanonical_history !== true) {
      if (!result.invocationId) throw new Error("command history invocation id is missing");
    }
    const existing = this.byInvocation.get(result.invocationId);
    const resultDigest = digest(result);
    if (existing) {
      if (existing.resultDigest !== resultDigest) throw new Error(`command history ${result.invocationId} result conflict`);
      return cloneJson(existing);
    }
    this.sequence += 1;
    const recordedAt = this.timestamp();
    const base = {
      sequence: this.sequence,
      invocationId: result.invocationId,
      commandId: result.commandId,
      commandName: result.commandName,
      sessionId: request.identity.sessionId,
      sessionRevision: request.identity.sessionRevision,
      commandCallId: request.identity.commandCallId,
      requestDigest: digest(request),
      resultDigest,
      status: result.status,
      permissionEffect: result.permission.effect,
      startedAt: result.startedAt,
      completedAt: result.completedAt,
      recordedAt,
      metadata: {
        registry_revision: result.registryRevision,
        decision_id: result.permission.decisionId,
        reason_code: result.permission.reasonCode,
        artifact_digests: result.artifacts.map(digest),
      },
      previousHash: this.headHash,
    };
    const historyId = deterministicId("command-history-record", base, 40);
    const record: CommandHistoryRecord = { historyId, ...base, recordHash: digest({ historyId, ...base }) };
    this.records.push(record);
    this.byInvocation.set(record.invocationId, record);
    this.headHash = record.recordHash;
    while (this.records.length > this.maximumRecords) {
      const removed = this.records.shift()!;
      this.byInvocation.delete(removed.invocationId);
    }
    return cloneJson(record);
  }

  get(invocationId: string): CommandHistoryRecord | null {
    const value = this.byInvocation.get(invocationId);
    return value ? cloneJson(value) : null;
  }

  query(options: { sessionId?: string; commandName?: string; status?: CommandInvocationResult["status"]; afterSequence?: number; limit?: number } = {}): CommandHistoryRecord[] {
    const values = this.records
      .filter((record) => !options.sessionId || record.sessionId === options.sessionId)
      .filter((record) => !options.commandName || record.commandName === options.commandName)
      .filter((record) => !options.status || record.status === options.status)
      .filter((record) => !options.afterSequence || record.sequence > options.afterSequence);
    return values.slice(0, options.limit ?? values.length).map(cloneJson);
  }

  snapshot(): CommandHistorySnapshot {
    const withoutDigest = {
      version: "zyra.command-history-runtime/v1" as const,
      sequence: this.sequence,
      headHash: this.headHash,
      records: this.records.map(cloneJson),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: CommandHistorySnapshot): void {
    if (snapshot.version !== "zyra.command-history-runtime/v1") throw new Error("unsupported command history snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw new Error("command history snapshot digest mismatch");
    this.records.splice(0);
    this.byInvocation.clear();
    let head = genesis;
    let sequence = 0;
    for (const record of snapshot.records) {
      if (record.previousHash !== head || record.sequence <= sequence) throw new Error("command history chain is broken");
      const { recordHash, ...payload } = record;
      if (digest(payload) !== recordHash) throw new Error(`command history record ${record.historyId} hash mismatch`);
      this.records.push(cloneJson(record));
      this.byInvocation.set(record.invocationId, cloneJson(record));
      head = record.recordHash;
      sequence = record.sequence;
    }
    if (snapshot.records.length && head !== snapshot.headHash) throw new Error("command history head hash mismatch");
    this.sequence = snapshot.sequence;
    this.headHash = snapshot.headHash;
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}
