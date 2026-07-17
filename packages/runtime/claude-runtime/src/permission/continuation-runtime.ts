import type { JsonObject } from "../contracts.ts";
import {
  cloneJson,
  constantTimeDigestEquals,
  deterministicId,
  digest,
} from "../e02/canonical.ts";
import type {
  E02Clock,
  PermissionApprovalResponse,
  PermissionContinuationRecord,
  PermissionDecisionRecord,
} from "../e02/contracts.ts";
import type { PermissionIdentityRecord } from "./model.ts";
import { PermissionIdentity } from "./model.ts";

export interface PermissionContinuationResumeResult extends JsonObject {
  continuation: PermissionContinuationRecord;
  accepted: boolean;
  effect: "allow" | "deny";
  responseDigest: string;
  exactBinding: boolean;
}
export class PermissionContinuationRuntime {
  private readonly clock: E02Clock;
  private readonly records = new Map<string, PermissionContinuationRecord>();
  private readonly identities = new Map<string, PermissionIdentityRecord>();
  private readonly responseIds = new Map<string, string>();
  private revisionValue = 0;

  constructor(clock: E02Clock = () => new Date().toISOString()) {
    this.clock = clock;
  }

  get revision(): number {
    return this.revisionValue;
  }

  park(
    identity: PermissionIdentityRecord,
    decision: PermissionDecisionRecord,
    options: { ttlMs: number; metadata?: JsonObject; expectedRevision?: number },
  ): PermissionContinuationRecord {
    this.assertRevision(options.expectedRevision ?? this.revisionValue);
    if (decision.effect !== "ask") throw new Error("only ASK decisions can create a permission continuation");
    if (decision.requestFingerprint !== identity.requestFingerprint) throw new Error("permission continuation decision fingerprint mismatch");
    if (!Number.isSafeInteger(options.ttlMs) || options.ttlMs < 1_000 || options.ttlMs > 86_400_000) {
      throw new Error("permission continuation TTL must be between 1 second and 24 hours");
    }
    const requestId = deterministicId("permission-request", {
      decisionId: decision.decisionId,
      requestFingerprint: identity.requestFingerprint,
      policyRevision: decision.policyRevision,
      modeRevision: decision.modeRevision,
    }, 40);
    const existing = this.records.get(requestId);
    if (existing) {
      if (existing.requestFingerprint !== identity.requestFingerprint || existing.decisionId !== decision.decisionId) {
        throw new Error(`permission continuation id collision ${requestId}`);
      }
      return cloneJson(existing);
    }
    const createdAt = this.clock();
    const continuation: PermissionContinuationRecord = {
      requestId,
      decisionId: decision.decisionId,
      requestFingerprint: identity.requestFingerprint,
      runId: identity.context.runId,
      taskId: identity.context.taskId,
      sessionId: identity.context.sessionId,
      sessionRevision: identity.context.sessionRevision,
      workerRequestId: identity.context.workerRequestId,
      toolCallId: identity.context.toolCallId,
      policyRevision: decision.policyRevision,
      modeRevision: decision.modeRevision,
      argumentsDigest: identity.argumentsDigest,
      status: "pending",
      createdAt,
      expiresAt: new Date(Date.parse(createdAt) + options.ttlMs).toISOString(),
      respondedAt: null,
      consumedAt: null,
      responseId: null,
      responder: null,
      responseDigest: null,
      metadata: cloneJson(options.metadata ?? {}),
    };
    this.records.set(requestId, continuation);
    this.identities.set(requestId, cloneJson(identity));
    this.revisionValue += 1;
    return cloneJson(continuation);
  }

  resume(
    response: PermissionApprovalResponse,
    current: { policyRevision: number; modeRevision: number; expectedRevision?: number },
  ): PermissionContinuationResumeResult {
    this.assertRevision(current.expectedRevision ?? this.revisionValue);
    const priorRequest = this.responseIds.get(response.responseId);
    if (priorRequest) {
      if (priorRequest !== response.requestId) throw new Error("permission response id was reused for a different request");
      const replay = this.records.get(priorRequest);
      if (!replay || replay.responseDigest !== digest(response)) throw new Error("permission duplicate response payload mismatch");
      return {
        continuation: cloneJson(replay),
        accepted: replay.status === "approved" || replay.status === "denied" || replay.status === "consumed",
        effect: response.effect,
        responseDigest: replay.responseDigest,
        exactBinding: true,
      };
    }
    const continuation = this.records.get(response.requestId);
    if (!continuation) return this.rejectResponse(response, "unknown_request", "permission request does not exist");
    if (continuation.status !== "pending") {
      return this.rejectResponse(response, "request_not_pending", `permission request is ${continuation.status}`);
    }
    if (Date.parse(continuation.expiresAt) <= Date.parse(response.respondedAt || this.clock())) {
      continuation.status = "expired";
      continuation.respondedAt = response.respondedAt;
      this.revisionValue += 1;
      return this.rejectResponse(response, "request_expired", "permission response arrived after expiry");
    }
    const identity = this.identities.get(response.requestId);
    if (!identity) throw new Error(`permission continuation ${response.requestId} has no identity binding`);
    try {
      PermissionIdentity.assertResponseBinding(identity, response);
    } catch (error) {
      return this.rejectResponse(response, "response_binding_mismatch", error instanceof Error ? error.message : String(error));
    }
    if (current.policyRevision !== continuation.policyRevision) {
      return this.rejectResponse(response, "policy_revision_mismatch", "permission policy changed while approval was pending");
    }
    if (current.modeRevision !== continuation.modeRevision) {
      return this.rejectResponse(response, "mode_revision_mismatch", "permission mode changed while approval was pending");
    }
    const responseDigest = digest(response);
    continuation.status = response.effect === "allow" ? "approved" : "denied";
    continuation.respondedAt = response.respondedAt || this.clock();
    continuation.responseId = response.responseId;
    continuation.responder = response.responder;
    continuation.responseDigest = responseDigest;
    continuation.metadata = { ...continuation.metadata, response_metadata: response.metadata };
    this.responseIds.set(response.responseId, response.requestId);
    this.revisionValue += 1;
    return {
      continuation: cloneJson(continuation),
      accepted: true,
      effect: response.effect,
      responseDigest,
      exactBinding: true,
    };
  }

  rejectResponse(
    response: PermissionApprovalResponse,
    code: string,
    message: string,
  ): PermissionContinuationResumeResult {
    const continuation = this.records.get(response.requestId);
    const responseDigest = digest(response);
    if (continuation) {
      continuation.metadata = {
        ...continuation.metadata,
        last_rejected_response: {
          code,
          message,
          response_id: response.responseId,
          response_digest: responseDigest,
          rejected_at: this.clock(),
        },
      };
    }
    return {
      continuation: continuation ? cloneJson(continuation) : syntheticRejected(response, code, message),
      accepted: false,
      effect: response.effect,
      responseDigest,
      exactBinding: false,
    };
  }

  consume(
    requestId: string,
    expectedStatus: "approved" | "denied",
    expectedRevision = this.revisionValue,
  ): PermissionContinuationRecord {
    this.assertRevision(expectedRevision);
    const continuation = this.records.get(requestId);
    if (!continuation) throw new Error(`unknown permission continuation ${requestId}`);
    if (continuation.status === "consumed") return cloneJson(continuation);
    if (continuation.status !== expectedStatus) {
      throw new Error(`permission continuation ${requestId} expected ${expectedStatus}, observed ${continuation.status}`);
    }
    continuation.status = "consumed";
    continuation.consumedAt = this.clock();
    this.revisionValue += 1;
    return cloneJson(continuation);
  }

  cancel(requestId: string, reason: string, expectedRevision = this.revisionValue): PermissionContinuationRecord {
    this.assertRevision(expectedRevision);
    const continuation = this.records.get(requestId);
    if (!continuation) throw new Error(`unknown permission continuation ${requestId}`);
    if (continuation.status === "pending") {
      continuation.status = "cancelled";
      continuation.metadata = { ...continuation.metadata, cancellation_reason: reason };
      this.revisionValue += 1;
    }
    return cloneJson(continuation);
  }

  expire(now = this.clock()): PermissionContinuationRecord[] {
    const expired: PermissionContinuationRecord[] = [];
    for (const continuation of this.records.values()) {
      if (continuation.status === "pending" && Date.parse(continuation.expiresAt) <= Date.parse(now)) {
        continuation.status = "expired";
        continuation.respondedAt = now;
        expired.push(cloneJson(continuation));
        this.revisionValue += 1;
      }
    }
    return expired;
  }

  get(requestId: string): PermissionContinuationRecord | null {
    const continuation = this.records.get(requestId);
    return continuation ? cloneJson(continuation) : null;
  }

  pending(): PermissionContinuationRecord[] {
    return [...this.records.values()]
      .filter((record) => record.status === "pending")
      .map(cloneJson)
      .sort((left, right) => left.createdAt.localeCompare(right.createdAt));
  }

  snapshot(): JsonObject {
    const base: JsonObject = {
      version: "zyra.e02-permission-continuations/v1",
      revision: this.revisionValue,
      records: [...this.records.values()].map(cloneJson).sort((left, right) => left.requestId.localeCompare(right.requestId)),
      identities: [...this.identities.entries()].map(([id, identity]) => [id, cloneJson(identity)]),
      response_ids: [...this.responseIds.entries()].sort(([left], [right]) => left.localeCompare(right)),
    };
    return { ...base, snapshot_hash: digest(base) };
  }

  restore(snapshot: JsonObject): void {
    if (snapshot.version !== "zyra.e02-permission-continuations/v1") throw new Error("unsupported permission continuation snapshot");
    const expectedHash = digest({
      version: snapshot.version,
      revision: snapshot.revision,
      records: snapshot.records,
      identities: snapshot.identities,
      response_ids: snapshot.response_ids,
    });
    if (!constantTimeDigestEquals(expectedHash, String(snapshot.snapshot_hash ?? ""))) throw new Error("permission continuation snapshot hash mismatch");
    const revision = Number(snapshot.revision);
    if (!Number.isSafeInteger(revision) || revision < 0) throw new Error("permission continuation revision is invalid");
    this.records.clear();
    this.identities.clear();
    this.responseIds.clear();
    for (const record of Array.isArray(snapshot.records) ? snapshot.records as unknown as PermissionContinuationRecord[] : []) {
      if (this.records.has(record.requestId)) throw new Error(`duplicate permission continuation ${record.requestId}`);
      this.records.set(record.requestId, cloneJson(record));
    }
    for (const pair of Array.isArray(snapshot.identities) ? snapshot.identities as unknown as Array<[string, PermissionIdentityRecord]> : []) {
      this.identities.set(pair[0], cloneJson(pair[1]));
    }
    for (const pair of Array.isArray(snapshot.response_ids) ? snapshot.response_ids as unknown as Array<[string, string]> : []) {
      this.responseIds.set(pair[0], pair[1]);
    }
    for (const requestId of this.records.keys()) {
      if (!this.identities.has(requestId)) throw new Error(`permission continuation ${requestId} lacks restored identity`);
    }
    this.revisionValue = revision;
  }

  private assertRevision(expectedRevision: number): void {
    if (expectedRevision !== this.revisionValue) {
      throw new Error(`permission continuation revision conflict: expected ${expectedRevision}, observed ${this.revisionValue}`);
    }
  }
}

function syntheticRejected(response: PermissionApprovalResponse, code: string, message: string): PermissionContinuationRecord {
  return {
    requestId: response.requestId,
    decisionId: "",
    requestFingerprint: "",
    runId: response.runId,
    taskId: "",
    sessionId: response.sessionId,
    sessionRevision: response.sessionRevision,
    workerRequestId: response.workerRequestId,
    toolCallId: response.toolCallId,
    policyRevision: 0,
    modeRevision: 0,
    argumentsDigest: "",
    status: "cancelled",
    createdAt: response.respondedAt,
    expiresAt: response.respondedAt,
    respondedAt: response.respondedAt,
    consumedAt: null,
    responseId: response.responseId,
    responder: response.responder,
    responseDigest: digest(response),
    metadata: { rejection_code: code, rejection_message: message },
  };
}
