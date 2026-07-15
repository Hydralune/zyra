import { createHash, randomUUID } from "node:crypto";

import type { JsonObject, JsonValue } from "../contracts.ts";

export const PERMISSION_ENFORCEMENT_SNAPSHOT_VERSION = "zyra.permission-enforcement/v1";

export type PermissionEffect = "allow" | "deny" | "ask";
export type EnforcementBatchState = "evaluating" | "delegating" | "settling" | "completed" | "failed";
export type EnforcementDisposition =
  | "delegated"
  | "denied"
  | "approval_required"
  | "decision_failed"
  | "delegate_failed"
  | "protocol_failed";

export interface PermissionDecisionView {
  effect: PermissionEffect;
  reason: string;
  ruleId?: string | null;
  metadata?: JsonObject;
}

export interface EnforcementRequestIdentity {
  requestId: string;
  toolName: string;
  inputDigest: string;
}

export interface EnforcementReceiptIdentity {
  requestId: string;
  success: boolean;
  errorCode: string | null;
}

export interface EnforcementItemRecord {
  requestId: string;
  toolName: string;
  inputDigest: string;
  effect: PermissionEffect;
  reason: string;
  ruleId: string | null;
  disposition: EnforcementDisposition;
  delegated: boolean;
  success: boolean;
  errorCode: string | null;
  decisionSequence: number;
  settlementSequence: number | null;
  decisionDigest: string;
  receiptDigest: string | null;
}

export interface EnforcementBatchRecord {
  batchId: string;
  state: EnforcementBatchState;
  requestIds: string[];
  allowedRequestIds: string[];
  blockedRequestIds: string[];
  delegatedRequestIds: string[];
  settledRequestIds: string[];
  items: EnforcementItemRecord[];
  createdSequence: number;
  completedSequence: number | null;
  failure: string | null;
  batchDigest: string;
}

export interface EnforcementTransition {
  transitionId: string;
  sequence: number;
  batchId: string;
  from: EnforcementBatchState | "idle";
  to: EnforcementBatchState;
  reason: string;
  requestIds: string[];
  digest: string;
}

export interface PermissionEnforcementAudit {
  valid: boolean;
  errors: string[];
  batchCount: number;
  completedBatchCount: number;
  failedBatchCount: number;
  allowedCount: number;
  deniedCount: number;
  approvalRequiredCount: number;
  delegatedCount: number;
  syntheticCount: number;
  transitionCount: number;
  transitionIdCount: number;
  activeBatchIds: string[];
}

export interface PermissionEnforcementSnapshot {
  version: typeof PERMISSION_ENFORCEMENT_SNAPSHOT_VERSION;
  runtimeId: string;
  restartEpoch: number;
  sequence: number;
  batches: EnforcementBatchRecord[];
  transitions: EnforcementTransition[];
  checksum: string;
}

export interface PermissionEnforcementAdapter<TRequest, TDecision, TReceipt> {
  identifyRequest(request: TRequest, index: number): EnforcementRequestIdentity;
  decide(request: TRequest, index: number): TDecision | Promise<TDecision>;
  viewDecision(decision: TDecision, request: TRequest, index: number): PermissionDecisionView;
  delegate(requests: readonly TRequest[]): Promise<readonly TReceipt[]>;
  identifyReceipt(receipt: TReceipt, index: number): EnforcementReceiptIdentity;
  blockedReceipt(
    request: TRequest,
    decision: PermissionDecisionView,
    identity: EnforcementRequestIdentity,
  ): TReceipt;
  failedReceipt?(
    request: TRequest,
    identity: EnforcementRequestIdentity,
    errorCode: string,
    message: string,
  ): TReceipt;
  serializeDecision?(decision: TDecision): JsonValue;
  serializeReceipt?(receipt: TReceipt): JsonValue;
}

export class PermissionEnforcementError extends Error {
  readonly code: string;
  readonly details: JsonObject;

  constructor(code: string, message: string, details: JsonObject = {}) {
    super(message);
    this.name = "PermissionEnforcementError";
    this.code = code;
    this.details = details;
  }
}

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((entry) => stableStringify(entry)).join(",")}]`;
  }
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableStringify(record[key])}`)
    .join(",")}}`;
}

function digest(value: unknown): string {
  return createHash("sha256").update(stableStringify(value)).digest("hex");
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

function normalizeIdentity(identity: EnforcementRequestIdentity): EnforcementRequestIdentity {
  const requestId = identity.requestId.trim();
  const toolName = identity.toolName.trim();
  const inputDigest = identity.inputDigest.trim();
  if (!requestId) {
    throw new PermissionEnforcementError("permission_missing_request_id", "permission request id must not be empty");
  }
  if (!toolName) {
    throw new PermissionEnforcementError("permission_missing_tool_name", "permission tool name must not be empty", {
      request_id: requestId,
    });
  }
  if (!inputDigest) {
    throw new PermissionEnforcementError("permission_missing_input_digest", "permission input digest must not be empty", {
      request_id: requestId,
      tool_name: toolName,
    });
  }
  return { requestId, toolName, inputDigest };
}

function normalizeDecision(view: PermissionDecisionView): PermissionDecisionView {
  const effect = view.effect;
  if (effect !== "allow" && effect !== "deny" && effect !== "ask") {
    throw new PermissionEnforcementError(
      "permission_invalid_effect",
      `permission decision has unsupported effect: ${String(effect)}`,
    );
  }
  const reason = view.reason.trim() || `permission_${effect}`;
  return {
    effect,
    reason,
    ruleId: view.ruleId?.trim() || null,
    ...(view.metadata ? { metadata: clone(view.metadata) } : {}),
  };
}

function dispositionFor(effect: PermissionEffect): EnforcementDisposition {
  if (effect === "deny") {
    return "denied";
  }
  if (effect === "ask") {
    return "approval_required";
  }
  return "delegated";
}

function checksumPayload(snapshot: Omit<PermissionEnforcementSnapshot, "checksum">): string {
  return digest(snapshot);
}

function batchPayload(record: Omit<EnforcementBatchRecord, "batchDigest">): string {
  return digest(record);
}

function transitionPayload(record: Omit<EnforcementTransition, "digest">): string {
  return digest(record);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function errorCode(error: unknown, fallback: string): string {
  if (error instanceof PermissionEnforcementError) {
    return error.code;
  }
  if (error !== null && typeof error === "object" && "code" in error && typeof error.code === "string") {
    return error.code;
  }
  return fallback;
}

export class PermissionEnforcementRuntime {
  readonly runtimeId: string;

  private restartEpoch = 0;
  private sequence = 0;
  private readonly batches: EnforcementBatchRecord[] = [];
  private readonly transitions: EnforcementTransition[] = [];
  private readonly activeBatchIds = new Set<string>();

  constructor(runtimeId: string = randomUUID()) {
    this.runtimeId = runtimeId;
  }

  async enforce<TRequest, TDecision, TReceipt>(
    requests: readonly TRequest[],
    adapter: PermissionEnforcementAdapter<TRequest, TDecision, TReceipt>,
  ): Promise<TReceipt[]> {
    if (requests.length === 0) {
      return [];
    }
    const identities = requests.map((request, index) => normalizeIdentity(adapter.identifyRequest(request, index)));
    const requestIds = identities.map((identity) => identity.requestId);
    this.assertUnique(requestIds, "permission_duplicate_request_id");
    const batchId = `permission-batch:${this.runtimeId}:${this.restartEpoch}:${this.sequence + 1}:${randomUUID()}`;
    const batch: EnforcementBatchRecord = {
      batchId,
      state: "evaluating",
      requestIds,
      allowedRequestIds: [],
      blockedRequestIds: [],
      delegatedRequestIds: [],
      settledRequestIds: [],
      items: [],
      createdSequence: ++this.sequence,
      completedSequence: null,
      failure: null,
      batchDigest: "",
    };
    batch.batchDigest = this.digestBatch(batch);
    this.batches.push(batch);
    this.activeBatchIds.add(batchId);
    this.transition(batch, "idle", "evaluating", "batch_created", requestIds);

    const output = new Map<string, TReceipt>();
    const allowedRequests: TRequest[] = [];
    const allowedIdentities: EnforcementRequestIdentity[] = [];
    try {
      for (let index = 0; index < requests.length; index += 1) {
        const request = requests[index]!;
        const identity = identities[index]!;
        let decision: TDecision;
        let view: PermissionDecisionView;
        try {
          decision = await adapter.decide(request, index);
          view = normalizeDecision(adapter.viewDecision(decision, request, index));
        } catch (error) {
          const message = errorMessage(error);
          const code = errorCode(error, "permission_decision_failed");
          const fallback: PermissionDecisionView = {
            effect: "deny",
            reason: `${code}: ${message}`,
            ruleId: "zyra:decision-error:fail-closed",
          };
          const receipt = this.makeFailureReceipt(adapter, request, identity, code, message, fallback);
          const receiptIdentity = adapter.identifyReceipt(receipt, index);
          this.assertReceiptCorrelation(identity, receiptIdentity);
          output.set(identity.requestId, receipt);
          batch.blockedRequestIds.push(identity.requestId);
          batch.settledRequestIds.push(identity.requestId);
          batch.items.push({
            requestId: identity.requestId,
            toolName: identity.toolName,
            inputDigest: identity.inputDigest,
            effect: "deny",
            reason: fallback.reason,
            ruleId: fallback.ruleId ?? null,
            disposition: "decision_failed",
            delegated: false,
            success: false,
            errorCode: receiptIdentity.errorCode ?? code,
            decisionSequence: ++this.sequence,
            settlementSequence: ++this.sequence,
            decisionDigest: digest(fallback),
            receiptDigest: this.receiptDigest(adapter, receipt),
          });
          continue;
        }

        const serializedDecision = adapter.serializeDecision ? adapter.serializeDecision(decision) : view;
        const item: EnforcementItemRecord = {
          requestId: identity.requestId,
          toolName: identity.toolName,
          inputDigest: identity.inputDigest,
          effect: view.effect,
          reason: view.reason,
          ruleId: view.ruleId ?? null,
          disposition: dispositionFor(view.effect),
          delegated: false,
          success: false,
          errorCode: null,
          decisionSequence: ++this.sequence,
          settlementSequence: null,
          decisionDigest: digest(serializedDecision),
          receiptDigest: null,
        };
        batch.items.push(item);
        if (view.effect === "allow") {
          batch.allowedRequestIds.push(identity.requestId);
          allowedRequests.push(request);
          allowedIdentities.push(identity);
          continue;
        }
        batch.blockedRequestIds.push(identity.requestId);
        const receipt = adapter.blockedReceipt(request, view, identity);
        const receiptIdentity = adapter.identifyReceipt(receipt, index);
        this.assertReceiptCorrelation(identity, receiptIdentity);
        if (receiptIdentity.success) {
          throw new PermissionEnforcementError(
            "permission_blocked_receipt_succeeded",
            `blocked request ${identity.requestId} produced a successful receipt`,
            { request_id: identity.requestId, effect: view.effect },
          );
        }
        output.set(identity.requestId, receipt);
        batch.settledRequestIds.push(identity.requestId);
        item.success = false;
        item.errorCode = receiptIdentity.errorCode ?? (view.effect === "ask" ? "permission_approval_required" : "permission_denied");
        item.settlementSequence = ++this.sequence;
        item.receiptDigest = this.receiptDigest(adapter, receipt);
      }

      batch.batchDigest = this.digestBatch(batch);
      this.transition(batch, "evaluating", "delegating", "decisions_complete", batch.allowedRequestIds);
      batch.state = "delegating";
      if (allowedRequests.length > 0) {
        let delegatedReceipts: readonly TReceipt[];
        try {
          delegatedReceipts = await adapter.delegate(allowedRequests);
        } catch (error) {
          delegatedReceipts = allowedRequests.map((request, index) => {
            const identity = allowedIdentities[index]!;
            return this.makeFailureReceipt(
              adapter,
              request,
              identity,
              errorCode(error, "permission_delegate_failed"),
              errorMessage(error),
              { effect: "deny", reason: "delegate failed after permission allow" },
            );
          });
        }
        const delegatedById = new Map<string, TReceipt>();
        for (let index = 0; index < delegatedReceipts.length; index += 1) {
          const receipt = delegatedReceipts[index]!;
          const identity = adapter.identifyReceipt(receipt, index);
          if (!identity.requestId.trim()) {
            throw new PermissionEnforcementError(
              "permission_delegate_missing_correlation",
              "delegated receipt does not identify its request",
              { receipt_index: index },
            );
          }
          if (!batch.allowedRequestIds.includes(identity.requestId)) {
            throw new PermissionEnforcementError(
              "permission_delegate_extra_receipt",
              `delegate returned an unauthorized receipt for ${identity.requestId}`,
              { request_id: identity.requestId },
            );
          }
          if (delegatedById.has(identity.requestId)) {
            throw new PermissionEnforcementError(
              "permission_delegate_duplicate_receipt",
              `delegate returned duplicate receipts for ${identity.requestId}`,
              { request_id: identity.requestId },
            );
          }
          delegatedById.set(identity.requestId, receipt);
        }
        for (let index = 0; index < allowedRequests.length; index += 1) {
          const request = allowedRequests[index]!;
          const identity = allowedIdentities[index]!;
          let receipt = delegatedById.get(identity.requestId);
          if (!receipt) {
            receipt = this.makeFailureReceipt(
              adapter,
              request,
              identity,
              "permission_delegate_missing_receipt",
              `delegate did not return a receipt for ${identity.requestId}`,
              { effect: "deny", reason: "delegate protocol failed" },
            );
          }
          const receiptIdentity = adapter.identifyReceipt(receipt, index);
          this.assertReceiptCorrelation(identity, receiptIdentity);
          output.set(identity.requestId, receipt);
          batch.delegatedRequestIds.push(identity.requestId);
          batch.settledRequestIds.push(identity.requestId);
          const item = this.item(batch, identity.requestId);
          item.delegated = true;
          item.success = receiptIdentity.success;
          item.errorCode = receiptIdentity.errorCode;
          item.disposition = receiptIdentity.errorCode === "permission_delegate_missing_receipt"
            ? "protocol_failed"
            : receiptIdentity.success
              ? "delegated"
              : "delegate_failed";
          item.settlementSequence = ++this.sequence;
          item.receiptDigest = this.receiptDigest(adapter, receipt);
        }
      }

      this.transition(batch, "delegating", "settling", "delegate_complete", batch.delegatedRequestIds);
      batch.state = "settling";
      const ordered = identities.map((identity) => {
        const receipt = output.get(identity.requestId);
        if (!receipt) {
          throw new PermissionEnforcementError(
            "permission_unsettled_request",
            `permission enforcement did not settle ${identity.requestId}`,
            { request_id: identity.requestId },
          );
        }
        return receipt;
      });
      this.assertUnique(batch.settledRequestIds, "permission_duplicate_settlement");
      if (batch.settledRequestIds.length !== requests.length) {
        throw new PermissionEnforcementError(
          "permission_settlement_cardinality",
          "permission settlement count does not match request count",
          { expected: requests.length, actual: batch.settledRequestIds.length },
        );
      }
      batch.state = "completed";
      batch.completedSequence = ++this.sequence;
      batch.batchDigest = this.digestBatch(batch);
      this.transition(batch, "settling", "completed", "batch_settled", batch.settledRequestIds);
      this.activeBatchIds.delete(batch.batchId);
      return ordered;
    } catch (error) {
      batch.failure = errorMessage(error);
      const from = batch.state;
      batch.state = "failed";
      batch.completedSequence = ++this.sequence;
      batch.batchDigest = this.digestBatch(batch);
      this.transition(batch, from, "failed", errorCode(error, "permission_enforcement_failed"), batch.requestIds);
      this.activeBatchIds.delete(batch.batchId);
      throw error;
    }
  }

  audit(): PermissionEnforcementAudit {
    const errors: string[] = [];
    const batchIds = this.batches.map((batch) => batch.batchId);
    const transitionIds = this.transitions.map((transition) => transition.transitionId);
    this.collectDuplicates(batchIds, "duplicate batch id", errors);
    this.collectDuplicates(transitionIds, "duplicate transition id", errors);
    for (const batch of this.batches) {
      if (batch.batchDigest !== this.digestBatch(batch)) {
        errors.push(`batch digest mismatch: ${batch.batchId}`);
      }
      const requestSet = new Set(batch.requestIds);
      if (requestSet.size !== batch.requestIds.length) {
        errors.push(`duplicate request id in batch: ${batch.batchId}`);
      }
      if (batch.items.length !== batch.requestIds.length) {
        errors.push(`decision cardinality mismatch: ${batch.batchId}`);
      }
      for (const item of batch.items) {
        if (!requestSet.has(item.requestId)) {
          errors.push(`item outside batch: ${batch.batchId}:${item.requestId}`);
        }
        if (item.effect !== "allow" && item.delegated) {
          errors.push(`blocked item delegated: ${batch.batchId}:${item.requestId}`);
        }
        if (item.effect === "allow" && batch.state === "completed" && !item.delegated) {
          errors.push(`allowed item not delegated: ${batch.batchId}:${item.requestId}`);
        }
        if (batch.state === "completed" && item.settlementSequence === null) {
          errors.push(`completed item missing settlement: ${batch.batchId}:${item.requestId}`);
        }
      }
      if (batch.state === "completed" && batch.settledRequestIds.length !== batch.requestIds.length) {
        errors.push(`completed batch settlement mismatch: ${batch.batchId}`);
      }
      if ((batch.state === "completed" || batch.state === "failed") && this.activeBatchIds.has(batch.batchId)) {
        errors.push(`terminal batch remains active: ${batch.batchId}`);
      }
    }
    for (const transition of this.transitions) {
      if (transition.digest !== this.digestTransition(transition)) {
        errors.push(`transition digest mismatch: ${transition.transitionId}`);
      }
      if (!batchIds.includes(transition.batchId)) {
        errors.push(`transition references unknown batch: ${transition.transitionId}`);
      }
    }
    const items = this.batches.flatMap((batch) => batch.items);
    return {
      valid: errors.length === 0,
      errors,
      batchCount: this.batches.length,
      completedBatchCount: this.batches.filter((batch) => batch.state === "completed").length,
      failedBatchCount: this.batches.filter((batch) => batch.state === "failed").length,
      allowedCount: items.filter((item) => item.effect === "allow").length,
      deniedCount: items.filter((item) => item.effect === "deny").length,
      approvalRequiredCount: items.filter((item) => item.effect === "ask").length,
      delegatedCount: items.filter((item) => item.delegated).length,
      syntheticCount: items.filter((item) => !item.delegated).length,
      transitionCount: this.transitions.length,
      transitionIdCount: new Set(transitionIds).size,
      activeBatchIds: [...this.activeBatchIds].sort(),
    };
  }

  snapshot(): PermissionEnforcementSnapshot {
    const payload: Omit<PermissionEnforcementSnapshot, "checksum"> = {
      version: PERMISSION_ENFORCEMENT_SNAPSHOT_VERSION,
      runtimeId: this.runtimeId,
      restartEpoch: this.restartEpoch,
      sequence: this.sequence,
      batches: clone(this.batches),
      transitions: clone(this.transitions),
    };
    return { ...payload, checksum: checksumPayload(payload) };
  }

  restore(snapshot: PermissionEnforcementSnapshot): void {
    if (snapshot.version !== PERMISSION_ENFORCEMENT_SNAPSHOT_VERSION) {
      throw new PermissionEnforcementError(
        "permission_snapshot_version",
        `unsupported permission enforcement snapshot: ${String(snapshot.version)}`,
      );
    }
    const payload: Omit<PermissionEnforcementSnapshot, "checksum"> = {
      version: snapshot.version,
      runtimeId: snapshot.runtimeId,
      restartEpoch: snapshot.restartEpoch,
      sequence: snapshot.sequence,
      batches: snapshot.batches,
      transitions: snapshot.transitions,
    };
    if (snapshot.checksum !== checksumPayload(payload)) {
      throw new PermissionEnforcementError("permission_snapshot_checksum", "permission enforcement snapshot checksum mismatch");
    }
    if (snapshot.runtimeId !== this.runtimeId) {
      throw new PermissionEnforcementError(
        "permission_snapshot_runtime",
        "permission enforcement snapshot belongs to a different runtime",
        { expected: this.runtimeId, actual: snapshot.runtimeId },
      );
    }
    this.batches.splice(0, this.batches.length, ...clone(snapshot.batches));
    this.transitions.splice(0, this.transitions.length, ...clone(snapshot.transitions));
    this.activeBatchIds.clear();
    this.sequence = snapshot.sequence;
    this.restartEpoch = snapshot.restartEpoch + 1;
    for (const batch of this.batches) {
      if (batch.state === "completed" || batch.state === "failed") {
        continue;
      }
      const from = batch.state;
      batch.state = "failed";
      batch.failure = "runtime_restarted_before_permission_settlement";
      batch.completedSequence = ++this.sequence;
      batch.batchDigest = this.digestBatch(batch);
      this.transition(batch, from, "failed", "restart_fail_closed", batch.requestIds);
    }
    const audit = this.audit();
    if (!audit.valid) {
      throw new PermissionEnforcementError("permission_snapshot_invariant", audit.errors.join("; "));
    }
  }

  records(): EnforcementBatchRecord[] {
    return clone(this.batches);
  }

  private transition(
    batch: EnforcementBatchRecord,
    from: EnforcementBatchState | "idle",
    to: EnforcementBatchState,
    reason: string,
    requestIds: readonly string[],
  ): void {
    const transition: EnforcementTransition = {
      transitionId: `permission-transition:${this.runtimeId}:${this.restartEpoch}:${this.sequence + 1}:${randomUUID()}`,
      sequence: ++this.sequence,
      batchId: batch.batchId,
      from,
      to,
      reason,
      requestIds: [...requestIds],
      digest: "",
    };
    transition.digest = this.digestTransition(transition);
    this.transitions.push(transition);
  }

  private item(batch: EnforcementBatchRecord, requestId: string): EnforcementItemRecord {
    const item = batch.items.find((candidate) => candidate.requestId === requestId);
    if (!item) {
      throw new PermissionEnforcementError(
        "permission_missing_item",
        `permission decision record is missing for ${requestId}`,
        { batch_id: batch.batchId, request_id: requestId },
      );
    }
    return item;
  }

  private makeFailureReceipt<TRequest, TDecision, TReceipt>(
    adapter: PermissionEnforcementAdapter<TRequest, TDecision, TReceipt>,
    request: TRequest,
    identity: EnforcementRequestIdentity,
    code: string,
    message: string,
    fallback: PermissionDecisionView,
  ): TReceipt {
    if (adapter.failedReceipt) {
      return adapter.failedReceipt(request, identity, code, message);
    }
    return adapter.blockedReceipt(request, fallback, identity);
  }

  private receiptDigest<TRequest, TDecision, TReceipt>(
    adapter: PermissionEnforcementAdapter<TRequest, TDecision, TReceipt>,
    receipt: TReceipt,
  ): string {
    return digest(adapter.serializeReceipt ? adapter.serializeReceipt(receipt) : receipt);
  }

  private assertReceiptCorrelation(
    request: EnforcementRequestIdentity,
    receipt: EnforcementReceiptIdentity,
  ): void {
    if (receipt.requestId !== request.requestId) {
      throw new PermissionEnforcementError(
        "permission_receipt_correlation",
        `permission receipt ${receipt.requestId} does not match request ${request.requestId}`,
        { request_id: request.requestId, receipt_request_id: receipt.requestId },
      );
    }
  }

  private assertUnique(values: readonly string[], code: string): void {
    if (new Set(values).size !== values.length) {
      throw new PermissionEnforcementError(code, `${code}: values must be unique`, { values: [...values] });
    }
  }

  private collectDuplicates(values: readonly string[], label: string, errors: string[]): void {
    const seen = new Set<string>();
    for (const value of values) {
      if (seen.has(value)) {
        errors.push(`${label}: ${value}`);
      }
      seen.add(value);
    }
  }

  private digestBatch(batch: EnforcementBatchRecord): string {
    const { batchDigest: _ignored, ...payload } = batch;
    return batchPayload(payload);
  }

  private digestTransition(transition: EnforcementTransition): string {
    const { digest: _ignored, ...payload } = transition;
    return transitionPayload(payload);
  }
}
