import type { JsonObject } from "../contracts.ts";
import {
  canonicalize,
  cloneJson,
  constantTimeDigestEquals,
  deterministicId,
  digest,
  hashChain,
  monotonicNow,
} from "./canonical.ts";
import type { E02Domain, E02RuntimeIdentity } from "./contracts.ts";

export type CapabilityPermitStatus = "issued" | "consumed" | "revoked" | "expired";
export type CapabilityExecutionPhase =
  | "authorized"
  | "prepared"
  | "executing"
  | "effect_recorded"
  | "committed"
  | "failed"
  | "recovery_required";

export interface CapabilityPermit {
  permitId: string;
  decisionId: string;
  effect: "allow";
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  toolName: string;
  namespace: string;
  argumentsDigest: string;
  policyRevision: number;
  modeRevision: number;
  issuedAt: string;
  expiresAt: string;
  consumedAt: string | null;
  revokedAt: string | null;
  status: CapabilityPermitStatus;
  metadata: JsonObject;
  bindingDigest: string;
}

export interface CapabilityExecutionRecord {
  executionId: string;
  permitId: string;
  transitionId: string | null;
  domain: E02Domain;
  owner: string;
  toolName: string;
  toolCallId: string;
  argumentsDigest: string;
  phase: CapabilityExecutionPhase;
  attempt: number;
  idempotencyKey: string;
  startedAt: string;
  updatedAt: string;
  completedAt: string | null;
  resultDigest: string | null;
  result: JsonObject | null;
  failure: JsonObject | null;
  recovery: JsonObject | null;
  metadata: JsonObject;
  recordDigest: string;
}

export interface CapabilityLedgerAuditRow {
  rowId: string;
  sequence: number;
  kind:
    | "permit_issued"
    | "permit_consumed"
    | "permit_revoked"
    | "permit_expired"
    | "execution_prepared"
    | "execution_started"
    | "execution_effect_recorded"
    | "execution_committed"
    | "execution_failed"
    | "execution_recovery_required"
    | "restore_fence";
  entityId: string;
  occurredAt: string;
  payload: JsonObject;
  payloadDigest: string;
  previousHash: string;
  rowHash: string;
}

export interface CapabilityExecutionLedgerSnapshot {
  version: "zyra.e02-capability-execution-ledger/v1";
  runtime: E02RuntimeIdentity;
  sequence: number;
  permits: CapabilityPermit[];
  executions: CapabilityExecutionRecord[];
  audit: CapabilityLedgerAuditRow[];
  headHash: string;
  restoredAt: string | null;
  snapshotHash: string;
  capturedAt: string;
}

export interface CapabilityPermitInput {
  decisionId: string;
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  toolName: string;
  namespace: string;
  argumentsDigest: string;
  policyRevision: number;
  modeRevision: number;
  ttlMs?: number;
  metadata?: JsonObject;
}

export interface CapabilityExecutionInput {
  permitId: string;
  domain: E02Domain;
  owner: string;
  toolName: string;
  toolCallId: string;
  argumentsDigest: string;
  idempotencyKey: string;
  metadata?: JsonObject;
}

const GENESIS = digest({
  version: "zyra.e02-capability-execution-ledger/v1",
  genesis: true,
});

export class CapabilityExecutionLedger {
  private readonly runtime: E02RuntimeIdentity;
  private readonly now: () => Date;
  private readonly maximumPermits: number;
  private readonly maximumExecutions: number;
  private readonly maximumAuditRows: number;
  private readonly permits = new Map<string, CapabilityPermit>();
  private readonly executions = new Map<string, CapabilityExecutionRecord>();
  private readonly executionByIdempotency = new Map<string, string>();
  private readonly audit: CapabilityLedgerAuditRow[] = [];
  private sequence = 0;
  private headHash = GENESIS;
  private restoredAt: string | null = null;
  private lastTimestamp: string | null = null;

  constructor(options: {
    runtime: E02RuntimeIdentity;
    now?: () => Date;
    maximumPermits?: number;
    maximumExecutions?: number;
    maximumAuditRows?: number;
    snapshot?: CapabilityExecutionLedgerSnapshot | null;
  }) {
    this.runtime = cloneJson(options.runtime);
    this.now = options.now ?? (() => new Date());
    this.maximumPermits = options.maximumPermits ?? 20_000;
    this.maximumExecutions = options.maximumExecutions ?? 20_000;
    this.maximumAuditRows = options.maximumAuditRows ?? 100_000;
    validateRuntime(this.runtime);
    if (options.snapshot) this.restore(options.snapshot);
  }

  issuePermit(inputValue: CapabilityPermitInput): CapabilityPermit {
    return this.issuePermitForSubject(inputValue, false);
  }

  issueExternalPermit(inputValue: CapabilityPermitInput): CapabilityPermit {
    return this.issuePermitForSubject(inputValue, true);
  }

  private issuePermitForSubject(
    inputValue: CapabilityPermitInput,
    externalSubject: boolean,
  ): CapabilityPermit {
    const input = cloneJson(inputValue);
    this.validatePermitInput(input, externalSubject);
    this.expirePermits();
    const issuedAt = this.timestamp();
    const expiresAt = new Date(
      Date.parse(issuedAt) + Math.max(1_000, Math.min(input.ttlMs ?? 300_000, 3_600_000)),
    ).toISOString();
    const binding = {
      decisionId: input.decisionId,
      runId: input.runId,
      taskId: input.taskId,
      sessionId: input.sessionId,
      sessionRevision: input.sessionRevision,
      workerRequestId: input.workerRequestId,
      toolCallId: input.toolCallId,
      toolName: input.toolName,
      namespace: input.namespace,
      argumentsDigest: input.argumentsDigest,
      policyRevision: input.policyRevision,
      modeRevision: input.modeRevision,
    };
    const bindingDigest = digest(binding);
    const permitId = deterministicId("e02-capability-permit", {
      ...binding,
      issued_at: issuedAt,
      runtime_epoch: this.runtime.epoch,
    }, 48);
    const existing = this.permits.get(permitId);
    if (existing) {
      if (!constantTimeDigestEquals(existing.bindingDigest, bindingDigest)) {
        throw ledgerError(
          "capability_permit_collision",
          `capability permit ${permitId} collides with another binding`,
        );
      }
      return cloneJson(existing);
    }
    for (const permit of this.permits.values()) {
      if (
        permit.status === "issued"
        && permit.toolCallId === input.toolCallId
        && permit.bindingDigest !== bindingDigest
      ) {
        this.revokePermit(permit.permitId, "superseded_by_new_permission_decision", {
          superseding_decision_id: input.decisionId,
        });
      }
    }
    const permit: CapabilityPermit = {
      permitId,
      decisionId: input.decisionId,
      effect: "allow",
      runId: input.runId,
      taskId: input.taskId,
      sessionId: input.sessionId,
      sessionRevision: input.sessionRevision,
      workerRequestId: input.workerRequestId,
      toolCallId: input.toolCallId,
      toolName: input.toolName,
      namespace: input.namespace,
      argumentsDigest: input.argumentsDigest,
      policyRevision: input.policyRevision,
      modeRevision: input.modeRevision,
      issuedAt,
      expiresAt,
      consumedAt: null,
      revokedAt: null,
      status: "issued",
      metadata: canonicalize(input.metadata ?? {}) as JsonObject,
      bindingDigest,
    };
    this.permits.set(permitId, permit);
    this.appendAudit("permit_issued", permitId, {
      decision_id: permit.decisionId,
      tool_call_id: permit.toolCallId,
      tool_name: permit.toolName,
      arguments_digest: permit.argumentsDigest,
      policy_revision: permit.policyRevision,
      mode_revision: permit.modeRevision,
      expires_at: permit.expiresAt,
    });
    this.trim();
    return cloneJson(permit);
  }

  consumePermit(
    permitId: string,
    binding: {
      runId: string;
      sessionId: string;
      sessionRevision: number;
      workerRequestId: string;
      toolCallId: string;
      toolName: string;
      argumentsDigest: string;
    },
  ): CapabilityPermit {
    this.expirePermits();
    const permit = this.permits.get(permitId);
    if (!permit) throw ledgerError("capability_permit_not_found", `capability permit ${permitId} was not found`);
    if (permit.status !== "issued") {
      throw ledgerError(
        `capability_permit_${permit.status}`,
        `capability permit ${permitId} is ${permit.status}`,
      );
    }
    const mismatches: string[] = [];
    if (permit.runId !== binding.runId) mismatches.push("run_id");
    if (permit.sessionId !== binding.sessionId) mismatches.push("session_id");
    if (permit.sessionRevision !== binding.sessionRevision) mismatches.push("session_revision");
    if (permit.workerRequestId !== binding.workerRequestId) mismatches.push("worker_request_id");
    if (permit.toolCallId !== binding.toolCallId) mismatches.push("tool_call_id");
    if (permit.toolName !== binding.toolName) mismatches.push("tool_name");
    if (!constantTimeDigestEquals(permit.argumentsDigest, binding.argumentsDigest)) mismatches.push("arguments_digest");
    if (mismatches.length) {
      throw ledgerError(
        "capability_permit_binding_mismatch",
        `capability permit ${permitId} binding mismatch: ${mismatches.join(", ")}`,
        { mismatches },
      );
    }
    permit.status = "consumed";
    permit.consumedAt = this.timestamp();
    this.appendAudit("permit_consumed", permitId, {
      tool_call_id: permit.toolCallId,
      arguments_digest: permit.argumentsDigest,
      consumed_at: permit.consumedAt,
    });
    return cloneJson(permit);
  }

  revokePermit(permitId: string, reason: string, metadata: JsonObject = {}): CapabilityPermit {
    const permit = this.permits.get(permitId);
    if (!permit) throw ledgerError("capability_permit_not_found", `capability permit ${permitId} was not found`);
    if (permit.status === "consumed") {
      throw ledgerError("capability_permit_already_consumed", `consumed permit ${permitId} cannot be revoked`);
    }
    if (permit.status !== "revoked") {
      permit.status = "revoked";
      permit.revokedAt = this.timestamp();
      permit.metadata = {
        ...permit.metadata,
        revocation_reason: reason,
        ...canonicalize(metadata) as JsonObject,
      };
      this.appendAudit("permit_revoked", permitId, {
        reason,
        revoked_at: permit.revokedAt,
        metadata: canonicalize(metadata),
      });
    }
    return cloneJson(permit);
  }

  prepareExecution(inputValue: CapabilityExecutionInput): CapabilityExecutionRecord {
    const input = cloneJson(inputValue);
    if (!input.idempotencyKey) {
      throw ledgerError("execution_idempotency_key_missing", "capability execution requires an idempotency key");
    }
    const permit = this.permits.get(input.permitId);
    if (!permit || permit.status !== "consumed") {
      throw ledgerError(
        "execution_permit_not_consumed",
        `capability execution requires a consumed permit, observed ${permit?.status ?? "missing"}`,
      );
    }
    if (
      permit.toolName !== input.toolName
      || permit.toolCallId !== input.toolCallId
      || !constantTimeDigestEquals(permit.argumentsDigest, input.argumentsDigest)
    ) {
      throw ledgerError(
        "execution_permit_binding_mismatch",
        "capability execution does not match its consumed permit",
      );
    }
    const existingId = this.executionByIdempotency.get(input.idempotencyKey);
    if (existingId) {
      const existing = this.executions.get(existingId);
      if (!existing) throw ledgerError("execution_idempotency_index_corrupt", "execution idempotency index is corrupt");
      if (
        existing.toolName !== input.toolName
        || existing.toolCallId !== input.toolCallId
        || existing.domain !== input.domain
        || !constantTimeDigestEquals(existing.argumentsDigest, input.argumentsDigest)
      ) {
        throw ledgerError(
          "execution_idempotency_binding_mismatch",
          "capability execution idempotency key was reused across bindings",
        );
      }
      return cloneJson(existing);
    }
    const startedAt = this.timestamp();
    const executionBase = {
      permitId: input.permitId,
      domain: input.domain,
      owner: input.owner,
      toolName: input.toolName,
      toolCallId: input.toolCallId,
      argumentsDigest: input.argumentsDigest,
      idempotencyKey: input.idempotencyKey,
      runtimeEpoch: this.runtime.epoch,
      startedAt,
    };
    const executionId = deterministicId("e02-capability-execution", executionBase, 48);
    const record: CapabilityExecutionRecord = {
      executionId,
      permitId: input.permitId,
      transitionId: null,
      domain: input.domain,
      owner: input.owner,
      toolName: input.toolName,
      toolCallId: input.toolCallId,
      argumentsDigest: input.argumentsDigest,
      phase: "authorized",
      attempt: 1,
      idempotencyKey: input.idempotencyKey,
      startedAt,
      updatedAt: startedAt,
      completedAt: null,
      resultDigest: null,
      result: null,
      failure: null,
      recovery: null,
      metadata: canonicalize(input.metadata ?? {}) as JsonObject,
      recordDigest: "",
    };
    record.recordDigest = recordDigest(record);
    this.executions.set(record.executionId, record);
    this.executionByIdempotency.set(record.idempotencyKey, record.executionId);
    this.appendAudit("execution_prepared", record.executionId, {
      permit_id: record.permitId,
      tool_name: record.toolName,
      tool_call_id: record.toolCallId,
      domain: record.domain,
      owner: record.owner,
      idempotency_key: record.idempotencyKey,
    });
    this.trim();
    return cloneJson(record);
  }

  bindTransition(executionId: string, transitionId: string): CapabilityExecutionRecord {
    const record = this.requireExecution(executionId);
    if (record.transitionId && record.transitionId !== transitionId) {
      throw ledgerError(
        "execution_transition_conflict",
        `execution ${executionId} is already bound to ${record.transitionId}`,
      );
    }
    record.transitionId = transitionId;
    record.phase = "prepared";
    this.touch(record);
    return cloneJson(record);
  }

  beginExecution(executionId: string): CapabilityExecutionRecord {
    const record = this.requireExecution(executionId);
    if (record.phase === "executing") return cloneJson(record);
    if (record.phase !== "prepared") {
      throw ledgerError(
        "execution_phase_invalid",
        `execution ${executionId} cannot start from ${record.phase}`,
      );
    }
    record.phase = "executing";
    this.touch(record);
    this.appendAudit("execution_started", executionId, {
      transition_id: record.transitionId,
      attempt: record.attempt,
    });
    return cloneJson(record);
  }

  recordEffect(executionId: string, resultValue: JsonObject): CapabilityExecutionRecord {
    const record = this.requireExecution(executionId);
    if (record.phase === "effect_recorded") {
      if (!constantTimeDigestEquals(record.resultDigest ?? "", digest(resultValue))) {
        throw ledgerError(
          "execution_effect_replay_mismatch",
          `execution ${executionId} effect differs from its recorded result`,
        );
      }
      return cloneJson(record);
    }
    if (record.phase === "recovery_required") {
      const result = canonicalize(resultValue) as JsonObject;
      const resultDigest = digest(result);
      if (record.resultDigest && !constantTimeDigestEquals(record.resultDigest, resultDigest)) {
        throw ledgerError(
          "execution_recovery_effect_mismatch",
          `execution ${executionId} recovery result differs from its durable effect`,
        );
      }
      record.phase = "effect_recorded";
      record.result = result;
      record.resultDigest = resultDigest;
      record.recovery = record.recovery
        ? { ...record.recovery, reconciled_at: this.timestamp() }
        : { reconciled_at: this.timestamp() };
      this.touch(record);
      this.appendAudit("execution_effect_recorded", executionId, {
        transition_id: record.transitionId,
        result_digest: record.resultDigest,
        recovered: true,
      });
      return cloneJson(record);
    }
    if (record.phase !== "executing") {
      throw ledgerError(
        "execution_effect_phase_invalid",
        `execution ${executionId} cannot record an effect from ${record.phase}`,
      );
    }
    record.phase = "effect_recorded";
    record.result = canonicalize(resultValue) as JsonObject;
    record.resultDigest = digest(record.result);
    this.touch(record);
    this.appendAudit("execution_effect_recorded", executionId, {
      transition_id: record.transitionId,
      result_digest: record.resultDigest,
    });
    return cloneJson(record);
  }

  commitExecution(executionId: string, resultValue?: JsonObject): CapabilityExecutionRecord {
    const record = this.requireExecution(executionId);
    if (record.phase === "committed") {
      if (resultValue && !constantTimeDigestEquals(record.resultDigest ?? "", digest(resultValue))) {
        throw ledgerError(
          "execution_commit_replay_mismatch",
          `execution ${executionId} commit replay differs from its result`,
        );
      }
      return cloneJson(record);
    }
    if (!new Set<CapabilityExecutionPhase>(["executing", "effect_recorded", "prepared"]).has(record.phase)) {
      throw ledgerError(
        "execution_commit_phase_invalid",
        `execution ${executionId} cannot commit from ${record.phase}`,
      );
    }
    if (resultValue) {
      record.result = canonicalize(resultValue) as JsonObject;
      record.resultDigest = digest(record.result);
    }
    if (!record.result || !record.resultDigest) {
      throw ledgerError(
        "execution_result_missing",
        `execution ${executionId} cannot commit without a result`,
      );
    }
    record.phase = "committed";
    record.completedAt = this.timestamp();
    this.touch(record, record.completedAt);
    this.appendAudit("execution_committed", executionId, {
      transition_id: record.transitionId,
      result_digest: record.resultDigest,
      completed_at: record.completedAt,
    });
    return cloneJson(record);
  }

  failExecution(
    executionId: string,
    error: unknown,
    metadata: JsonObject = {},
  ): CapabilityExecutionRecord {
    const record = this.requireExecution(executionId);
    if (record.phase === "committed") {
      throw ledgerError("execution_already_committed", `committed execution ${executionId} cannot fail`);
    }
    const failedAt = this.timestamp();
    record.phase = "failed";
    record.completedAt = failedAt;
    record.failure = {
      name: error instanceof Error ? error.name : "Error",
      message: error instanceof Error ? error.message : String(error),
      code: errorCode(error),
      failed_at: failedAt,
      metadata: canonicalize(metadata),
    };
    this.touch(record, failedAt);
    this.appendAudit("execution_failed", executionId, {
      transition_id: record.transitionId,
      failure: record.failure,
    });
    return cloneJson(record);
  }

  requireRecovery(
    executionId: string,
    recoveryValue: JsonObject,
  ): CapabilityExecutionRecord {
    const record = this.requireExecution(executionId);
    if (record.phase === "committed") {
      throw ledgerError(
        "execution_already_committed",
        `committed execution ${executionId} cannot enter recovery`,
      );
    }
    record.phase = "recovery_required";
    record.recovery = canonicalize(recoveryValue) as JsonObject;
    this.touch(record);
    this.appendAudit("execution_recovery_required", executionId, {
      transition_id: record.transitionId,
      recovery: record.recovery,
    });
    return cloneJson(record);
  }

  getPermit(permitId: string): CapabilityPermit | null {
    const permit = this.permits.get(permitId);
    return permit ? cloneJson(permit) : null;
  }

  getExecution(executionId: string): CapabilityExecutionRecord | null {
    const record = this.executions.get(executionId);
    return record ? cloneJson(record) : null;
  }

  executionByKey(idempotencyKey: string): CapabilityExecutionRecord | null {
    const executionId = this.executionByIdempotency.get(idempotencyKey);
    return executionId ? this.getExecution(executionId) : null;
  }

  listExecutions(options: {
    phase?: CapabilityExecutionPhase;
    toolName?: string;
    after?: string;
    limit?: number;
  } = {}): CapabilityExecutionRecord[] {
    const limit = Math.max(1, Math.min(options.limit ?? 1_000, 20_000));
    return [...this.executions.values()]
      .filter((record) => !options.phase || record.phase === options.phase)
      .filter((record) => !options.toolName || record.toolName === options.toolName)
      .filter((record) => !options.after || record.updatedAt > options.after)
      .sort((left, right) => left.startedAt.localeCompare(right.startedAt))
      .slice(-limit)
      .map(cloneJson);
  }

  recoveryRequired(): CapabilityExecutionRecord[] {
    return this.listExecutions({ phase: "recovery_required", limit: 20_000 });
  }

  expirePermits(at = this.timestamp()): CapabilityPermit[] {
    const expired: CapabilityPermit[] = [];
    for (const permit of this.permits.values()) {
      if (permit.status !== "issued" || permit.expiresAt > at) continue;
      permit.status = "expired";
      permit.revokedAt = at;
      this.appendAudit("permit_expired", permit.permitId, {
        expires_at: permit.expiresAt,
        observed_at: at,
      });
      expired.push(cloneJson(permit));
    }
    return expired;
  }

  snapshot(): CapabilityExecutionLedgerSnapshot {
    this.expirePermits();
    const withoutHash = {
      version: "zyra.e02-capability-execution-ledger/v1" as const,
      runtime: cloneJson(this.runtime),
      sequence: this.sequence,
      permits: [...this.permits.values()]
        .sort((left, right) => left.issuedAt.localeCompare(right.issuedAt))
        .map(cloneJson),
      executions: [...this.executions.values()]
        .sort((left, right) => left.startedAt.localeCompare(right.startedAt))
        .map(cloneJson),
      audit: this.audit.map(cloneJson),
      headHash: this.headHash,
      restoredAt: this.restoredAt,
      capturedAt: this.timestamp(),
    };
    return {
      ...withoutHash,
      snapshotHash: digest(withoutHash),
    };
  }

  restore(snapshotValue: CapabilityExecutionLedgerSnapshot): void {
    const snapshot = cloneJson(snapshotValue);
    if (snapshot.version !== "zyra.e02-capability-execution-ledger/v1") {
      throw ledgerError(
        "unsupported_execution_ledger_snapshot",
        `unsupported execution ledger snapshot ${snapshot.version}`,
      );
    }
    const { snapshotHash: expectedHash, ...withoutHash } = snapshot;
    if (!constantTimeDigestEquals(digest(withoutHash), expectedHash)) {
      throw ledgerError(
        "execution_ledger_snapshot_digest_mismatch",
        "capability execution ledger snapshot digest does not match its payload",
      );
    }
    if (
      snapshot.runtime.runId !== this.runtime.runId
      || snapshot.runtime.taskId !== this.runtime.taskId
      || snapshot.runtime.sessionId !== this.runtime.sessionId
      || snapshot.runtime.workerRequestId !== this.runtime.workerRequestId
    ) {
      throw ledgerError(
        "execution_ledger_restore_binding_mismatch",
        "capability execution ledger snapshot belongs to another execution binding",
      );
    }
    if (snapshot.runtime.epoch >= this.runtime.epoch) {
      throw ledgerError(
        "execution_ledger_restore_epoch_not_advanced",
        "capability execution ledger restore must advance the runtime epoch",
      );
    }
    this.verifyAudit(snapshot.audit, snapshot.sequence, snapshot.headHash);
    this.permits.clear();
    this.executions.clear();
    this.executionByIdempotency.clear();
    this.audit.splice(0);
    this.sequence = snapshot.sequence;
    this.headHash = snapshot.headHash;
    let revokedActivePermits = 0;
    let preservedExternalApprovalPermits = 0;
    const restoreObservedAt = this.now().getTime();
    for (const permitValue of snapshot.permits.slice(-this.maximumPermits)) {
      const permit = cloneJson(permitValue);
      if (permit.status === "issued") {
        const preserveExactExternalApproval = (
          permit.metadata.external_permission_subject === true
          && permit.metadata.restart_safe_exact_approval === true
          && permit.metadata.host_runtime_id === this.runtime.runtimeId
          && Date.parse(permit.expiresAt) > restoreObservedAt
        );
        if (preserveExactExternalApproval) {
          preservedExternalApprovalPermits += 1;
          permit.metadata = {
            ...permit.metadata,
            restored_source_epoch: snapshot.runtime.epoch,
            restored_target_epoch: this.runtime.epoch,
          };
        } else {
          revokedActivePermits += 1;
          permit.status = "revoked";
          permit.revokedAt = this.timestamp();
          permit.metadata = {
            ...permit.metadata,
            revocation_reason: "restart_epoch_fence",
            source_epoch: snapshot.runtime.epoch,
            target_epoch: this.runtime.epoch,
          };
        }
      }
      this.permits.set(permit.permitId, permit);
    }
    for (const recordValue of snapshot.executions.slice(-this.maximumExecutions)) {
      const record = cloneJson(recordValue);
      if (!constantTimeDigestEquals(record.recordDigest, recordDigest(record))) {
        throw ledgerError(
          "execution_record_digest_mismatch",
          `capability execution ${record.executionId} digest is invalid`,
        );
      }
      if (record.phase === "executing" || record.phase === "effect_recorded") {
        record.phase = "recovery_required";
        record.recovery = {
          kind: "restart_reconciliation",
          prior_phase: recordValue.phase,
          source_epoch: snapshot.runtime.epoch,
          target_epoch: this.runtime.epoch,
          reexecute_without_receipt: false,
        };
        record.updatedAt = this.timestamp();
        record.recordDigest = recordDigest(record);
      }
      this.executions.set(record.executionId, record);
      this.executionByIdempotency.set(record.idempotencyKey, record.executionId);
    }
    for (const row of snapshot.audit.slice(-this.maximumAuditRows)) this.audit.push(cloneJson(row));
    this.restoredAt = this.timestamp();
    this.appendAudit("restore_fence", this.runtime.runtimeId, {
      source_epoch: snapshot.runtime.epoch,
      target_epoch: this.runtime.epoch,
      revoked_active_permits: revokedActivePermits,
      preserved_external_approval_permits: preservedExternalApprovalPermits,
      recovery_required: this.recoveryRequired().length,
    });
  }

  health(): JsonObject {
    this.expirePermits();
    const activePermits = [...this.permits.values()].filter((permit) => permit.status === "issued").length;
    const recovery = this.recoveryRequired();
    return {
      canonical_owner: "typescript",
      runtime_epoch: this.runtime.epoch,
      active_permits: activePermits,
      consumed_permits: [...this.permits.values()].filter((permit) => permit.status === "consumed").length,
      execution_count: this.executions.size,
      committed_executions: [...this.executions.values()].filter((record) => record.phase === "committed").length,
      recovery_required: recovery.length,
      recovery_execution_ids: recovery.map((record) => record.executionId),
      audit_sequence: this.sequence,
      audit_head_hash: this.headHash,
      restored_at: this.restoredAt,
      python_execution_ledger_fallback: false,
    };
  }

  private validatePermitInput(
    input: CapabilityPermitInput,
    externalSubject: boolean,
  ): void {
    for (const [label, value] of Object.entries({
      decisionId: input.decisionId,
      runId: input.runId,
      taskId: input.taskId,
      sessionId: input.sessionId,
      workerRequestId: input.workerRequestId,
      toolCallId: input.toolCallId,
      toolName: input.toolName,
      namespace: input.namespace,
      argumentsDigest: input.argumentsDigest,
    })) {
      if (typeof value !== "string" || !value) {
        throw ledgerError("capability_permit_identity_incomplete", `capability permit ${label} is required`);
      }
    }
    const runtimeBindingMismatch = (
      input.runId !== this.runtime.runId
      || input.taskId !== this.runtime.taskId
      || input.sessionId !== this.runtime.sessionId
      || input.workerRequestId !== this.runtime.workerRequestId
    );
    if (runtimeBindingMismatch && !externalSubject) {
      throw ledgerError(
        "capability_permit_runtime_binding_mismatch",
        "capability permit does not belong to this runtime binding",
      );
    }
    if (externalSubject) {
      const requestBinding = input.metadata?.permission_request_binding;
      if (
        input.metadata?.external_permission_subject !== true
        || input.metadata?.host_runtime_id !== this.runtime.runtimeId
        || !requestBinding
        || typeof requestBinding !== "object"
        || Array.isArray(requestBinding)
      ) {
        throw ledgerError(
          "capability_external_permit_custody_invalid",
          "external capability permit requires canonical host and permission binding custody",
        );
      }
      if (!runtimeBindingMismatch) {
        throw ledgerError(
          "capability_external_permit_subject_invalid",
          "external capability permit must identify a subject outside the host runtime binding",
        );
      }
      const binding = requestBinding as JsonObject;
      if (
        binding.run_id !== input.runId
        || binding.task_id !== input.taskId
        || binding.session_id !== input.sessionId
        || binding.session_revision !== input.sessionRevision
        || binding.worker_request_id !== input.workerRequestId
        || binding.tool_call_id !== input.toolCallId
        || binding.tool_name !== input.toolName
        || binding.namespace !== input.namespace
        || binding.arguments_digest !== input.argumentsDigest
      ) {
        throw ledgerError(
          "capability_external_permit_binding_mismatch",
          "external capability permit differs from its canonical permission request binding",
        );
      }
    }
    for (const [label, value] of Object.entries({
      sessionRevision: input.sessionRevision,
      policyRevision: input.policyRevision,
      modeRevision: input.modeRevision,
    })) {
      if (!Number.isSafeInteger(value) || value < 0) {
        throw ledgerError("capability_permit_revision_invalid", `capability permit ${label} is invalid`);
      }
    }
    if (!/^[a-f0-9]{64}$/i.test(input.argumentsDigest)) {
      throw ledgerError("capability_permit_arguments_digest_invalid", "capability permit arguments digest is invalid");
    }
  }

  private requireExecution(executionId: string): CapabilityExecutionRecord {
    const record = this.executions.get(executionId);
    if (!record) throw ledgerError("capability_execution_not_found", `capability execution ${executionId} was not found`);
    return record;
  }

  private touch(record: CapabilityExecutionRecord, at?: string): void {
    record.updatedAt = at ?? this.timestamp();
    record.recordDigest = recordDigest(record);
  }

  private appendAudit(
    kind: CapabilityLedgerAuditRow["kind"],
    entityId: string,
    payloadValue: JsonObject,
  ): CapabilityLedgerAuditRow {
    const occurredAt = this.timestamp();
    const payload = canonicalize(payloadValue) as JsonObject;
    const payloadDigest = digest(payload);
    const base = {
      sequence: this.sequence + 1,
      kind,
      entityId,
      occurredAt,
      payload,
      payloadDigest,
      previousHash: this.headHash,
    };
    const rowId = deterministicId("e02-capability-ledger-row", base, 40);
    const rowHash = hashChain(this.headHash, { row_id: rowId, ...base });
    const row: CapabilityLedgerAuditRow = {
      rowId,
      ...base,
      rowHash,
    };
    this.sequence += 1;
    this.headHash = rowHash;
    this.audit.push(row);
    if (this.audit.length > this.maximumAuditRows) {
      this.audit.splice(0, this.audit.length - this.maximumAuditRows);
    }
    return cloneJson(row);
  }

  private verifyAudit(rows: CapabilityLedgerAuditRow[], sequence: number, headHashValue: string): void {
    if (!rows.length) {
      if (sequence !== 0 || headHashValue !== GENESIS) {
        throw ledgerError("execution_ledger_empty_chain_invalid", "empty execution ledger audit chain is invalid");
      }
      return;
    }
    let previousHash = rows[0]!.previousHash;
    let expectedSequence = rows[0]!.sequence;
    for (const row of rows) {
      if (row.sequence !== expectedSequence) {
        throw ledgerError("execution_ledger_audit_sequence_invalid", `audit row ${row.rowId} sequence is invalid`);
      }
      if (row.previousHash !== previousHash) {
        throw ledgerError("execution_ledger_audit_chain_invalid", `audit row ${row.rowId} previous hash is invalid`);
      }
      if (!constantTimeDigestEquals(row.payloadDigest, digest(row.payload))) {
        throw ledgerError("execution_ledger_audit_payload_invalid", `audit row ${row.rowId} payload digest is invalid`);
      }
      const base = {
        sequence: row.sequence,
        kind: row.kind,
        entityId: row.entityId,
        occurredAt: row.occurredAt,
        payload: row.payload,
        payloadDigest: row.payloadDigest,
        previousHash: row.previousHash,
      };
      const expectedId = deterministicId("e02-capability-ledger-row", base, 40);
      const expectedHash = hashChain(previousHash, { row_id: expectedId, ...base });
      if (row.rowId !== expectedId || !constantTimeDigestEquals(row.rowHash, expectedHash)) {
        throw ledgerError("execution_ledger_audit_row_invalid", `audit row ${row.rowId} hash is invalid`);
      }
      previousHash = row.rowHash;
      expectedSequence += 1;
    }
    if (sequence !== rows.at(-1)!.sequence || !constantTimeDigestEquals(headHashValue, previousHash)) {
      throw ledgerError("execution_ledger_audit_tail_invalid", "execution ledger audit tail is invalid");
    }
  }

  private trim(): void {
    while (this.permits.size > this.maximumPermits) {
      const removable = [...this.permits.values()]
        .filter((permit) => permit.status !== "issued")
        .sort((left, right) => left.issuedAt.localeCompare(right.issuedAt))[0];
      if (!removable) break;
      this.permits.delete(removable.permitId);
    }
    while (this.executions.size > this.maximumExecutions) {
      const removable = [...this.executions.values()]
        .filter((record) => record.phase === "committed" || record.phase === "failed")
        .sort((left, right) => left.startedAt.localeCompare(right.startedAt))[0];
      if (!removable) break;
      this.executions.delete(removable.executionId);
      this.executionByIdempotency.delete(removable.idempotencyKey);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function recordDigest(record: CapabilityExecutionRecord): string {
  const { recordDigest: _recordDigest, ...withoutDigest } = record;
  return digest(withoutDigest);
}

function validateRuntime(runtime: E02RuntimeIdentity): void {
  for (const [label, value] of Object.entries({
    runtimeId: runtime.runtimeId,
    runId: runtime.runId,
    taskId: runtime.taskId,
    sessionId: runtime.sessionId,
    workerRequestId: runtime.workerRequestId,
  })) {
    if (typeof value !== "string" || !value) {
      throw ledgerError("execution_ledger_runtime_incomplete", `execution ledger runtime ${label} is required`);
    }
  }
  if (!Number.isSafeInteger(runtime.epoch) || runtime.epoch < 1) {
    throw ledgerError("execution_ledger_epoch_invalid", "execution ledger runtime epoch is invalid");
  }
}

function errorCode(error: unknown): string {
  if (error && typeof error === "object") {
    const code = (error as { code?: unknown }).code;
    if (typeof code === "string" && code) return code;
  }
  return error instanceof Error && error.name ? error.name : "capability_execution_failed";
}

function ledgerError(code: string, message: string, details: JsonObject = {}): Error {
  return Object.assign(new Error(message), {
    name: "CapabilityExecutionLedgerError",
    code,
    details: canonicalize(details),
  });
}
