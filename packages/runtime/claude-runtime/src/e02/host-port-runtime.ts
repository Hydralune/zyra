import type { JsonObject, JsonValue } from "../contracts.ts";
import {
  canonicalize,
  cloneJson,
  constantTimeDigestEquals,
  deterministicId,
  digest,
  hashChain,
} from "./canonical.ts";
import type { E02RuntimeIdentity } from "./contracts.ts";

export type E02HostPortKind =
  | "event-delivery"
  | "checkpoint-delivery"
  | "approval-delivery"
  | "credential-read"
  | "credential-write"
  | "credential-delete"
  | "artifact-write"
  | "physical-tool";

export type E02HostPortPhase =
  | "prepared"
  | "sent"
  | "receipt-recorded"
  | "committed"
  | "failed"
  | "indeterminate"
  | "cancelled";

export interface E02HostPortBinding extends JsonObject {
  runId: string;
  taskId: string;
  sessionId: string;
  workerRequestId: string;
  correlationId: string;
  entityId: string;
  expectedRevision: number;
}

export interface E02HostPortPrepareInput {
  kind: E02HostPortKind;
  operation: string;
  binding: E02HostPortBinding;
  payload: JsonObject;
  idempotencyKey: string;
  nonIdempotent: boolean;
  replayPolicy: "return-committed" | "resume-pending" | "reject-duplicate";
  metadata?: JsonObject;
}

export interface E02HostPortReceipt extends JsonObject {
  receiptId: string;
  portRequestId: string;
  correlationId: string;
  requestDigest: string;
  responseDigest: string;
  ok: boolean;
  output: JsonObject;
  errorCode: string | null;
  errorMessage: string | null;
  providerReceiptId: string | null;
  receivedAt: string;
  runtimeEpoch: number;
  metadata: JsonObject;
  receiptDigest: string;
}

export interface E02HostPortCommit extends JsonObject {
  commitId: string;
  portRequestId: string;
  entityId: string;
  revisionBefore: number;
  revisionAfter: number;
  requestDigest: string;
  receiptDigest: string;
  outputDigest: string;
  committedAt: string;
  runtimeEpoch: number;
  previousCommitHash: string;
  commitHash: string;
}

export interface E02HostPortRecord extends JsonObject {
  portRequestId: string;
  sequence: number;
  kind: E02HostPortKind;
  operation: string;
  phase: E02HostPortPhase;
  binding: E02HostPortBinding;
  payload: JsonObject;
  payloadDigest: string;
  idempotencyKey: string;
  nonIdempotent: boolean;
  replayPolicy: E02HostPortPrepareInput["replayPolicy"];
  revisionBefore: number;
  revisionAfter: number | null;
  sentAt: string | null;
  sendAttempt: number;
  receipt: E02HostPortReceipt | null;
  commit: E02HostPortCommit | null;
  failureCode: string | null;
  failureMessage: string | null;
  recovery: JsonObject | null;
  preparedAt: string;
  updatedAt: string;
  runtimeEpochPrepared: number;
  runtimeEpochCompleted: number | null;
  metadata: JsonObject;
  recordDigest: string;
}

export interface E02HostPortEntity extends JsonObject {
  entityId: string;
  revision: number;
  lastPortRequestId: string | null;
  lastCommitHash: string;
  updatedAt: string;
}

export interface E02HostPortSnapshot {
  version: "zyra.e02-host-port/v1";
  runtime: E02RuntimeIdentity;
  sequence: number;
  previousCommitHash: string;
  records: E02HostPortRecord[];
  entities: E02HostPortEntity[];
  idempotency: Array<[string, string]>;
  snapshotHash: string;
}

export interface E02HostPortPrepareResult {
  disposition: "prepared" | "return-committed" | "resume-pending";
  record: E02HostPortRecord;
}

export interface E02HostPortEffectResult {
  ok: boolean;
  output: JsonObject;
  errorCode?: string | null;
  errorMessage?: string | null;
  providerReceiptId?: string | null;
  metadata?: JsonObject;
}

export interface E02HostPortTransactionResult {
  record: E02HostPortRecord;
  output: JsonObject;
  replayed: boolean;
}

export class E02HostPortRuntime {
  readonly runtime: E02RuntimeIdentity;
  private readonly now: () => Date;
  private sequence = 0;
  private previousCommitHash = digest({ genesis: "zyra.e02-host-port/v1" });
  private readonly records = new Map<string, E02HostPortRecord>();
  private readonly idempotency = new Map<string, string>();
  private readonly entities = new Map<string, E02HostPortEntity>();

  constructor(options: {
    runtime: E02RuntimeIdentity;
    now?: () => Date;
    snapshot?: E02HostPortSnapshot | null;
  }) {
    this.runtime = cloneJson(options.runtime);
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  prepare(inputValue: E02HostPortPrepareInput): E02HostPortPrepareResult {
    const input = normalizePrepare(inputValue);
    this.assertBinding(input.binding);
    const existingId = this.idempotency.get(input.idempotencyKey);
    if (existingId) {
      const existing = this.requireRecord(existingId);
      this.assertReplayIdentity(existing, input);
      if (existing.phase === "committed") {
        if (input.replayPolicy === "reject-duplicate") {
          throw hostPortError(
            "e02_host_port_duplicate_rejected",
            `host port request ${existingId} was already committed`,
          );
        }
        return { disposition: "return-committed", record: cloneJson(existing) };
      }
      if (input.replayPolicy !== "resume-pending") {
        throw hostPortError(
          "e02_host_port_pending_duplicate",
          `host port request ${existingId} is ${existing.phase}`,
        );
      }
      if (existing.phase === "indeterminate") {
        throw hostPortError(
          "e02_host_port_indeterminate",
          `host port request ${existingId} requires reconciliation`,
          { recovery: cloneJson(existing.recovery) },
        );
      }
      return { disposition: "resume-pending", record: cloneJson(existing) };
    }
    const entity = this.entity(input.binding.entityId);
    if (input.binding.expectedRevision !== entity.revision) {
      throw hostPortError(
        "e02_host_port_revision_conflict",
        `host port entity ${entity.entityId} expected revision ${input.binding.expectedRevision}, current ${entity.revision}`,
      );
    }
    const preparedAt = this.timestamp();
    const payloadDigest = digest(input.payload);
    const sequence = this.sequence + 1;
    const portRequestId = deterministicId("e02-host-port-request", {
      runtime_id: this.runtime.runtimeId,
      runtime_epoch: this.runtime.epoch,
      sequence,
      kind: input.kind,
      operation: input.operation,
      binding: input.binding,
      payload_digest: payloadDigest,
      idempotency_key: input.idempotencyKey,
    }, 48);
    const base: Omit<E02HostPortRecord, "recordDigest"> = {
      portRequestId,
      sequence,
      kind: input.kind,
      operation: input.operation,
      phase: "prepared",
      binding: cloneJson(input.binding),
      payload: cloneJson(input.payload),
      payloadDigest,
      idempotencyKey: input.idempotencyKey,
      nonIdempotent: input.nonIdempotent,
      replayPolicy: input.replayPolicy,
      revisionBefore: entity.revision,
      revisionAfter: null,
      sentAt: null,
      sendAttempt: 0,
      receipt: null,
      commit: null,
      failureCode: null,
      failureMessage: null,
      recovery: null,
      preparedAt,
      updatedAt: preparedAt,
      runtimeEpochPrepared: this.runtime.epoch,
      runtimeEpochCompleted: null,
      metadata: cloneJson(input.metadata ?? {}),
    };
    const record = {
      ...base,
      recordDigest: recordDigest(base),
    } as E02HostPortRecord;
    this.sequence = sequence;
    this.records.set(portRequestId, record);
    this.idempotency.set(input.idempotencyKey, portRequestId);
    return { disposition: "prepared", record: cloneJson(record) };
  }

  markSent(portRequestId: string): E02HostPortRecord {
    const record = this.requireRecord(portRequestId);
    if (record.phase === "committed" || record.phase === "receipt-recorded") {
      return cloneJson(record);
    }
    if (record.phase !== "prepared" && record.phase !== "sent" && record.phase !== "failed") {
      throw hostPortError(
        "e02_host_port_send_phase_invalid",
        `cannot send host port request ${portRequestId} from ${record.phase}`,
      );
    }
    if (record.phase === "failed" && record.nonIdempotent) {
      throw hostPortError(
        "e02_host_port_non_idempotent_retry_forbidden",
        `cannot retry failed non-idempotent host port request ${portRequestId}`,
      );
    }
    record.phase = "sent";
    record.sentAt = this.timestamp();
    record.sendAttempt += 1;
    record.updatedAt = record.sentAt;
    this.refreshDigest(record);
    return cloneJson(record);
  }

  recordReceipt(
    portRequestId: string,
    receiptValue: E02HostPortEffectResult,
  ): E02HostPortRecord {
    const record = this.requireRecord(portRequestId);
    if (record.phase === "committed") return cloneJson(record);
    if (record.receipt) {
      const candidate = buildReceipt(record, receiptValue, this.runtime.epoch, record.receipt.receivedAt);
      if (!constantTimeDigestEquals(candidate.receiptDigest, record.receipt.receiptDigest)) {
        throw hostPortError(
          "e02_host_port_receipt_conflict",
          `host port request ${portRequestId} received a conflicting receipt`,
        );
      }
      return cloneJson(record);
    }
    if (record.phase !== "sent") {
      throw hostPortError(
        "e02_host_port_receipt_phase_invalid",
        `cannot record a receipt for ${portRequestId} from ${record.phase}`,
      );
    }
    const receipt = buildReceipt(record, receiptValue, this.runtime.epoch, this.timestamp());
    record.receipt = receipt;
    record.phase = "receipt-recorded";
    record.failureCode = receipt.ok ? null : receipt.errorCode ?? "host_port_effect_failed";
    record.failureMessage = receipt.ok ? null : receipt.errorMessage ?? "host port effect failed";
    record.updatedAt = receipt.receivedAt;
    this.refreshDigest(record);
    return cloneJson(record);
  }

  commit(portRequestId: string): E02HostPortRecord {
    const record = this.requireRecord(portRequestId);
    if (record.phase === "committed") return cloneJson(record);
    if (record.phase !== "receipt-recorded" || !record.receipt) {
      throw hostPortError(
        "e02_host_port_commit_phase_invalid",
        `host port request ${portRequestId} has no receipt to commit`,
      );
    }
    if (!record.receipt.ok) {
      throw hostPortError(
        "e02_host_port_failed_receipt",
        record.receipt.errorMessage ?? `host port request ${portRequestId} failed`,
      );
    }
    const entity = this.entity(record.binding.entityId);
    if (entity.revision !== record.revisionBefore) {
      throw hostPortError(
        "e02_host_port_commit_conflict",
        `host port entity ${entity.entityId} advanced before ${portRequestId} committed`,
      );
    }
    const committedAt = this.timestamp();
    const revisionAfter = entity.revision + 1;
    const commitBase = {
      portRequestId,
      entityId: entity.entityId,
      revisionBefore: entity.revision,
      revisionAfter,
      requestDigest: record.payloadDigest,
      receiptDigest: record.receipt.receiptDigest,
      outputDigest: record.receipt.responseDigest,
      committedAt,
      runtimeEpoch: this.runtime.epoch,
      previousCommitHash: this.previousCommitHash,
    };
    const commitId = deterministicId("e02-host-port-commit", commitBase, 48);
    const commitHash = hashChain(this.previousCommitHash, { commitId, ...commitBase });
    const commit: E02HostPortCommit = { commitId, ...commitBase, commitHash };
    record.commit = commit;
    record.phase = "committed";
    record.revisionAfter = revisionAfter;
    record.runtimeEpochCompleted = this.runtime.epoch;
    record.updatedAt = committedAt;
    record.failureCode = null;
    record.failureMessage = null;
    record.recovery = null;
    this.previousCommitHash = commitHash;
    this.entities.set(entity.entityId, {
      entityId: entity.entityId,
      revision: revisionAfter,
      lastPortRequestId: portRequestId,
      lastCommitHash: commitHash,
      updatedAt: committedAt,
    });
    this.refreshDigest(record);
    return cloneJson(record);
  }

  fail(portRequestId: string, errorValue: unknown): E02HostPortRecord {
    const record = this.requireRecord(portRequestId);
    if (record.phase === "committed") return cloneJson(record);
    const failure = failureObject(errorValue);
    record.failureCode = String(failure.code ?? "host_port_effect_failed");
    record.failureMessage = String(failure.message ?? "host port effect failed");
    record.updatedAt = this.timestamp();
    if (record.nonIdempotent && record.phase === "sent" && !record.receipt) {
      record.phase = "indeterminate";
      record.recovery = {
        kind: "host_port_effect_reconciliation",
        port_request_id: record.portRequestId,
        correlation_id: record.binding.correlationId,
        entity_id: record.binding.entityId,
        request_digest: record.payloadDigest,
        provider_receipt_required: true,
        reexecute_without_receipt: false,
        failure,
      };
    } else {
      record.phase = "failed";
      record.recovery = record.nonIdempotent
        ? {
          kind: "host_port_failure_before_effect",
          reexecute_without_receipt: true,
          failure,
        }
        : null;
    }
    this.refreshDigest(record);
    return cloneJson(record);
  }

  cancel(portRequestId: string, reason: string): E02HostPortRecord {
    const record = this.requireRecord(portRequestId);
    if (record.phase === "cancelled") return cloneJson(record);
    if (record.phase !== "prepared") {
      throw hostPortError(
        "e02_host_port_cancel_phase_invalid",
        `host port request ${portRequestId} cannot be cancelled from ${record.phase}`,
      );
    }
    record.phase = "cancelled";
    record.failureCode = "host_port_cancelled";
    record.failureMessage = reason || "host port request cancelled";
    record.updatedAt = this.timestamp();
    record.runtimeEpochCompleted = this.runtime.epoch;
    this.refreshDigest(record);
    return cloneJson(record);
  }

  reconcile(
    portRequestId: string,
    effect: E02HostPortEffectResult | null,
    reason: string,
  ): E02HostPortRecord {
    const record = this.requireRecord(portRequestId);
    if (record.phase !== "indeterminate") {
      throw hostPortError(
        "e02_host_port_reconcile_phase_invalid",
        `host port request ${portRequestId} is not indeterminate`,
      );
    }
    if (!reason.trim()) {
      throw hostPortError("e02_host_port_reconcile_reason_missing", "reconciliation reason is required");
    }
    if (!effect) {
      record.phase = "cancelled";
      record.failureCode = "host_port_effect_confirmed_absent";
      record.failureMessage = reason;
      record.recovery = null;
      record.runtimeEpochCompleted = this.runtime.epoch;
      record.updatedAt = this.timestamp();
      this.refreshDigest(record);
      return cloneJson(record);
    }
    record.phase = "sent";
    record.metadata = {
      ...record.metadata,
      reconciled: true,
      reconciliation_reason: reason,
      reconciliation_epoch: this.runtime.epoch,
    };
    this.refreshDigest(record);
    this.recordReceipt(portRequestId, effect);
    return effect.ok ? this.commit(portRequestId) : this.fail(portRequestId, effect.errorMessage);
  }

  async transact(
    input: E02HostPortPrepareInput,
    effect: (record: E02HostPortRecord) => Promise<E02HostPortEffectResult>,
  ): Promise<E02HostPortTransactionResult> {
    const prepared = this.prepare(input);
    if (prepared.disposition === "return-committed") {
      return {
        record: prepared.record,
        output: cloneJson(prepared.record.receipt?.output ?? {}),
        replayed: true,
      };
    }
    const current = this.requireRecord(prepared.record.portRequestId);
    if (current.phase === "receipt-recorded" && current.receipt?.ok) {
      const committed = this.commit(current.portRequestId);
      return { record: committed, output: cloneJson(committed.receipt?.output ?? {}), replayed: true };
    }
    if (current.phase === "sent" && current.nonIdempotent) {
      current.phase = "indeterminate";
      current.recovery = {
        kind: "restored_host_port_effect_without_receipt",
        reexecute_without_receipt: false,
      };
      current.updatedAt = this.timestamp();
      this.refreshDigest(current);
      throw hostPortError(
        "e02_host_port_indeterminate",
        `host port request ${current.portRequestId} was sent without a durable receipt`,
      );
    }
    this.markSent(current.portRequestId);
    try {
      const result = await effect(cloneJson(this.requireRecord(current.portRequestId)));
      this.recordReceipt(current.portRequestId, result);
      if (!result.ok) {
        this.fail(current.portRequestId, result.errorMessage ?? result.errorCode ?? "host port effect failed");
        throw hostPortError(
          result.errorCode ?? "e02_host_port_effect_failed",
          result.errorMessage ?? "host port effect failed",
        );
      }
      const committed = this.commit(current.portRequestId);
      return { record: committed, output: cloneJson(result.output), replayed: false };
    } catch (error) {
      const latest = this.requireRecord(current.portRequestId);
      if (latest.phase !== "committed" && latest.phase !== "failed" && latest.phase !== "indeterminate") {
        this.fail(current.portRequestId, error);
      }
      throw error;
    }
  }

  get(portRequestId: string): E02HostPortRecord | null {
    const record = this.records.get(portRequestId);
    return record ? cloneJson(record) : null;
  }

  byIdempotencyKey(idempotencyKey: string): E02HostPortRecord | null {
    const id = this.idempotency.get(idempotencyKey);
    return id ? this.get(id) : null;
  }

  list(filter: {
    kind?: E02HostPortKind;
    phase?: E02HostPortPhase;
    entityId?: string;
    limit?: number;
  } = {}): E02HostPortRecord[] {
    const limit = Math.max(1, Math.min(20_000, filter.limit ?? 20_000));
    return [...this.records.values()]
      .filter((record) => !filter.kind || record.kind === filter.kind)
      .filter((record) => !filter.phase || record.phase === filter.phase)
      .filter((record) => !filter.entityId || record.binding.entityId === filter.entityId)
      .sort((left, right) => left.sequence - right.sequence)
      .slice(-limit)
      .map(cloneJson);
  }

  pending(): E02HostPortRecord[] {
    return this.list().filter((record) => ["prepared", "sent", "receipt-recorded"].includes(record.phase));
  }

  indeterminate(): E02HostPortRecord[] {
    return this.list({ phase: "indeterminate" });
  }

  entityState(entityId: string): E02HostPortEntity {
    return cloneJson(this.entity(entityId));
  }

  health(): JsonObject {
    const records = this.list();
    const byPhase: JsonObject = {};
    const byKind: JsonObject = {};
    for (const record of records) {
      byPhase[record.phase] = Number(byPhase[record.phase] ?? 0) + 1;
      byKind[record.kind] = Number(byKind[record.kind] ?? 0) + 1;
    }
    return {
      canonical_owner: "typescript",
      runtime_epoch: this.runtime.epoch,
      record_count: records.length,
      entity_count: this.entities.size,
      pending_count: this.pending().length,
      indeterminate_count: this.indeterminate().length,
      previous_commit_hash: this.previousCommitHash,
      by_phase: byPhase,
      by_kind: byKind,
      python_decision_fallback: false,
    };
  }

  snapshot(): E02HostPortSnapshot {
    const withoutHash = {
      version: "zyra.e02-host-port/v1" as const,
      runtime: cloneJson(this.runtime),
      sequence: this.sequence,
      previousCommitHash: this.previousCommitHash,
      records: this.list(),
      entities: [...this.entities.values()]
        .sort((left, right) => left.entityId.localeCompare(right.entityId))
        .map(cloneJson),
      idempotency: [...this.idempotency.entries()]
        .sort(([left], [right]) => left.localeCompare(right)),
    };
    return { ...withoutHash, snapshotHash: digest(withoutHash) };
  }

  private restore(snapshotValue: E02HostPortSnapshot): void {
    const snapshot = cloneJson(snapshotValue);
    if (snapshot.version !== "zyra.e02-host-port/v1") {
      throw hostPortError("e02_host_port_snapshot_version", `unsupported host port snapshot ${snapshot.version}`);
    }
    const { snapshotHash, ...withoutHash } = snapshot;
    if (!constantTimeDigestEquals(digest(withoutHash), snapshotHash)) {
      throw hostPortError("e02_host_port_snapshot_digest", "host port snapshot digest mismatch");
    }
    if (
      snapshot.runtime.runtimeId !== this.runtime.runtimeId
      || snapshot.runtime.runId !== this.runtime.runId
      || snapshot.runtime.taskId !== this.runtime.taskId
      || snapshot.runtime.sessionId !== this.runtime.sessionId
      || snapshot.runtime.workerRequestId !== this.runtime.workerRequestId
    ) {
      throw hostPortError("e02_host_port_snapshot_binding", "host port snapshot binding mismatch");
    }
    if (this.runtime.epoch <= snapshot.runtime.epoch) {
      throw hostPortError("e02_host_port_snapshot_epoch", "host port restore epoch must advance");
    }
    let lastSequence = 0;
    for (const record of snapshot.records) {
      validateRecord(record);
      if (record.sequence <= lastSequence) {
        throw hostPortError("e02_host_port_snapshot_sequence", "host port record sequence is not monotonic");
      }
      lastSequence = record.sequence;
      if (this.records.has(record.portRequestId)) {
        throw hostPortError("e02_host_port_snapshot_duplicate", `duplicate host port record ${record.portRequestId}`);
      }
      const restored = cloneJson(record);
      if (restored.phase === "sent" && restored.nonIdempotent && !restored.receipt) {
        restored.phase = "indeterminate";
        restored.recovery = {
          kind: "restored_host_port_effect_without_receipt",
          source_epoch: snapshot.runtime.epoch,
          target_epoch: this.runtime.epoch,
          reexecute_without_receipt: false,
        };
        restored.updatedAt = this.timestamp();
        restored.recordDigest = recordDigest(restored);
      }
      this.records.set(restored.portRequestId, restored);
    }
    for (const [key, id] of snapshot.idempotency) {
      if (!this.records.has(id) || this.idempotency.has(key)) {
        throw hostPortError("e02_host_port_snapshot_idempotency", "invalid host port idempotency index");
      }
      this.idempotency.set(key, id);
    }
    for (const entity of snapshot.entities) {
      if (!entity.entityId || this.entities.has(entity.entityId) || entity.revision < 0) {
        throw hostPortError("e02_host_port_snapshot_entity", "invalid host port entity state");
      }
      this.entities.set(entity.entityId, cloneJson(entity));
    }
    this.sequence = snapshot.sequence;
    this.previousCommitHash = snapshot.previousCommitHash;
  }

  private entity(entityId: string): E02HostPortEntity {
    const id = entityId.trim();
    if (!id) throw hostPortError("e02_host_port_entity_missing", "host port entity id is required");
    const existing = this.entities.get(id);
    if (existing) return existing;
    const created: E02HostPortEntity = {
      entityId: id,
      revision: 0,
      lastPortRequestId: null,
      lastCommitHash: digest({ entity_id: id, genesis: true }),
      updatedAt: this.timestamp(),
    };
    this.entities.set(id, created);
    return created;
  }

  private requireRecord(portRequestId: string): E02HostPortRecord {
    const record = this.records.get(portRequestId);
    if (!record) throw hostPortError("e02_host_port_unknown", `unknown host port request ${portRequestId}`);
    return record;
  }

  private assertBinding(binding: E02HostPortBinding): void {
    if (
      binding.runId !== this.runtime.runId
      || binding.taskId !== this.runtime.taskId
      || binding.sessionId !== this.runtime.sessionId
      || binding.workerRequestId !== this.runtime.workerRequestId
    ) {
      throw hostPortError("e02_host_port_binding_mismatch", "host port request binding differs from runtime identity");
    }
    if (!binding.correlationId || !binding.entityId) {
      throw hostPortError("e02_host_port_binding_incomplete", "host port correlation and entity identities are required");
    }
    if (!Number.isSafeInteger(binding.expectedRevision) || binding.expectedRevision < 0) {
      throw hostPortError("e02_host_port_revision_invalid", "host port expected revision is invalid");
    }
  }

  private assertReplayIdentity(record: E02HostPortRecord, input: E02HostPortPrepareInput): void {
    const candidate = digest({
      kind: input.kind,
      operation: input.operation,
      binding: input.binding,
      payload: input.payload,
      non_idempotent: input.nonIdempotent,
    });
    const existing = digest({
      kind: record.kind,
      operation: record.operation,
      binding: record.binding,
      payload: record.payload,
      non_idempotent: record.nonIdempotent,
    });
    if (!constantTimeDigestEquals(candidate, existing)) {
      throw hostPortError("e02_host_port_idempotency_conflict", "host port idempotency key reused with different input");
    }
  }

  private refreshDigest(record: E02HostPortRecord): void {
    record.recordDigest = recordDigest(record);
  }

  private timestamp(): string {
    return this.now().toISOString();
  }
}

function normalizePrepare(inputValue: E02HostPortPrepareInput): E02HostPortPrepareInput {
  const input = cloneJson(inputValue);
  if (!input.operation.trim() || !input.idempotencyKey.trim()) {
    throw hostPortError("e02_host_port_identity_missing", "host port operation and idempotency key are required");
  }
  return {
    ...input,
    operation: input.operation.trim(),
    idempotencyKey: input.idempotencyKey.trim(),
    binding: canonicalize(input.binding) as unknown as E02HostPortBinding,
    payload: canonicalize(input.payload) as JsonObject,
    metadata: canonicalize(input.metadata ?? {}) as JsonObject,
  };
}

function buildReceipt(
  record: E02HostPortRecord,
  value: E02HostPortEffectResult,
  runtimeEpoch: number,
  receivedAt: string,
): E02HostPortReceipt {
  const output = canonicalize(value.output) as JsonObject;
  const base = {
    portRequestId: record.portRequestId,
    correlationId: record.binding.correlationId,
    requestDigest: record.payloadDigest,
    responseDigest: digest(output),
    ok: value.ok,
    output,
    errorCode: value.errorCode ?? null,
    errorMessage: value.errorMessage ?? null,
    providerReceiptId: value.providerReceiptId ?? null,
    receivedAt,
    runtimeEpoch,
    metadata: canonicalize(value.metadata ?? {}) as JsonObject,
  };
  const receiptId = deterministicId("e02-host-port-receipt", base, 48);
  return { receiptId, ...base, receiptDigest: digest({ receiptId, ...base }) };
}

function validateRecord(record: E02HostPortRecord): void {
  if (!record.portRequestId || !Number.isSafeInteger(record.sequence) || record.sequence < 1) {
    throw hostPortError("e02_host_port_record_invalid", "host port record identity is invalid");
  }
  if (!constantTimeDigestEquals(recordDigest(record), record.recordDigest)) {
    throw hostPortError("e02_host_port_record_digest", `host port record ${record.portRequestId} digest mismatch`);
  }
  if (record.receipt) {
    const { receiptDigest, ...withoutDigest } = record.receipt;
    if (!constantTimeDigestEquals(digest(withoutDigest), receiptDigest)) {
      throw hostPortError("e02_host_port_receipt_digest", `host port receipt ${record.receipt.receiptId} digest mismatch`);
    }
  }
  if (record.phase === "committed" && (!record.commit || !record.receipt?.ok)) {
    throw hostPortError("e02_host_port_commit_invalid", `committed host port request ${record.portRequestId} is incomplete`);
  }
}

function recordDigest(recordValue: Omit<E02HostPortRecord, "recordDigest"> | E02HostPortRecord): string {
  const record = recordValue as E02HostPortRecord;
  const { recordDigest: _ignored, ...withoutDigest } = record;
  return digest(withoutDigest);
}

function failureObject(error: unknown): JsonObject {
  if (error && typeof error === "object") {
    const value = error as { code?: unknown; message?: unknown; name?: unknown };
    return {
      code: typeof value.code === "string" && value.code ? value.code : "host_port_effect_failed",
      name: typeof value.name === "string" && value.name ? value.name : "Error",
      message: typeof value.message === "string" ? value.message : String(error),
    };
  }
  return { code: "host_port_effect_failed", name: "Error", message: String(error) };
}

function hostPortError(code: string, message: string, details: JsonObject = {}): Error {
  return Object.assign(new Error(message), {
    name: "E02HostPortError",
    code,
    details: cloneJson(details),
  });
}

export function isE02HostPortSnapshot(value: JsonValue): value is E02HostPortSnapshot & JsonObject {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && (value as { version?: unknown }).version === "zyra.e02-host-port/v1");
}
