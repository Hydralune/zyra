import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type { PermissionApprovalResponse, PermissionDecisionRecord } from "../e02/contracts.ts";

export type PermissionAuditKind = "evaluated" | "blocked" | "allowed" | "approval_requested" | "approval_resumed" | "approval_rejected" | "policy_reloaded" | "mode_changed" | "grant_changed";

export interface PermissionAuditRecord {
  auditId: string;
  sequence: number;
  kind: PermissionAuditKind;
  runId: string;
  taskId: string;
  sessionId: string;
  workerRequestId: string;
  toolCallId: string | null;
  decisionId: string | null;
  requestId: string | null;
  effect: string | null;
  reasonCode: string;
  stateDigest: string;
  details: JsonObject;
  occurredAt: string;
  previousHash: string;
  auditHash: string;
}

export interface PermissionAuditSnapshot {
  version: "zyra.permission-audit/v1";
  sequence: number;
  headHash: string;
  records: PermissionAuditRecord[];
  digest: string;
  capturedAt: string;
}

const genesis = "sha256:zyra-permission-audit-genesis";

export class PermissionAuditRuntime {
  private readonly records: PermissionAuditRecord[] = [];
  private readonly now: () => Date;
  private readonly maximumRecords: number;
  private sequence = 0;
  private headHash = genesis;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumRecords?: number; snapshot?: PermissionAuditSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumRecords = options.maximumRecords ?? 100_000;
    if (options.snapshot) this.restore(options.snapshot);
  }

  decision(record: PermissionDecisionRecord): PermissionAuditRecord {
    const binding = record.requestBinding;
    const kind: PermissionAuditKind = record.effect === "allow" ? "allowed" : record.effect === "ask" ? "approval_requested" : "blocked";
    return this.append({
      kind,
      runId: text(binding.run_id),
      taskId: text(binding.task_id),
      sessionId: text(binding.session_id),
      workerRequestId: text(binding.worker_request_id),
      toolCallId: text(binding.tool_call_id) || null,
      decisionId: record.decisionId,
      requestId: record.continuationRequestId,
      effect: record.effect,
      reasonCode: record.reasonCode,
      stateDigest: digest(record),
      details: {
        event_type: "permission.decision.recorded",
        decision_id: record.decisionId,
        disposition: record.effect,
        mutation_id: record.decisionId,
        revision: this.sequence + 1,
        policy_revision: record.policyRevision,
        mode_revision: record.modeRevision,
        request_fingerprint: record.requestFingerprint,
        original_arguments_digest: record.originalArgumentsDigest,
        final_arguments_digest: record.finalArgumentsDigest,
        hook_audits: record.hooks,
        risk: record.risk,
        replan_required: record.replanRequired,
        recovery_input: record.recoveryInput,
        canonical_owner: record.canonicalOwner,
      },
    });
  }

  approval(response: PermissionApprovalResponse, accepted: boolean, decision: PermissionDecisionRecord | null, reasonCode: string): PermissionAuditRecord {
    return this.append({
      kind: accepted ? "approval_resumed" : "approval_rejected",
      runId: response.runId,
      taskId: decision ? text(decision.requestBinding.task_id) : "",
      sessionId: response.sessionId,
      workerRequestId: response.workerRequestId,
      toolCallId: response.toolCallId,
      decisionId: decision?.decisionId ?? null,
      requestId: response.requestId,
      effect: response.effect,
      reasonCode,
      stateDigest: digest({ response, accepted, decision }),
      details: {
        response_id: response.responseId,
        session_revision: response.sessionRevision,
        responder: response.responder,
        accepted,
      },
    });
  }

  append(input: Omit<PermissionAuditRecord, "auditId" | "sequence" | "occurredAt" | "previousHash" | "auditHash">): PermissionAuditRecord {
    this.sequence += 1;
    const occurredAt = this.timestamp();
    const base = {
      sequence: this.sequence,
      ...cloneJson(input),
      occurredAt,
      previousHash: this.headHash,
    };
    const auditId = deterministicId("permission-audit", base, 40);
    const record: PermissionAuditRecord = {
      auditId,
      ...base,
      auditHash: digest({ auditId, ...base }),
    };
    this.records.push(record);
    this.headHash = record.auditHash;
    while (this.records.length > this.maximumRecords) this.records.shift();
    return cloneJson(record);
  }

  query(options: { sessionId?: string; toolCallId?: string; decisionId?: string; kind?: PermissionAuditKind; afterSequence?: number; limit?: number } = {}): PermissionAuditRecord[] {
    const values = this.records
      .filter((record) => !options.sessionId || record.sessionId === options.sessionId)
      .filter((record) => !options.toolCallId || record.toolCallId === options.toolCallId)
      .filter((record) => !options.decisionId || record.decisionId === options.decisionId)
      .filter((record) => !options.kind || record.kind === options.kind)
      .filter((record) => !options.afterSequence || record.sequence > options.afterSequence);
    return values.slice(0, options.limit ?? values.length).map(cloneJson);
  }

  snapshot(): PermissionAuditSnapshot {
    const withoutDigest = {
      version: "zyra.permission-audit/v1" as const,
      sequence: this.sequence,
      headHash: this.headHash,
      records: this.records.map(cloneJson),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: PermissionAuditSnapshot): void {
    if (snapshot.version !== "zyra.permission-audit/v1") throw new Error("unsupported permission audit snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw new Error("permission audit snapshot digest mismatch");
    let head = genesis;
    let sequence = 0;
    for (const record of snapshot.records) {
      if (record.sequence <= sequence) throw new Error("permission audit sequence is not monotonic");
      if (record.previousHash !== head) throw new Error("permission audit hash chain is broken");
      const { auditHash, ...payload } = record;
      if (digest(payload) !== auditHash) throw new Error(`permission audit record ${record.auditId} hash mismatch`);
      head = record.auditHash;
      sequence = record.sequence;
    }
    if (snapshot.records.length && head !== snapshot.headHash) throw new Error("permission audit head hash mismatch");
    this.records.splice(0);
    for (const record of snapshot.records.slice(-this.maximumRecords)) this.records.push(cloneJson(record));
    this.sequence = snapshot.sequence;
    this.headHash = snapshot.headHash;
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}
