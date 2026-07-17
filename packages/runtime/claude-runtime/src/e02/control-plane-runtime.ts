import type { JsonObject, JsonValue } from "../contracts.ts";
import {
  canonicalize,
  cloneJson,
  constantTimeDigestEquals,
  deterministicId,
  digest,
  hashChain,
  normalizeIdentifier,
} from "./canonical.ts";
import type { E02RuntimeIdentity } from "./contracts.ts";

export type E02ControlRisk = "read" | "bounded_write" | "privileged_write" | "recovery";
export type E02ControlPhase =
  | "prepared"
  | "running"
  | "effect_recorded"
  | "committed"
  | "failed"
  | "recovery_required"
  | "cancelled";

export interface E02ControlOperationDescriptor extends JsonObject {
  operation: string;
  domain: string;
  risk: E02ControlRisk;
  description: string;
  effectful: boolean;
  idempotent: boolean;
  requiresExpectedRevision: boolean;
  requiredFields: string[];
  optionalFields: string[];
  resultKind: string;
  enabled: boolean;
  descriptorDigest: string;
}

export interface E02ControlBinding extends JsonObject {
  runId: string;
  taskId: string;
  sessionId: string;
  workerRequestId: string;
  toolCallId: string;
  actor: string;
  correlationId: string;
}

export interface E02ControlRequest extends JsonObject {
  requestId: string;
  operation: string;
  binding: E02ControlBinding;
  payload: JsonObject;
  payloadDigest: string;
  expectedRevision: number | null;
  idempotencyKey: string;
  reason: string;
  requestedAt: string;
  metadata: JsonObject;
  requestDigest: string;
}

export interface E02ControlEffectReceipt extends JsonObject {
  effectId: string;
  requestId: string;
  operation: string;
  ok: boolean;
  result: JsonValue;
  resultDigest: string;
  providerReceiptId: string | null;
  startedAt: string;
  completedAt: string;
  metadata: JsonObject;
  effectDigest: string;
}

export interface E02ControlRecord extends JsonObject {
  request: E02ControlRequest;
  descriptor: E02ControlOperationDescriptor;
  phase: E02ControlPhase;
  revisionBefore: number;
  revisionAfter: number | null;
  preparedAt: string;
  startedAt: string | null;
  completedAt: string | null;
  effect: E02ControlEffectReceipt | null;
  result: JsonValue;
  error: JsonObject | null;
  recovery: JsonObject | null;
  priorRecordHash: string;
  recordHash: string;
}

export interface E02ControlBatchItem {
  operation: string;
  payload: JsonObject;
  expectedRevision?: number | null;
  idempotencyKey?: string;
  reason?: string;
  metadata?: JsonObject;
}

export interface E02ControlBatchReceipt extends JsonObject {
  batchId: string;
  binding: E02ControlBinding;
  atomicity: "stop_on_failure";
  itemRequestIds: string[];
  committedRequestIds: string[];
  failedRequestId: string | null;
  status: "committed" | "partially_committed" | "failed";
  startedAt: string;
  completedAt: string;
  resultDigest: string;
  metadata: JsonObject;
}

export interface E02ControlPlaneSnapshot {
  version: "zyra.e02-control-plane/v1";
  runtime: E02RuntimeIdentity;
  revision: number;
  sequence: number;
  previousRecordHash: string;
  descriptors: E02ControlOperationDescriptor[];
  records: E02ControlRecord[];
  batches: E02ControlBatchReceipt[];
  idempotency: Array<[string, string]>;
  snapshotHash: string;
}

export type E02ControlHandler = (
  request: E02ControlRequest,
  descriptor: E02ControlOperationDescriptor,
  signal?: AbortSignal,
) => Promise<{
  result: JsonValue;
  providerReceiptId?: string | null;
  revisionAfter?: number | null;
  metadata?: JsonObject;
}>;

type DescriptorSeed = {
  operation: string;
  domain: string;
  risk: E02ControlRisk;
  description: string;
  effectful: boolean;
  idempotent: boolean;
  requiresExpectedRevision: boolean;
  requiredFields: string[];
  optionalFields: string[];
  resultKind: string;
  enabled: boolean;
};

const BUILTIN_DESCRIPTORS: readonly DescriptorSeed[] = [
  descriptor("runtime.health.read", "runtime", "read", false, true, false, [], [], "runtime-health"),
  descriptor("runtime.snapshot.read", "runtime", "read", false, true, false, [], ["domains", "cursor", "limit"], "runtime-snapshot"),
  descriptor("route.catalog.read", "route", "read", false, true, false, [], ["tool_name", "include_candidates", "limit"], "route-catalog"),
  descriptor("checkpoint.history.read", "checkpoint", "read", false, true, false, [], ["phase", "reason", "limit"], "checkpoint-history"),
  descriptor("permission.mode.transition", "permission", "privileged_write", true, false, true, ["mode", "actor", "reason"], ["metadata"], "permission-mode-transition"),
  descriptor("permission.rules.replace", "permission", "privileged_write", true, false, true, ["rules", "actor", "reason"], ["metadata"], "permission-policy-commit"),
  descriptor("permission.settings.reload", "permission", "bounded_write", true, true, false, ["actor", "reason"], ["metadata"], "permission-policy-commit"),
  descriptor("mcp.reload", "mcp", "bounded_write", true, true, false, ["actor", "reason"], ["server_id", "metadata"], "mcp-reload"),
  descriptor("skill.reload", "skill", "bounded_write", true, true, false, ["actor", "reason"], ["metadata"], "skill-reload"),
  descriptor("plugin.reload", "plugin", "bounded_write", true, true, false, ["actor", "reason"], ["metadata"], "plugin-reload"),
  descriptor("recovery.transition.reconcile", "recovery", "recovery", true, false, false, ["transition_id", "outcome", "actor", "reason"], ["result", "provider_receipt_id", "metadata"], "transition-recovery"),
  descriptor("recovery.checkpoint.reconcile", "recovery", "recovery", true, false, false, ["bundle_id", "outcome", "actor", "reason"], ["delivered_snapshot_hash", "provider_receipt_id"], "checkpoint-recovery"),
  descriptor("checkpoint.compact", "checkpoint", "bounded_write", true, true, false, ["actor", "reason"], ["retain_count"], "checkpoint-compaction"),
];

export class E02ControlPlaneRuntime {
  readonly runtime: E02RuntimeIdentity;
  private readonly handler: E02ControlHandler;
  private readonly now: () => Date;
  private revision = 0;
  private sequence = 0;
  private previousRecordHash = digest({ genesis: "zyra.e02-control-plane/v1" });
  private readonly descriptors = new Map<string, E02ControlOperationDescriptor>();
  private readonly records = new Map<string, E02ControlRecord>();
  private readonly batches = new Map<string, E02ControlBatchReceipt>();
  private readonly idempotency = new Map<string, string>();
  private readonly inFlight = new Map<string, Promise<E02ControlRecord>>();

  constructor(options: {
    runtime: E02RuntimeIdentity;
    handler: E02ControlHandler;
    now?: () => Date;
    snapshot?: E02ControlPlaneSnapshot | null;
  }) {
    this.runtime = cloneJson(options.runtime);
    this.handler = options.handler;
    this.now = options.now ?? (() => new Date());
    for (const value of BUILTIN_DESCRIPTORS) this.registerDescriptor(value);
    if (options.snapshot) this.restore(options.snapshot);
  }

  registerDescriptor(value: DescriptorSeed): E02ControlOperationDescriptor {
    const operation = normalizeOperation(value.operation);
    if (!operation || !value.domain.trim() || !value.description.trim()) {
      throw controlError("e02_control_descriptor_invalid", "control descriptor requires operation, domain, and description");
    }
    const requiredFields = normalizeFields(value.requiredFields);
    const optionalFields = normalizeFields(value.optionalFields).filter((field) => !requiredFields.includes(field));
    const base = {
      operation,
      domain: normalizeIdentifier(value.domain),
      risk: value.risk,
      description: value.description.trim(),
      effectful: value.effectful,
      idempotent: value.idempotent,
      requiresExpectedRevision: value.requiresExpectedRevision,
      requiredFields,
      optionalFields,
      resultKind: normalizeIdentifier(value.resultKind),
      enabled: value.enabled,
    };
    const registered: E02ControlOperationDescriptor = { ...base, descriptorDigest: digest(base) };
    const prior = this.descriptors.get(operation);
    if (prior && !constantTimeDigestEquals(prior.descriptorDigest, registered.descriptorDigest)) {
      throw controlError("e02_control_descriptor_collision", `control operation ${operation} is already registered differently`);
    }
    this.descriptors.set(operation, registered);
    return cloneJson(registered);
  }

  setDescriptorEnabled(operationValue: string, enabled: boolean, actor: string, reason: string): E02ControlOperationDescriptor {
    const value = this.requireDescriptor(operationValue);
    if (!actor.trim() || !reason.trim()) {
      throw controlError("e02_control_descriptor_change_identity_missing", "descriptor changes require actor and reason");
    }
    if (value.enabled === enabled) return cloneJson(value);
    value.enabled = enabled;
    value.descriptorDigest = descriptorDigest(value);
    return cloneJson(value);
  }

  descriptor(operationValue: string): E02ControlOperationDescriptor | null {
    const value = this.descriptors.get(normalizeOperation(operationValue));
    return value ? cloneJson(value) : null;
  }

  listDescriptors(input: { domain?: string; risk?: E02ControlRisk; enabled?: boolean } = {}): E02ControlOperationDescriptor[] {
    return [...this.descriptors.values()]
      .filter((value) => !input.domain || value.domain === normalizeIdentifier(input.domain))
      .filter((value) => !input.risk || value.risk === input.risk)
      .filter((value) => input.enabled === undefined || value.enabled === input.enabled)
      .sort((left, right) => left.operation.localeCompare(right.operation))
      .map(cloneJson);
  }

  prepare(input: {
    operation: string;
    binding: E02ControlBinding;
    payload?: JsonObject;
    expectedRevision?: number | null;
    idempotencyKey?: string;
    reason?: string;
    metadata?: JsonObject;
  }): E02ControlRecord {
    const descriptor = this.requireDescriptor(input.operation);
    if (!descriptor.enabled) {
      throw controlError("e02_control_operation_disabled", `control operation ${descriptor.operation} is disabled`);
    }
    const binding = this.normalizeBinding(input.binding);
    const payload = canonicalize(input.payload ?? {}) as JsonObject;
    this.validatePayload(descriptor, payload);
    const expectedRevision = input.expectedRevision ?? null;
    if (descriptor.requiresExpectedRevision && expectedRevision === null) {
      throw controlError("e02_control_expected_revision_missing", `${descriptor.operation} requires expected_revision`);
    }
    if (expectedRevision !== null && (!Number.isSafeInteger(expectedRevision) || expectedRevision < 0)) {
      throw controlError("e02_control_expected_revision_invalid", `control revision ${expectedRevision} is invalid`);
    }
    const payloadDigest = digest(payload);
    const idempotencyKey = input.idempotencyKey?.trim() || deterministicId("e02-control-idempotency", {
      operation: descriptor.operation,
      binding,
      payload_digest: payloadDigest,
    }, 48);
    const priorId = this.idempotency.get(idempotencyKey);
    if (priorId) {
      const prior = this.requireRecord(priorId);
      this.assertReplayEquivalent(prior, descriptor.operation, binding, payloadDigest, expectedRevision);
      return cloneJson(prior);
    }
    const requestedAt = this.timestamp();
    const requestBase = {
      operation: descriptor.operation,
      binding,
      payload,
      payloadDigest,
      expectedRevision,
      idempotencyKey,
      reason: input.reason?.trim() || `${descriptor.operation} requested by ${binding.actor}`,
      requestedAt,
      metadata: canonicalize(input.metadata ?? {}) as JsonObject,
    };
    const requestId = deterministicId("e02-control-request", requestBase, 48);
    const request: E02ControlRequest = {
      requestId,
      ...requestBase,
      requestDigest: digest({ requestId, ...requestBase }),
    };
    if (this.records.has(requestId)) {
      throw controlError("e02_control_request_collision", `control request ${requestId} collided`);
    }
    const record: E02ControlRecord = {
      request,
      descriptor: cloneJson(descriptor),
      phase: "prepared",
      revisionBefore: this.revision,
      revisionAfter: null,
      preparedAt: requestedAt,
      startedAt: null,
      completedAt: null,
      effect: null,
      result: null,
      error: null,
      recovery: null,
      priorRecordHash: this.previousRecordHash,
      recordHash: "",
    };
    record.recordHash = controlRecordDigest(record);
    this.records.set(requestId, record);
    this.idempotency.set(idempotencyKey, requestId);
    return cloneJson(record);
  }

  async execute(input: {
    operation: string;
    binding: E02ControlBinding;
    payload?: JsonObject;
    expectedRevision?: number | null;
    idempotencyKey?: string;
    reason?: string;
    metadata?: JsonObject;
  }, signal?: AbortSignal): Promise<E02ControlRecord> {
    const prepared = this.prepare(input);
    if (prepared.phase === "committed") return prepared;
    if (prepared.phase === "recovery_required") {
      throw controlError("e02_control_recovery_required", `control request ${prepared.request.requestId} requires reconciliation`, {
        request_id: prepared.request.requestId,
        recovery: prepared.recovery,
      });
    }
    if (prepared.phase === "failed" || prepared.phase === "cancelled") {
      throw controlError("e02_control_terminal_replay", `control request is already ${prepared.phase}`, {
        request_id: prepared.request.requestId,
        error: prepared.error,
      });
    }
    const running = this.inFlight.get(prepared.request.requestId);
    if (running) return running;
    const promise = this.perform(prepared.request.requestId, signal);
    this.inFlight.set(prepared.request.requestId, promise);
    try {
      return await promise;
    } finally {
      this.inFlight.delete(prepared.request.requestId);
    }
  }

  async executeBatch(
    bindingValue: E02ControlBinding,
    itemsValue: E02ControlBatchItem[],
    metadataValue: JsonObject = {},
    signal?: AbortSignal,
  ): Promise<E02ControlBatchReceipt> {
    const binding = this.normalizeBinding(bindingValue);
    if (!Array.isArray(itemsValue) || itemsValue.length === 0) {
      throw controlError("e02_control_batch_empty", "control batch requires at least one operation");
    }
    if (itemsValue.length > 64) {
      throw controlError("e02_control_batch_too_large", "control batch exceeds 64 operations");
    }
    const items = itemsValue.map((value) => canonicalize(value) as unknown as E02ControlBatchItem);
    const startedAt = this.timestamp();
    const batchId = deterministicId("e02-control-batch", {
      binding,
      items: items.map((value) => ({
        operation: normalizeOperation(value.operation),
        payload_digest: digest(value.payload),
        expected_revision: value.expectedRevision ?? null,
      })),
      started_at: startedAt,
    }, 48);
    const prior = this.batches.get(batchId);
    if (prior) return cloneJson(prior);
    const itemRequestIds: string[] = [];
    const committedRequestIds: string[] = [];
    let failedRequestId: string | null = null;
    for (const [index, item] of items.entries()) {
      if (signal?.aborted) {
        failedRequestId = `batch-aborted:${index}`;
        break;
      }
      const itemKey = item.idempotencyKey || `${batchId}:${index}`;
      try {
        const record = await this.execute({
          operation: item.operation,
          binding: { ...binding, correlationId: `${binding.correlationId}:batch:${batchId}:${index}` },
          payload: item.payload,
          expectedRevision: item.expectedRevision ?? null,
          idempotencyKey: itemKey,
          reason: item.reason || `batch ${batchId} item ${index}`,
          metadata: { ...metadataValue, ...item.metadata, batch_id: batchId, batch_index: index },
        }, signal);
        itemRequestIds.push(record.request.requestId);
        if (record.phase !== "committed") {
          failedRequestId = record.request.requestId;
          break;
        }
        committedRequestIds.push(record.request.requestId);
      } catch (error) {
        const requestId = this.idempotency.get(itemKey) ?? null;
        if (requestId) itemRequestIds.push(requestId);
        failedRequestId = requestId ?? `batch-error:${index}:${errorCode(error)}`;
        break;
      }
    }
    const completedAt = this.timestamp();
    const status = failedRequestId === null
      ? "committed"
      : committedRequestIds.length > 0
        ? "partially_committed"
        : "failed";
    const base = {
      batchId,
      binding,
      atomicity: "stop_on_failure" as const,
      itemRequestIds,
      committedRequestIds,
      failedRequestId,
      status: status as E02ControlBatchReceipt["status"],
      startedAt,
      completedAt,
      metadata: canonicalize(metadataValue) as JsonObject,
    };
    const receipt: E02ControlBatchReceipt = { ...base, resultDigest: digest(base) };
    this.batches.set(batchId, receipt);
    return cloneJson(receipt);
  }

  cancel(requestId: string, actor: string, reason: string): E02ControlRecord {
    const record = this.requireRecord(requestId);
    if (!actor.trim() || !reason.trim()) {
      throw controlError("e02_control_cancel_identity_missing", "control cancellation requires actor and reason");
    }
    if (record.phase === "cancelled") return cloneJson(record);
    if (record.phase !== "prepared") {
      throw controlError("e02_control_cancel_too_late", `cannot cancel ${requestId} from ${record.phase}`);
    }
    record.phase = "cancelled";
    record.completedAt = this.timestamp();
    record.error = { code: "control_cancelled", message: reason, actor };
    this.finalizeRecord(record);
    return cloneJson(record);
  }

  reconcile(requestId: string, input: {
    outcome: "confirm_effect" | "confirm_no_effect";
    actor: string;
    reason: string;
    result?: JsonValue;
    providerReceiptId?: string | null;
    metadata?: JsonObject;
  }): E02ControlRecord {
    const record = this.requireRecord(requestId);
    if (record.phase !== "recovery_required") {
      throw controlError("e02_control_reconciliation_not_required", `control request ${requestId} is ${record.phase}`);
    }
    if (!input.actor.trim() || !input.reason.trim()) {
      throw controlError("e02_control_reconciliation_identity_missing", "reconciliation requires actor and reason");
    }
    if (input.outcome === "confirm_no_effect") {
      record.phase = "failed";
      record.completedAt = this.timestamp();
      record.error = {
        code: "control_effect_confirmed_absent",
        message: input.reason,
        actor: input.actor,
      };
      record.recovery = {
        ...record.recovery,
        outcome: input.outcome,
        reconciled_by: input.actor,
        reconciled_at: record.completedAt,
      };
      this.finalizeRecord(record);
      return cloneJson(record);
    }
    const completedAt = this.timestamp();
    const result = canonicalize(input.result ?? record.effect?.result ?? null);
    const effectBase = {
      requestId,
      operation: record.request.operation,
      ok: true,
      result,
      resultDigest: digest(result),
      providerReceiptId: input.providerReceiptId ?? record.effect?.providerReceiptId ?? null,
      startedAt: record.startedAt ?? record.preparedAt,
      completedAt,
      metadata: canonicalize({
        ...input.metadata,
        reconciled_by: input.actor,
        reconciliation_reason: input.reason,
      }) as JsonObject,
    };
    const effectId = deterministicId("e02-control-effect", effectBase, 48);
    record.effect = { effectId, ...effectBase, effectDigest: digest({ effectId, ...effectBase }) };
    record.result = result;
    record.phase = "committed";
    record.completedAt = completedAt;
    record.error = null;
    record.recovery = {
      ...record.recovery,
      outcome: input.outcome,
      reconciled_by: input.actor,
      reconciled_at: completedAt,
      reason: input.reason,
    };
    this.finalizeRecord(record);
    return cloneJson(record);
  }

  record(requestId: string): E02ControlRecord | null {
    const value = this.records.get(requestId);
    return value ? cloneJson(value) : null;
  }

  listRecords(input: {
    operation?: string;
    domain?: string;
    phase?: E02ControlPhase;
    actor?: string;
    limit?: number;
  } = {}): E02ControlRecord[] {
    const limit = Math.max(1, Math.min(10_000, Math.trunc(input.limit ?? 100)));
    const operation = input.operation ? normalizeOperation(input.operation) : null;
    const domain = input.domain ? normalizeIdentifier(input.domain) : null;
    return [...this.records.values()]
      .filter((value) => !operation || value.request.operation === operation)
      .filter((value) => !domain || value.descriptor.domain === domain)
      .filter((value) => !input.phase || value.phase === input.phase)
      .filter((value) => !input.actor || value.request.binding.actor === input.actor)
      .sort((left, right) => right.preparedAt.localeCompare(left.preparedAt))
      .slice(0, limit)
      .map(cloneJson);
  }

  listBatches(limitValue = 100): E02ControlBatchReceipt[] {
    const limit = Math.max(1, Math.min(10_000, Math.trunc(limitValue)));
    return [...this.batches.values()]
      .sort((left, right) => right.startedAt.localeCompare(left.startedAt))
      .slice(0, limit)
      .map(cloneJson);
  }

  pendingRecovery(): E02ControlRecord[] {
    return [...this.records.values()]
      .filter((value) => value.phase === "recovery_required")
      .sort((left, right) => left.preparedAt.localeCompare(right.preparedAt))
      .map(cloneJson);
  }

  verifyRecord(requestId: string): JsonObject {
    const record = this.requireRecord(requestId);
    const errors: JsonObject[] = [];
    const { requestDigest, ...requestBase } = record.request;
    if (!constantTimeDigestEquals(digest(requestBase), requestDigest)) errors.push({ code: "request_digest_mismatch" });
    if (!constantTimeDigestEquals(descriptorDigest(record.descriptor), record.descriptor.descriptorDigest)) {
      errors.push({ code: "descriptor_digest_mismatch" });
    }
    if (record.effect) {
      const { effectDigest, ...effectBase } = record.effect;
      if (!constantTimeDigestEquals(digest(effectBase), effectDigest)) errors.push({ code: "effect_digest_mismatch" });
    }
    if (!record.recordHash) errors.push({ code: "record_hash_missing" });
    if (record.phase === "committed" && record.descriptor.effectful && !record.effect) {
      errors.push({ code: "committed_effect_missing" });
    }
    if (record.phase === "recovery_required" && !record.recovery) {
      errors.push({ code: "recovery_metadata_missing" });
    }
    return {
      ok: errors.length === 0,
      request_id: requestId,
      operation: record.request.operation,
      phase: record.phase,
      errors,
    };
  }

  verifyChain(): JsonObject {
    const terminal = [...this.records.values()]
      .filter((value) => isTerminal(value.phase))
      .sort((left, right) => (left.revisionAfter ?? Number.MAX_SAFE_INTEGER)
        - (right.revisionAfter ?? Number.MAX_SAFE_INTEGER));
    const errors: JsonObject[] = [];
    let previous = digest({ genesis: "zyra.e02-control-plane/v1" });
    let revision = 0;
    for (const record of terminal) {
      if (!constantTimeDigestEquals(record.priorRecordHash, previous)) {
        errors.push({ code: "prior_record_hash_mismatch", request_id: record.request.requestId });
      }
      if ((record.revisionAfter ?? -1) !== revision + 1) {
        errors.push({ code: "revision_gap", request_id: record.request.requestId, expected: revision + 1, actual: record.revisionAfter });
      }
      previous = record.recordHash;
      revision = record.revisionAfter ?? revision;
    }
    if (revision !== this.revision) errors.push({ code: "head_revision_mismatch", expected: revision, actual: this.revision });
    if (!constantTimeDigestEquals(previous, this.previousRecordHash)) errors.push({ code: "head_hash_mismatch" });
    return {
      ok: errors.length === 0,
      revision: this.revision,
      terminal_record_count: terminal.length,
      head_hash: this.previousRecordHash,
      errors,
    };
  }

  health(): JsonObject {
    const phases = new Map<E02ControlPhase, number>();
    for (const record of this.records.values()) {
      phases.set(record.phase, (phases.get(record.phase) ?? 0) + 1);
    }
    return {
      canonical_owner: "typescript",
      runtime_epoch: this.runtime.epoch,
      revision: this.revision,
      sequence: this.sequence,
      descriptor_count: this.descriptors.size,
      enabled_descriptor_count: [...this.descriptors.values()].filter((value) => value.enabled).length,
      record_count: this.records.size,
      batch_count: this.batches.size,
      in_flight_count: this.inFlight.size,
      recovery_required_count: this.pendingRecovery().length,
      phase_counts: Object.fromEntries(phases),
      chain_ok: this.verifyChain().ok,
      previous_record_hash: this.previousRecordHash,
      python_control_fallback: false,
    };
  }

  snapshot(): E02ControlPlaneSnapshot {
    const withoutHash = {
      version: "zyra.e02-control-plane/v1" as const,
      runtime: cloneJson(this.runtime),
      revision: this.revision,
      sequence: this.sequence,
      previousRecordHash: this.previousRecordHash,
      descriptors: [...this.descriptors.values()]
        .sort((left, right) => left.operation.localeCompare(right.operation))
        .map(cloneJson),
      records: [...this.records.values()]
        .sort((left, right) => left.preparedAt.localeCompare(right.preparedAt))
        .map(cloneJson),
      batches: [...this.batches.values()]
        .sort((left, right) => left.startedAt.localeCompare(right.startedAt))
        .map(cloneJson),
      idempotency: [...this.idempotency.entries()].sort(([left], [right]) => left.localeCompare(right)),
    };
    return { ...withoutHash, snapshotHash: digest(withoutHash) };
  }

  private async perform(requestId: string, signal?: AbortSignal): Promise<E02ControlRecord> {
    const record = this.requireRecord(requestId);
    if (record.phase !== "prepared") {
      throw controlError("e02_control_phase_invalid", `control request ${requestId} cannot start from ${record.phase}`);
    }
    if (signal?.aborted) {
      record.phase = "cancelled";
      record.completedAt = this.timestamp();
      record.error = { code: "control_aborted_before_effect", message: abortMessage(signal.reason) };
      this.finalizeRecord(record);
      return cloneJson(record);
    }
    if (record.request.expectedRevision !== null && record.request.expectedRevision !== this.revision) {
      record.phase = "failed";
      record.completedAt = this.timestamp();
      record.error = {
        code: "control_compare_and_swap_failed",
        message: `expected revision ${record.request.expectedRevision}, current revision ${this.revision}`,
        expected_revision: record.request.expectedRevision,
        actual_revision: this.revision,
      };
      this.finalizeRecord(record);
      return cloneJson(record);
    }
    record.phase = "running";
    record.startedAt = this.timestamp();
    record.recordHash = controlRecordDigest(record);
    try {
      const output = await this.handler(cloneJson(record.request), cloneJson(record.descriptor), signal);
      const completedAt = this.timestamp();
      const result = canonicalize(output.result);
      const effectBase = {
        requestId,
        operation: record.request.operation,
        ok: true,
        result,
        resultDigest: digest(result),
        providerReceiptId: output.providerReceiptId ?? null,
        startedAt: record.startedAt,
        completedAt,
        metadata: canonicalize(output.metadata ?? {}) as JsonObject,
      };
      const effectId = deterministicId("e02-control-effect", effectBase, 48);
      record.effect = { effectId, ...effectBase, effectDigest: digest({ effectId, ...effectBase }) };
      record.phase = "effect_recorded";
      record.result = result;
      record.recordHash = controlRecordDigest(record);
      record.phase = "committed";
      record.completedAt = completedAt;
      record.revisionAfter = output.revisionAfter ?? null;
      if (record.revisionAfter !== null && record.revisionAfter !== this.revision + 1) {
        throw controlError("e02_control_handler_revision_invalid", `handler returned revision ${record.revisionAfter}`);
      }
      this.finalizeRecord(record);
      return cloneJson(record);
    } catch (error) {
      const effectMayHaveOccurred = record.descriptor.effectful
        && (record.phase === "effect_recorded" || Boolean(record.effect));
      record.completedAt = this.timestamp();
      record.error = errorObject(error);
      if (effectMayHaveOccurred && !record.descriptor.idempotent) {
        record.phase = "recovery_required";
        record.recovery = {
          kind: "control_effect_reconciliation",
          request_id: requestId,
          operation: record.request.operation,
          effect: canonicalize(record.effect),
          reexecute_without_receipt: false,
          failure: record.error,
        };
        record.recordHash = controlRecordDigest(record);
      } else {
        record.phase = "failed";
        this.finalizeRecord(record);
      }
      throw error;
    }
  }

  private finalizeRecord(record: E02ControlRecord): void {
    if (!isTerminal(record.phase)) {
      throw controlError("e02_control_finalize_nonterminal", `cannot finalize ${record.request.requestId} in ${record.phase}`);
    }
    const revisionAfter = record.revisionAfter ?? this.revision + 1;
    if (revisionAfter !== this.revision + 1) {
      throw controlError("e02_control_revision_not_contiguous", `control request ${record.request.requestId} revision is not contiguous`);
    }
    record.revisionAfter = revisionAfter;
    record.priorRecordHash = this.previousRecordHash;
    record.recordHash = hashChain(this.previousRecordHash, {
      request_id: record.request.requestId,
      phase: record.phase,
      revision_before: record.revisionBefore,
      revision_after: revisionAfter,
      base_hash: controlRecordDigest(record),
    });
    this.revision = revisionAfter;
    this.sequence += 1;
    this.previousRecordHash = record.recordHash;
  }

  private validatePayload(descriptor: E02ControlOperationDescriptor, payload: JsonObject): void {
    for (const required of descriptor.requiredFields) {
      if (!(required in payload) || payload[required] === null || payload[required] === "") {
        throw controlError("e02_control_payload_field_missing", `${descriptor.operation} requires payload.${required}`, {
          operation: descriptor.operation,
          field: required,
        });
      }
    }
    const allowed = new Set([...descriptor.requiredFields, ...descriptor.optionalFields]);
    const unknown = Object.keys(payload).filter((key) => !allowed.has(key));
    if (unknown.length > 0) {
      throw controlError("e02_control_payload_field_unknown", `${descriptor.operation} received unknown fields`, {
        operation: descriptor.operation,
        fields: unknown.sort(),
      });
    }
  }

  private normalizeBinding(value: E02ControlBinding): E02ControlBinding {
    const binding = canonicalize(value) as E02ControlBinding;
    if (
      binding.runId !== this.runtime.runId
      || binding.taskId !== this.runtime.taskId
      || binding.sessionId !== this.runtime.sessionId
      || binding.workerRequestId !== this.runtime.workerRequestId
    ) {
      throw controlError("e02_control_binding_mismatch", "control request binding differs from the active E02 runtime");
    }
    if (!binding.toolCallId?.trim() || !binding.actor?.trim() || !binding.correlationId?.trim()) {
      throw controlError("e02_control_binding_incomplete", "control binding requires tool call, actor, and correlation ids");
    }
    return binding;
  }

  private assertReplayEquivalent(
    record: E02ControlRecord,
    operation: string,
    binding: E02ControlBinding,
    payloadDigest: string,
    expectedRevision: number | null,
  ): void {
    if (
      record.request.operation !== operation
      || record.request.binding.runId !== binding.runId
      || record.request.binding.taskId !== binding.taskId
      || record.request.binding.sessionId !== binding.sessionId
      || record.request.binding.workerRequestId !== binding.workerRequestId
      || record.request.binding.toolCallId !== binding.toolCallId
      || !constantTimeDigestEquals(record.request.payloadDigest, payloadDigest)
      || record.request.expectedRevision !== expectedRevision
    ) {
      throw controlError("e02_control_idempotency_collision", `idempotency key ${record.request.idempotencyKey} was reused`);
    }
  }

  private requireDescriptor(operationValue: string): E02ControlOperationDescriptor {
    const operation = normalizeOperation(operationValue);
    const value = this.descriptors.get(operation);
    if (!value) throw controlError("e02_control_operation_unknown", `unknown E02 control operation ${operationValue}`);
    return value;
  }

  private requireRecord(requestId: string): E02ControlRecord {
    const value = this.records.get(requestId);
    if (!value) throw controlError("e02_control_request_not_found", `control request ${requestId} was not found`);
    return value;
  }

  private restore(snapshotValue: E02ControlPlaneSnapshot): void {
    const snapshot = cloneJson(snapshotValue);
    if (snapshot.version !== "zyra.e02-control-plane/v1") {
      throw controlError("e02_control_snapshot_version", `unsupported control snapshot ${snapshot.version}`);
    }
    const { snapshotHash, ...withoutHash } = snapshot;
    if (!constantTimeDigestEquals(digest(withoutHash), snapshotHash)) {
      throw controlError("e02_control_snapshot_digest", "control plane snapshot digest mismatch");
    }
    this.assertRuntimeBinding(snapshot.runtime);
    if (this.runtime.epoch <= snapshot.runtime.epoch) {
      throw controlError("e02_control_snapshot_epoch", "control plane restore target epoch must advance");
    }
    for (const descriptorValue of snapshot.descriptors) {
      const current = this.descriptors.get(descriptorValue.operation);
      if (!current || !constantTimeDigestEquals(current.descriptorDigest, descriptorValue.descriptorDigest)) {
        throw controlError("e02_control_descriptor_contract_changed", `descriptor ${descriptorValue.operation} changed across restore`);
      }
      if (!constantTimeDigestEquals(descriptorDigest(descriptorValue), descriptorValue.descriptorDigest)) {
        throw controlError("e02_control_snapshot_descriptor", `descriptor ${descriptorValue.operation} digest mismatch`);
      }
    }
    for (const recordValue of snapshot.records) {
      const record = cloneJson(recordValue);
      if (this.records.has(record.request.requestId)) {
        throw controlError("e02_control_snapshot_duplicate_record", `duplicate request ${record.request.requestId}`);
      }
      const verification = verifyRestoredRecord(record);
      if (verification.ok !== true) {
        throw controlError("e02_control_snapshot_record_invalid", `request ${record.request.requestId} is invalid`, verification);
      }
      if (record.phase === "prepared") {
        record.phase = "cancelled";
        record.completedAt = this.timestamp();
        record.error = { code: "restore_epoch_fence", message: "prepared request cancelled on restore" };
      } else if (record.phase === "running" || record.phase === "effect_recorded") {
        record.phase = record.descriptor.effectful && !record.descriptor.idempotent
          ? "recovery_required"
          : "failed";
        record.completedAt = this.timestamp();
        record.error = { code: "restore_epoch_fence", message: "in-flight request fenced on restore" };
        if (record.phase === "recovery_required") {
          record.recovery = {
            kind: "restore_epoch_control_reconciliation",
            source_epoch: snapshot.runtime.epoch,
            target_epoch: this.runtime.epoch,
            reexecute_without_receipt: false,
          };
        }
      }
      this.records.set(record.request.requestId, record);
    }
    for (const batchValue of snapshot.batches) {
      if (this.batches.has(batchValue.batchId)) {
        throw controlError("e02_control_snapshot_duplicate_batch", `duplicate batch ${batchValue.batchId}`);
      }
      this.batches.set(batchValue.batchId, cloneJson(batchValue));
    }
    for (const [key, requestId] of snapshot.idempotency) {
      if (this.idempotency.has(key) || !this.records.has(requestId)) {
        throw controlError("e02_control_snapshot_idempotency_invalid", `invalid idempotency binding ${key}`);
      }
      this.idempotency.set(key, requestId);
    }
    this.revision = snapshot.revision;
    this.sequence = snapshot.sequence;
    this.previousRecordHash = snapshot.previousRecordHash;
    const chain = this.verifyChain();
    if (chain.ok !== true) {
      throw controlError("e02_control_snapshot_chain_invalid", "control record chain is invalid", chain);
    }
  }

  private assertRuntimeBinding(runtime: E02RuntimeIdentity): void {
    if (
      runtime.runtimeId !== this.runtime.runtimeId
      || runtime.runId !== this.runtime.runId
      || runtime.taskId !== this.runtime.taskId
      || runtime.sessionId !== this.runtime.sessionId
      || runtime.workerRequestId !== this.runtime.workerRequestId
    ) {
      throw controlError("e02_control_snapshot_binding", "control snapshot belongs to another runtime binding");
    }
  }

  private timestamp(): string {
    return this.now().toISOString();
  }
}

function descriptor(
  operation: string,
  domain: string,
  risk: E02ControlRisk,
  effectful: boolean,
  idempotent: boolean,
  requiresExpectedRevision: boolean,
  requiredFields: string[],
  optionalFields: string[],
  resultKind: string,
): DescriptorSeed {
  return {
    operation,
    domain,
    risk,
    description: `E02 ${operation}`,
    effectful,
    idempotent,
    requiresExpectedRevision,
    requiredFields,
    optionalFields,
    resultKind,
    enabled: true,
  };
}

function verifyRestoredRecord(record: E02ControlRecord): JsonObject {
  const errors: string[] = [];
  const { requestDigest, ...requestBase } = record.request;
  if (!constantTimeDigestEquals(digest(requestBase), requestDigest)) errors.push("request_digest");
  if (!constantTimeDigestEquals(descriptorDigest(record.descriptor), record.descriptor.descriptorDigest)) errors.push("descriptor_digest");
  if (record.effect) {
    const { effectDigest, ...effectBase } = record.effect;
    if (!constantTimeDigestEquals(digest(effectBase), effectDigest)) errors.push("effect_digest");
  }
  if (!record.recordHash) errors.push("record_hash");
  return { ok: errors.length === 0, errors };
}

function descriptorDigest(value: E02ControlOperationDescriptor): string {
  const { descriptorDigest: _ignored, ...base } = value;
  return digest(base);
}

function controlRecordDigest(record: E02ControlRecord): string {
  return digest({
    request_digest: record.request.requestDigest,
    descriptor_digest: record.descriptor.descriptorDigest,
    phase: record.phase,
    revision_before: record.revisionBefore,
    revision_after: record.revisionAfter,
    prepared_at: record.preparedAt,
    started_at: record.startedAt,
    completed_at: record.completedAt,
    effect_digest: record.effect?.effectDigest ?? null,
    result_digest: digest(record.result),
    error: record.error,
    recovery: record.recovery,
    prior_record_hash: record.priorRecordHash,
  });
}

function normalizeOperation(value: string): string {
  return value.trim().toLowerCase().replaceAll(/[^a-z0-9._-]+/g, "-");
}

function normalizeFields(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))].sort();
}

function isTerminal(phase: E02ControlPhase): boolean {
  return phase === "committed" || phase === "failed" || phase === "cancelled";
}

function abortMessage(reason: unknown): string {
  if (reason instanceof Error) return reason.message;
  if (reason === undefined || reason === null) return "control request aborted";
  return String(reason);
}

function errorCode(error: unknown): string {
  if (error && typeof error === "object" && "code" in error && typeof error.code === "string") return error.code;
  return "control_operation_failed";
}

function errorObject(error: unknown): JsonObject {
  if (error instanceof Error) {
    const value = error as Error & { code?: unknown; details?: unknown };
    return {
      name: value.name,
      message: value.message,
      code: typeof value.code === "string" ? value.code : "control_operation_failed",
      details: canonicalize(value.details ?? {}) as JsonValue,
    };
  }
  return { name: "Error", message: String(error), code: "control_operation_failed", details: {} };
}

function controlError(code: string, message: string, details: JsonObject = {}): Error {
  return Object.assign(new Error(message), {
    name: "E02ControlPlaneError",
    code,
    details: cloneJson(details),
  });
}
