import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type { PermissionApprovalResponse, PermissionContinuationRecord, PermissionDecisionRecord } from "../e02/contracts.ts";
import type { PermissionEvaluator, PermissionApprovalResult } from "./evaluator.ts";
import { PermissionAuditRuntime } from "./audit-runtime.ts";

export interface PermissionApprovalEnvelope {
  envelopeId: string;
  requestId: string;
  decisionId: string;
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  prompt: string;
  reason: string;
  toolName: string;
  namespace: string;
  serverId: string;
  operation: string;
  argumentsDigest: string;
  finalArguments: JsonObject;
  policyRevision: number;
  modeRevision: number;
  expiresAt: string;
  status: "pending_delivery" | "delivered" | "delivery_failed" | "responded" | "expired" | "cancelled";
  deliveryAttempt: number;
  deliveryReceiptId: string | null;
  createdAt: string;
  updatedAt: string;
  metadata: JsonObject;
}

export interface PermissionApprovalDeliveryReceipt {
  receiptId: string;
  envelopeId: string;
  requestId: string;
  transport: string;
  transportRequestId: string;
  accepted: boolean;
  deliveredAt: string;
  responseDigest: string | null;
  failure: JsonObject | null;
  metadata: JsonObject;
}

export interface PermissionApprovalSnapshot {
  version: "zyra.permission-approval-runtime/v1";
  revision: number;
  envelopes: PermissionApprovalEnvelope[];
  receipts: PermissionApprovalDeliveryReceipt[];
  digest: string;
  capturedAt: string;
}

export type PermissionApprovalTransport = (envelope: PermissionApprovalEnvelope, signal?: AbortSignal) => Promise<{
  transport: string;
  transportRequestId: string;
  accepted: boolean;
  responseDigest?: string | null;
  metadata?: JsonObject;
}>;

export class PermissionApprovalRuntime {
  private readonly evaluator: PermissionEvaluator;
  private readonly audit: PermissionAuditRuntime;
  private readonly transport: PermissionApprovalTransport;
  private readonly envelopes = new Map<string, PermissionApprovalEnvelope>();
  private readonly byRequest = new Map<string, string>();
  private readonly receipts = new Map<string, PermissionApprovalDeliveryReceipt>();
  private readonly inFlight = new Map<string, Promise<PermissionApprovalEnvelope>>();
  private readonly now: () => Date;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: {
    evaluator: PermissionEvaluator;
    audit: PermissionAuditRuntime;
    transport: PermissionApprovalTransport;
    now?: () => Date;
    snapshot?: PermissionApprovalSnapshot | null;
  }) {
    this.evaluator = options.evaluator;
    this.audit = options.audit;
    this.transport = options.transport;
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  async request(decisionValue: PermissionDecisionRecord, signal?: AbortSignal): Promise<PermissionApprovalEnvelope> {
    const decision = cloneJson(decisionValue);
    if (decision.effect !== "ask" || !decision.continuationRequestId) throw approvalError("approval_not_required", `decision ${decision.decisionId} is not an approval request`);
    const existingId = this.byRequest.get(decision.continuationRequestId);
    if (existingId) return cloneJson(this.envelopes.get(existingId)!);
    const pending = this.inFlight.get(decision.continuationRequestId);
    if (pending) return pending;
    const promise = this.deliver(decision, signal);
    this.inFlight.set(decision.continuationRequestId, promise);
    try {
      return await promise;
    } finally {
      this.inFlight.delete(decision.continuationRequestId);
    }
  }

  respond(responseValue: PermissionApprovalResponse): PermissionApprovalResult {
    const response = cloneJson(responseValue);
    const envelopeId = this.byRequest.get(response.requestId);
    if (!envelopeId) throw approvalError("approval_envelope_not_found", `approval request ${response.requestId} was not delivered by this runtime`);
    const envelope = this.envelopes.get(envelopeId)!;
    if (envelope.status !== "delivered") {
      const result: PermissionApprovalResult = {
        accepted: false,
        effect: response.effect,
        requestId: response.requestId,
        decision: null,
        errorCode: "approval_envelope_not_active",
        errorMessage: `approval envelope ${envelope.envelopeId} is ${envelope.status}`,
      };
      this.audit.approval(response, false, null, result.errorCode!);
      return result;
    }
    const result = this.evaluator.resumeApproval(response);
    envelope.status = result.accepted ? "responded" : envelope.status;
    envelope.updatedAt = this.timestamp();
    envelope.metadata = {
      ...envelope.metadata,
      response_id: response.responseId,
      response_effect: response.effect,
      response_accepted: result.accepted,
      response_error_code: result.errorCode,
    };
    this.revision += 1;
    this.audit.approval(response, result.accepted, result.decision, result.errorCode ?? result.decision?.reasonCode ?? "approval_resumed");
    return result;
  }

  cancel(requestId: string, reason = "cancelled"): PermissionApprovalEnvelope {
    const envelope = this.requireRequest(requestId);
    if (envelope.status === "responded" || envelope.status === "expired" || envelope.status === "cancelled") return cloneJson(envelope);
    envelope.status = "cancelled";
    envelope.updatedAt = this.timestamp();
    envelope.metadata = { ...envelope.metadata, cancellation_reason: reason };
    this.evaluator.continuations.cancel(requestId, reason);
    this.revision += 1;
    return cloneJson(envelope);
  }

  expire(now = this.now()): PermissionApprovalEnvelope[] {
    const expired: PermissionApprovalEnvelope[] = [];
    this.evaluator.continuations.expire(now.toISOString());
    for (const envelope of this.envelopes.values()) {
      if ((envelope.status === "delivered" || envelope.status === "pending_delivery") && Date.parse(envelope.expiresAt) <= now.getTime()) {
        envelope.status = "expired";
        envelope.updatedAt = this.timestamp();
        expired.push(cloneJson(envelope));
        this.revision += 1;
      }
    }
    return expired;
  }

  get(requestId: string): PermissionApprovalEnvelope | null {
    const id = this.byRequest.get(requestId);
    return id ? cloneJson(this.envelopes.get(id)!) : null;
  }

  list(status?: PermissionApprovalEnvelope["status"]): PermissionApprovalEnvelope[] {
    return [...this.envelopes.values()].filter((value) => !status || value.status === status).sort((left, right) => left.createdAt.localeCompare(right.createdAt)).map(cloneJson);
  }

  snapshot(): PermissionApprovalSnapshot {
    const withoutDigest = {
      version: "zyra.permission-approval-runtime/v1" as const,
      revision: this.revision,
      envelopes: this.list(),
      receipts: [...this.receipts.values()].sort((left, right) => left.deliveredAt.localeCompare(right.deliveredAt)).map(cloneJson),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: PermissionApprovalSnapshot): void {
    if (snapshot.version !== "zyra.permission-approval-runtime/v1") throw approvalError("unsupported_approval_snapshot", "unsupported permission approval snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw approvalError("approval_snapshot_digest_mismatch", "permission approval snapshot digest mismatch");
    this.envelopes.clear();
    this.byRequest.clear();
    this.receipts.clear();
    this.revision = snapshot.revision;
    for (const value of snapshot.envelopes) {
      const envelope = cloneJson(value);
      if (envelope.status === "pending_delivery") {
        envelope.status = "delivery_failed";
        envelope.updatedAt = snapshot.capturedAt;
        envelope.metadata = { ...envelope.metadata, restore_failure: "approval delivery interrupted by restart" };
      }
      this.envelopes.set(envelope.envelopeId, envelope);
      this.byRequest.set(envelope.requestId, envelope.envelopeId);
    }
    for (const receipt of snapshot.receipts) this.receipts.set(receipt.receiptId, cloneJson(receipt));
  }

  private async deliver(decision: PermissionDecisionRecord, signal?: AbortSignal): Promise<PermissionApprovalEnvelope> {
    const continuation = this.evaluator.continuations.get(decision.continuationRequestId!);
    if (!continuation) throw approvalError("continuation_not_found", `permission continuation ${decision.continuationRequestId} was not found`);
    const envelope = createEnvelope(decision, continuation, this.timestamp());
    this.envelopes.set(envelope.envelopeId, envelope);
    this.byRequest.set(envelope.requestId, envelope.envelopeId);
    this.revision += 1;
    try {
      envelope.deliveryAttempt += 1;
      const result = await this.transport(cloneJson(envelope), signal);
      const deliveredAt = this.timestamp();
      const receiptBase = {
        envelopeId: envelope.envelopeId,
        requestId: envelope.requestId,
        transport: result.transport,
        transportRequestId: result.transportRequestId,
        accepted: result.accepted,
        deliveredAt,
        responseDigest: result.responseDigest ?? null,
        failure: null,
        metadata: cloneJson(result.metadata ?? {}),
      };
      const receipt: PermissionApprovalDeliveryReceipt = {
        receiptId: deterministicId("permission-approval-delivery", receiptBase, 40),
        ...receiptBase,
      };
      this.receipts.set(receipt.receiptId, receipt);
      envelope.deliveryReceiptId = receipt.receiptId;
      envelope.status = result.accepted ? "delivered" : "delivery_failed";
      envelope.updatedAt = deliveredAt;
      this.revision += 1;
      return cloneJson(envelope);
    } catch (error) {
      const deliveredAt = this.timestamp();
      const failure = { name: error instanceof Error ? error.name : "Error", message: error instanceof Error ? error.message : String(error) };
      const receiptBase = {
        envelopeId: envelope.envelopeId,
        requestId: envelope.requestId,
        transport: "failed",
        transportRequestId: "",
        accepted: false,
        deliveredAt,
        responseDigest: null,
        failure,
        metadata: {},
      };
      const receipt: PermissionApprovalDeliveryReceipt = { receiptId: deterministicId("permission-approval-delivery", receiptBase, 40), ...receiptBase };
      this.receipts.set(receipt.receiptId, receipt);
      envelope.deliveryReceiptId = receipt.receiptId;
      envelope.status = "delivery_failed";
      envelope.updatedAt = deliveredAt;
      envelope.metadata = { ...envelope.metadata, delivery_failure: failure };
      this.revision += 1;
      throw error;
    }
  }

  private requireRequest(requestId: string): PermissionApprovalEnvelope {
    const id = this.byRequest.get(requestId);
    if (!id) throw approvalError("approval_envelope_not_found", `approval request ${requestId} was not found`);
    return this.envelopes.get(id)!;
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function createEnvelope(decision: PermissionDecisionRecord, continuation: PermissionContinuationRecord, timestamp: string): PermissionApprovalEnvelope {
  const binding = decision.requestBinding;
  const base = {
    requestId: continuation.requestId,
    decisionId: decision.decisionId,
    runId: continuation.runId,
    taskId: continuation.taskId,
    sessionId: continuation.sessionId,
    sessionRevision: continuation.sessionRevision,
    workerRequestId: continuation.workerRequestId,
    toolCallId: continuation.toolCallId,
    prompt: `Allow ${text(binding.tool_name)} for this request?`,
    reason: decision.reason,
    toolName: text(binding.tool_name),
    namespace: text(binding.namespace),
    serverId: text(binding.server_id),
    operation: text(binding.operation),
    argumentsDigest: decision.finalArgumentsDigest,
    finalArguments: cloneJson(decision.finalArguments),
    policyRevision: decision.policyRevision,
    modeRevision: decision.modeRevision,
    expiresAt: continuation.expiresAt,
    status: "pending_delivery" as const,
    deliveryAttempt: 0,
    deliveryReceiptId: null,
    createdAt: timestamp,
    updatedAt: timestamp,
    metadata: { request_fingerprint: decision.requestFingerprint, canonical_owner: "typescript" },
  };
  return { envelopeId: deterministicId("permission-approval-envelope", base, 40), ...base };
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function approvalError(code: string, message: string): Error {
  return Object.assign(new Error(message), { name: "PermissionApprovalError", code });
}
