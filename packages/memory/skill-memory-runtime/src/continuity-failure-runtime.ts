import {
  cloneJson,
  contractError,
  digest,
  normalizeIdentity,
  nowIso,
  stableId,
  uniqueStrings,
  type JsonObject,
  type RuntimeIdentity,
} from "./contracts.ts";

export const CONTINUITY_FAILURE_PROTOCOL = "zyra.skill-memory-continuity-failure/v1" as const;

export type ContinuityFailureStage =
  | "compact_trigger"
  | "safe_cut"
  | "archive_commit"
  | "retrieval_composition"
  | "restore_fidelity"
  | "context_projection"
  | "provider_delivery"
  | "browser_delivery"
  | "authority_revalidation"
  | "resume_verification";

export type ContinuityRecoveryDisposition =
  | "retry_text_reference"
  | "reroute_canonical_checkpoint"
  | "replan_without_skill_memory"
  | "deny_resume"
  | "operator_diagnostic";

export type ContinuityFailureState =
  | "recorded"
  | "recovery_claimed"
  | "recovered"
  | "terminal";

export interface ContinuityFailureInput {
  identity: RuntimeIdentity;
  boundaryId: string;
  stage: ContinuityFailureStage;
  code: string;
  message: string;
  retryable: boolean;
  baselineTextReferenceAvailable: boolean;
  canonicalCheckpointAvailable: boolean;
  currentAuthorityRequired: boolean;
  provenanceComplete: boolean;
  relatedIds?: string[];
  details?: JsonObject;
  causationId?: string;
  occurredAt?: string;
}

export interface ContinuityFailureRecord {
  failureId: string;
  protocol: typeof CONTINUITY_FAILURE_PROTOCOL;
  identity: RuntimeIdentity;
  boundaryId: string;
  stage: ContinuityFailureStage;
  code: string;
  message: string;
  retryable: boolean;
  attempt: number;
  state: ContinuityFailureState;
  disposition: ContinuityRecoveryDisposition;
  baselineTextReferenceAvailable: boolean;
  canonicalCheckpointAvailable: boolean;
  currentAuthorityRequired: boolean;
  provenanceComplete: boolean;
  modelCanOverride: false;
  relatedIds: string[];
  causationId: string;
  occurredAt: string;
  terminalAt: string | null;
  details: JsonObject;
  failureDigest: string;
}

export interface ContinuityRecoveryClaim {
  claimId: string;
  failureId: string;
  workerRequestId: string;
  disposition: ContinuityRecoveryDisposition;
  state: "claimed" | "committed" | "released";
  leaseEpoch: number;
  claimedAt: string;
  settledAt: string | null;
  terminalEventIds: string[];
  reason: string;
  claimDigest: string;
}

export interface ContinuityFailureAudit {
  auditId: string;
  failureId: string;
  valid: boolean;
  findings: string[];
  fallbackExplicit: boolean;
  authorityFailClosed: boolean;
  provenanceFailClosed: boolean;
  modelOverrideBlocked: true;
  auditedAt: string;
  auditDigest: string;
}

export interface ContinuityFailureSnapshot {
  version: "zyra.skill-memory-continuity-failure-runtime/v1";
  identity: RuntimeIdentity;
  revision: number;
  failures: ContinuityFailureRecord[];
  claims: ContinuityRecoveryClaim[];
  audits: ContinuityFailureAudit[];
  capturedAt: string;
  checksum: string;
}

/**
 * Durable, deterministic failure routing for the 06C integration boundary.
 * It emits recovery intent only. Scheduler/recovery owners consume the intent;
 * this runtime never performs a route, retries a tool, or mutates a checkpoint.
 */
export class SkillMemoryContinuityFailureRuntime {
  readonly identity: RuntimeIdentity;
  private readonly failures = new Map<string, ContinuityFailureRecord>();
  private readonly claims = new Map<string, ContinuityRecoveryClaim>();
  private readonly audits = new Map<string, ContinuityFailureAudit>();
  private readonly now: () => Date;
  private revision = 0;

  constructor(options: {
    identity: RuntimeIdentity;
    now?: () => Date;
    snapshot?: ContinuityFailureSnapshot | null;
  }) {
    this.identity = normalizeIdentity(options.identity);
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  record(inputValue: ContinuityFailureInput): ContinuityFailureRecord {
    this.assertEnabled();
    const input = normalizeFailureInput(inputValue, this.identity);
    const attempt = this.stageAttempts(input.boundaryId, input.stage, input.code) + 1;
    const disposition = dispositionFor(input, attempt);
    const occurredAt = input.occurredAt || nowIso(this.now);
    const unsigned = {
      failureId: stableId(
        "skill-memory-continuity-failure",
        input.identity.taskId,
        input.identity.sessionId,
        input.boundaryId,
        input.stage,
        input.code,
        attempt,
        input.causationId,
      ),
      protocol: CONTINUITY_FAILURE_PROTOCOL,
      identity: cloneJson(input.identity),
      boundaryId: input.boundaryId,
      stage: input.stage,
      code: input.code,
      message: input.message,
      retryable: input.retryable,
      attempt,
      state: "recorded" as const,
      disposition,
      baselineTextReferenceAvailable: input.baselineTextReferenceAvailable,
      canonicalCheckpointAvailable: input.canonicalCheckpointAvailable,
      currentAuthorityRequired: input.currentAuthorityRequired,
      provenanceComplete: input.provenanceComplete,
      modelCanOverride: false as const,
      relatedIds: cloneJson(input.relatedIds),
      causationId: input.causationId,
      occurredAt,
      terminalAt: null,
      details: {
        ...input.details,
        sealed_policy_deterministic: true,
        scheduler_action_performed: false,
        recovery_action_performed: false,
      },
    } satisfies Omit<ContinuityFailureRecord, "failureDigest">;
    const record: ContinuityFailureRecord = { ...unsigned, failureDigest: digest(unsigned) };
    const existing = this.failures.get(record.failureId);
    if (existing && existing.failureDigest !== record.failureDigest) {
      throw contractError("continuity_failure_conflict", `continuity failure id conflicts: ${record.failureId}`);
    }
    this.failures.set(record.failureId, existing ?? record);
    if (!existing) this.revision += 1;
    this.audit(record.failureId);
    return cloneJson(existing ?? record);
  }

  claim(input: {
    failureId: string;
    workerRequestId: string;
    expectedDisposition?: ContinuityRecoveryDisposition;
  }): ContinuityRecoveryClaim {
    this.assertEnabled();
    const failure = this.requireFailure(input.failureId);
    if (failure.state === "recovered" || failure.state === "terminal") {
      throw contractError("continuity_failure_terminal", `continuity failure is already terminal: ${failure.failureId}`);
    }
    if (input.expectedDisposition && input.expectedDisposition !== failure.disposition) {
      throw contractError("continuity_failure_disposition", "recovery consumer expected another disposition");
    }
    const workerRequestId = input.workerRequestId.trim();
    if (!workerRequestId) throw contractError("continuity_failure_worker", "recovery claim requires worker request id");
    const existing = [...this.claims.values()].find((item) =>
      item.failureId === failure.failureId && item.state === "claimed"
    );
    if (existing) {
      if (existing.workerRequestId !== workerRequestId) {
        throw contractError("continuity_failure_claimed", "continuity failure is claimed by another worker request");
      }
      return cloneJson(existing);
    }
    const claimedAt = nowIso(this.now);
    const leaseEpoch = 1 + Math.max(
      0,
      ...[...this.claims.values()]
        .filter((item) => item.failureId === failure.failureId)
        .map((item) => item.leaseEpoch),
    );
    const unsigned = {
      claimId: stableId("continuity-recovery-claim", failure.failureId, workerRequestId, leaseEpoch),
      failureId: failure.failureId,
      workerRequestId,
      disposition: failure.disposition,
      state: "claimed" as const,
      leaseEpoch,
      claimedAt,
      settledAt: null,
      terminalEventIds: [],
      reason: "deterministic_recovery_claimed",
    } satisfies Omit<ContinuityRecoveryClaim, "claimDigest">;
    const claim: ContinuityRecoveryClaim = { ...unsigned, claimDigest: digest(unsigned) };
    this.claims.set(claim.claimId, claim);
    this.replaceFailure({ ...failure, state: "recovery_claimed" });
    this.revision += 1;
    return cloneJson(claim);
  }

  settle(input: {
    claimId: string;
    committed: boolean;
    terminalEventIds?: string[];
    reason?: string;
  }): ContinuityRecoveryClaim {
    this.assertEnabled();
    const claim = this.claims.get(input.claimId.trim());
    if (!claim) throw contractError("continuity_recovery_claim_missing", `recovery claim not found: ${input.claimId}`);
    const expected = input.committed ? "committed" : "released";
    if (claim.state !== "claimed") {
      if (claim.state !== expected) throw contractError("continuity_recovery_settlement_conflict", "recovery claim settled differently");
      return cloneJson(claim);
    }
    const settled: ContinuityRecoveryClaim = {
      ...claim,
      state: expected,
      settledAt: nowIso(this.now),
      terminalEventIds: uniqueStrings(input.terminalEventIds),
      reason: input.reason?.trim() || (input.committed ? "recovery_evidence_committed" : "recovery_claim_released"),
      claimDigest: "",
    };
    const { claimDigest: _ignored, ...unsigned } = settled;
    settled.claimDigest = digest(unsigned);
    this.claims.set(settled.claimId, settled);
    const failure = this.requireFailure(settled.failureId);
    this.replaceFailure({
      ...failure,
      state: input.committed ? "recovered" : "recorded",
      terminalAt: input.committed ? settled.settledAt : null,
    });
    this.revision += 1;
    this.audit(failure.failureId);
    return cloneJson(settled);
  }

  terminate(input: { failureId: string; reason: string; terminalEventIds?: string[] }): ContinuityFailureRecord {
    this.assertEnabled();
    const failure = this.requireFailure(input.failureId);
    const terminal = this.replaceFailure({
      ...failure,
      state: "terminal",
      terminalAt: nowIso(this.now),
      details: {
        ...failure.details,
        terminal_reason: input.reason.trim() || "continuity_failure_terminal",
        terminal_event_ids: uniqueStrings(input.terminalEventIds),
      },
    });
    this.revision += 1;
    this.audit(terminal.failureId);
    return terminal;
  }

  audit(failureIdValue: string): ContinuityFailureAudit {
    const failure = this.requireFailure(failureIdValue);
    const findings: string[] = [];
    const fallbackExplicit = failure.baselineTextReferenceAvailable
      ? ["retry_text_reference", "reroute_canonical_checkpoint"].includes(failure.disposition)
      : true;
    const authorityFailClosed = failure.stage !== "authority_revalidation"
      || ["replan_without_skill_memory", "deny_resume", "reroute_canonical_checkpoint"].includes(failure.disposition);
    const provenanceFailClosed = failure.provenanceComplete
      || ["replan_without_skill_memory", "deny_resume", "reroute_canonical_checkpoint"].includes(failure.disposition);
    if (!fallbackExplicit) findings.push("text_reference_fallback_not_explicit");
    if (!authorityFailClosed) findings.push("authority_failure_not_fail_closed");
    if (!provenanceFailClosed) findings.push("missing_provenance_not_fail_closed");
    if (failure.modelCanOverride !== false) findings.push("model_override_not_blocked");
    if (failure.disposition === "retry_text_reference" && !failure.retryable) findings.push("non_retryable_failure_marked_retry");
    if (failure.attempt >= 3 && failure.disposition === "retry_text_reference") findings.push("retry_circuit_not_open");
    const auditedAt = nowIso(this.now);
    const unsigned = {
      auditId: stableId("continuity-failure-audit", failure.failureId, failure.failureDigest),
      failureId: failure.failureId,
      valid: findings.length === 0,
      findings,
      fallbackExplicit,
      authorityFailClosed,
      provenanceFailClosed,
      modelOverrideBlocked: true as const,
      auditedAt,
    } satisfies Omit<ContinuityFailureAudit, "auditDigest">;
    const audit: ContinuityFailureAudit = { ...unsigned, auditDigest: digest(unsigned) };
    this.audits.set(audit.auditId, audit);
    return cloneJson(audit);
  }

  event(failureIdValue: string): JsonObject {
    const failure = this.requireFailure(failureIdValue);
    const audit = this.audit(failure.failureId);
    return {
      protocol: CONTINUITY_FAILURE_PROTOCOL,
      kind: "skill_memory_continuity_failure",
      failure_id: failure.failureId,
      compact_boundary_id: failure.boundaryId,
      stage: failure.stage,
      code: failure.code,
      attempt: failure.attempt,
      state: failure.state,
      disposition: failure.disposition,
      retryable: failure.retryable,
      text_reference_fallback_available: failure.baselineTextReferenceAvailable,
      canonical_checkpoint_available: failure.canonicalCheckpointAvailable,
      current_authority_required: failure.currentAuthorityRequired,
      provenance_complete: failure.provenanceComplete,
      model_can_override: false,
      audit_id: audit.auditId,
      audit_valid: audit.valid,
      routing_consumer_required: true,
      recovery_consumer_required: true,
      scheduler_action_performed: false,
      recovery_action_performed: false,
    };
  }

  list(options: {
    boundaryId?: string;
    stage?: ContinuityFailureStage;
    state?: ContinuityFailureState;
    limit?: number;
  } = {}): ContinuityFailureRecord[] {
    return [...this.failures.values()]
      .filter((item) => !options.boundaryId || item.boundaryId === options.boundaryId)
      .filter((item) => !options.stage || item.stage === options.stage)
      .filter((item) => !options.state || item.state === options.state)
      .sort((left, right) => right.occurredAt.localeCompare(left.occurredAt) || right.attempt - left.attempt)
      .slice(0, Math.max(0, Math.min(options.limit ?? 1_000, 100_000)))
      .map(cloneJson);
  }

  health(): JsonObject {
    const failures = [...this.failures.values()];
    const claims = [...this.claims.values()];
    return {
      protocol: CONTINUITY_FAILURE_PROTOCOL,
      revision: this.revision,
      failure_count: failures.length,
      recorded_count: failures.filter((item) => item.state === "recorded").length,
      recovery_claimed_count: failures.filter((item) => item.state === "recovery_claimed").length,
      recovered_count: failures.filter((item) => item.state === "recovered").length,
      terminal_count: failures.filter((item) => item.state === "terminal").length,
      open_claim_count: claims.filter((item) => item.state === "claimed").length,
      invalid_audit_count: [...this.audits.values()].filter((item) => !item.valid).length,
      scheduler_owner_preserved: true,
      recovery_owner_preserved: true,
      model_can_override: false,
    };
  }

  snapshot(): ContinuityFailureSnapshot {
    const unsigned: Omit<ContinuityFailureSnapshot, "checksum"> = {
      version: "zyra.skill-memory-continuity-failure-runtime/v1",
      identity: cloneJson(this.identity),
      revision: this.revision,
      failures: this.list({ limit: 100_000 }).sort((left, right) => left.failureId.localeCompare(right.failureId)),
      claims: [...this.claims.values()].sort((left, right) => left.claimId.localeCompare(right.claimId)).map(cloneJson),
      audits: [...this.audits.values()].sort((left, right) => left.auditId.localeCompare(right.auditId)).map(cloneJson),
      capturedAt: nowIso(this.now),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshotValue: ContinuityFailureSnapshot): void {
    if (snapshotValue.version !== "zyra.skill-memory-continuity-failure-runtime/v1") {
      throw contractError("continuity_failure_snapshot_version", "unsupported continuity failure snapshot version");
    }
    const { checksum, ...unsigned } = snapshotValue;
    if (digest(unsigned) !== checksum) throw contractError("continuity_failure_snapshot_checksum", "continuity failure snapshot checksum mismatch");
    const identity = normalizeIdentity(snapshotValue.identity);
    if (identity.taskId !== this.identity.taskId || identity.sessionId !== this.identity.sessionId) {
      throw contractError("continuity_failure_snapshot_binding", "continuity failure snapshot belongs to another task/session");
    }
    const failures = new Map<string, ContinuityFailureRecord>();
    for (const failure of snapshotValue.failures) {
      const { failureDigest, ...failureUnsigned } = failure;
      if (digest(failureUnsigned) !== failureDigest) throw contractError("continuity_failure_digest", `continuity failure digest mismatch: ${failure.failureId}`);
      failures.set(failure.failureId, cloneJson(failure));
    }
    const claims = new Map<string, ContinuityRecoveryClaim>();
    for (const claim of snapshotValue.claims) {
      const { claimDigest, ...claimUnsigned } = claim;
      if (digest(claimUnsigned) !== claimDigest) throw contractError("continuity_recovery_claim_digest", `continuity claim digest mismatch: ${claim.claimId}`);
      if (!failures.has(claim.failureId)) throw contractError("continuity_recovery_failure_missing", "continuity claim references missing failure");
      claims.set(claim.claimId, cloneJson(claim));
    }
    const audits = new Map<string, ContinuityFailureAudit>();
    for (const audit of snapshotValue.audits) {
      const { auditDigest, ...auditUnsigned } = audit;
      if (digest(auditUnsigned) !== auditDigest) throw contractError("continuity_failure_audit_digest", `continuity audit digest mismatch: ${audit.auditId}`);
      if (!failures.has(audit.failureId)) throw contractError("continuity_failure_audit_missing", "continuity audit references missing failure");
      audits.set(audit.auditId, cloneJson(audit));
    }
    this.failures.clear();
    this.claims.clear();
    this.audits.clear();
    for (const [key, value] of failures) this.failures.set(key, value);
    for (const [key, value] of claims) this.claims.set(key, value);
    for (const [key, value] of audits) this.audits.set(key, value);
    this.revision = snapshotValue.revision;
  }

  private stageAttempts(boundaryId: string, stage: ContinuityFailureStage, code: string): number {
    return [...this.failures.values()].filter((item) =>
      item.boundaryId === boundaryId && item.stage === stage && item.code === code
    ).length;
  }

  private requireFailure(failureIdValue: string): ContinuityFailureRecord {
    const failure = this.failures.get(failureIdValue.trim());
    if (!failure) throw contractError("continuity_failure_missing", `continuity failure not found: ${failureIdValue}`);
    return cloneJson(failure);
  }

  private replaceFailure(value: ContinuityFailureRecord): ContinuityFailureRecord {
    const { failureDigest: _ignored, ...unsigned } = value;
    const updated: ContinuityFailureRecord = { ...unsigned, failureDigest: digest(unsigned) };
    this.failures.set(updated.failureId, updated);
    return cloneJson(updated);
  }

  private assertEnabled(): void {
    if (process.env.ZYRA_DISABLE_SKILL_MEMORY_CONTINUITY_FAILURE === "1") {
      throw contractError("continuity_failure_runtime_disabled", "06C continuity failure runtime is disabled");
    }
  }
}

function normalizeFailureInput(value: ContinuityFailureInput, expected: RuntimeIdentity): Required<ContinuityFailureInput> {
  const identity = normalizeIdentity(value.identity);
  if (identity.taskId !== expected.taskId || identity.sessionId !== expected.sessionId) {
    throw contractError("continuity_failure_identity", "continuity failure belongs to another task/session");
  }
  if (!value.boundaryId?.trim()) throw contractError("continuity_failure_boundary", "continuity failure requires compact boundary id");
  if (!value.code?.trim() || !value.message?.trim()) throw contractError("continuity_failure_detail", "continuity failure requires code and message");
  return {
    identity,
    boundaryId: value.boundaryId.trim(),
    stage: value.stage,
    code: value.code.trim(),
    message: value.message.trim().slice(0, 8_192),
    retryable: Boolean(value.retryable),
    baselineTextReferenceAvailable: Boolean(value.baselineTextReferenceAvailable),
    canonicalCheckpointAvailable: Boolean(value.canonicalCheckpointAvailable),
    currentAuthorityRequired: Boolean(value.currentAuthorityRequired),
    provenanceComplete: Boolean(value.provenanceComplete),
    relatedIds: uniqueStrings(value.relatedIds),
    details: cloneJson(value.details ?? {}),
    causationId: value.causationId?.trim() || value.boundaryId.trim(),
    occurredAt: value.occurredAt?.trim() || "",
  };
}

function dispositionFor(
  input: Required<ContinuityFailureInput>,
  attempt: number,
): ContinuityRecoveryDisposition {
  if (input.stage === "authority_revalidation") return "replan_without_skill_memory";
  if (input.stage === "resume_verification") return "deny_resume";
  if (!input.provenanceComplete) {
    return input.canonicalCheckpointAvailable
      ? "reroute_canonical_checkpoint"
      : "replan_without_skill_memory";
  }
  if (input.retryable && input.baselineTextReferenceAvailable && attempt < 3) {
    return "retry_text_reference";
  }
  if (input.canonicalCheckpointAvailable) return "reroute_canonical_checkpoint";
  if (input.currentAuthorityRequired) return "replan_without_skill_memory";
  return "operator_diagnostic";
}
