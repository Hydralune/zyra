import {
  SKILL_OUTCOME_PROTOCOL,
  assertSameRuntime,
  canonicalize,
  cloneJson,
  contractError,
  digest,
  durationMilliseconds,
  normalizeEvidence,
  normalizeIdentity,
  normalizePolicyDecision,
  normalizeReuseCondition,
  normalizeSkillMemoryPolicy,
  normalizeSkillVersion,
  nowIso,
  required,
  sanitizeJsonObject,
  sha256Digest,
  stableId,
  timestamp,
  uniqueStrings,
  type JsonObject,
  type OutcomeEvidenceReference,
  type OutcomeMemoryState,
  type ReuseCondition,
  type RuntimeIdentity,
  type SkillInvocationOutcomeInput,
  type SkillInvocationOutcomeMemory,
  type SkillInvocationStatus,
  type SkillMemoryPolicy,
} from "./contracts.ts";

export interface SkillOutcomeQuery {
  taskId?: string;
  sessionId?: string;
  skillId?: string;
  skillName?: string;
  statuses?: SkillInvocationStatus[];
  states?: OutcomeMemoryState[];
  toolNames?: string[];
  artifactIds?: string[];
  completedOnly?: boolean;
  reusableOnly?: boolean;
  occurredAfter?: string;
  occurredBefore?: string;
  limit?: number;
}

export interface SkillOutcomeAdmissionReceipt {
  receiptId: string;
  memoryId: string;
  invocationId: string;
  disposition: "accepted" | "replayed" | "quarantined";
  reason: string;
  revisionBefore: number;
  revisionAfter: number;
  stateDigest: string;
  createdAt: string;
}

export interface SkillOutcomeQuarantineRecord {
  quarantineId: string;
  invocationId: string;
  code: string;
  message: string;
  inputDigest: string;
  identity: RuntimeIdentity;
  observedAt: string;
  details: JsonObject;
}

export interface SkillOutcomeSupersession {
  supersessionId: string;
  priorMemoryId: string;
  replacementMemoryId: string;
  reason: string;
  actor: string;
  occurredAt: string;
  digest: string;
}

export interface SkillOutcomeRuntimeSnapshot {
  version: "zyra.skill-outcome-runtime/v1";
  identity: RuntimeIdentity;
  policy: SkillMemoryPolicy;
  revision: number;
  records: SkillInvocationOutcomeMemory[];
  receipts: SkillOutcomeAdmissionReceipt[];
  quarantines: SkillOutcomeQuarantineRecord[];
  supersessions: SkillOutcomeSupersession[];
  capturedAt: string;
  checksum: string;
}

export interface SkillOutcomeRuntimeOptions {
  identity: RuntimeIdentity;
  policy?: Partial<SkillMemoryPolicy>;
  now?: () => Date;
  snapshot?: SkillOutcomeRuntimeSnapshot | null;
}

export class SkillOutcomeRuntime {
  readonly identity: RuntimeIdentity;
  private policy: SkillMemoryPolicy;
  private readonly records = new Map<string, SkillInvocationOutcomeMemory>();
  private readonly invocationBindings = new Map<string, string>();
  private readonly sourceDigestBindings = new Map<string, string>();
  private readonly receipts: SkillOutcomeAdmissionReceipt[] = [];
  private readonly quarantines = new Map<string, SkillOutcomeQuarantineRecord>();
  private readonly supersessions: SkillOutcomeSupersession[] = [];
  private readonly now: () => Date;
  private revision = 0;

  constructor(options: SkillOutcomeRuntimeOptions) {
    this.identity = normalizeIdentity(options.identity);
    this.policy = normalizeSkillMemoryPolicy(options.policy);
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  admit(inputValue: SkillInvocationOutcomeInput): SkillOutcomeAdmissionReceipt {
    this.assertEnabled();
    const revisionBefore = this.revision;
    let input: SkillInvocationOutcomeInput;
    try {
      input = this.normalizeInput(inputValue);
      this.validateInput(input);
    } catch (error) {
      const quarantine = this.quarantine(inputValue, error);
      this.revision += 1;
      return this.receipt({
        memoryId: "",
        invocationId: String(inputValue.invocationId ?? ""),
        disposition: "quarantined",
        reason: quarantine.code,
        revisionBefore,
      });
    }
    const inputDigest = digest(input);
    const existingMemoryId = this.invocationBindings.get(input.invocationId);
    if (existingMemoryId) {
      const existing = this.require(existingMemoryId);
      if (existing.sourceRecordDigest !== input.sourceRecordDigest || existing.recordDigest !== this.recordDigestFor(input, existing.memoryId, existing.observedAt)) {
        const quarantine = this.quarantine(input, contractError(
          "skill_outcome_invocation_conflict",
          `invocation ${input.invocationId} was already admitted with different content`,
          { existing_memory_id: existingMemoryId, input_digest: inputDigest },
        ));
        this.revision += 1;
        return this.receipt({
          memoryId: existingMemoryId,
          invocationId: input.invocationId,
          disposition: "quarantined",
          reason: quarantine.code,
          revisionBefore,
        });
      }
      return this.receipt({
        memoryId: existingMemoryId,
        invocationId: input.invocationId,
        disposition: "replayed",
        reason: "invocation_already_admitted",
        revisionBefore,
        incrementRevision: false,
      });
    }
    const digestBoundMemoryId = this.sourceDigestBindings.get(input.sourceRecordDigest);
    if (digestBoundMemoryId) {
      const existing = this.require(digestBoundMemoryId);
      if (existing.invocationId !== input.invocationId) {
        const quarantine = this.quarantine(input, contractError(
          "skill_outcome_source_digest_reused",
          "03C source record digest is already bound to another invocation",
          { existing_memory_id: digestBoundMemoryId, existing_invocation_id: existing.invocationId },
        ));
        this.revision += 1;
        return this.receipt({
          memoryId: digestBoundMemoryId,
          invocationId: input.invocationId,
          disposition: "quarantined",
          reason: quarantine.code,
          revisionBefore,
        });
      }
    }
    const observedAt = nowIso(this.now);
    const memoryId = stableId(
      "skill-outcome-memory",
      input.identity.taskId,
      input.identity.sessionId,
      input.invocationId,
      input.sourceRecordDigest,
    );
    const record = this.toRecord(input, memoryId, observedAt);
    this.records.set(memoryId, record);
    this.invocationBindings.set(record.invocationId, memoryId);
    this.sourceDigestBindings.set(record.sourceRecordDigest, memoryId);
    this.revision += 1;
    this.enforceRetention();
    return this.receipt({
      memoryId,
      invocationId: input.invocationId,
      disposition: record.state === "quarantined" ? "quarantined" : "accepted",
      reason: record.state === "quarantined" ? "outcome_not_reusable" : "outcome_admitted",
      revisionBefore,
    });
  }

  get(memoryId: string): SkillInvocationOutcomeMemory | null {
    const record = this.records.get(memoryId.trim());
    return record ? cloneJson(record) : null;
  }

  getByInvocation(invocationId: string): SkillInvocationOutcomeMemory | null {
    const memoryId = this.invocationBindings.get(invocationId.trim());
    return memoryId ? this.get(memoryId) : null;
  }

  query(queryValue: SkillOutcomeQuery = {}): SkillInvocationOutcomeMemory[] {
    const query = normalizeQuery(queryValue);
    const statuses = new Set(query.statuses);
    const states = new Set(query.states);
    const toolNames = new Set(query.toolNames);
    const artifactIds = new Set(query.artifactIds);
    const after = query.occurredAfter ? Date.parse(query.occurredAfter) : Number.NEGATIVE_INFINITY;
    const before = query.occurredBefore ? Date.parse(query.occurredBefore) : Number.POSITIVE_INFINITY;
    return [...this.records.values()]
      .filter((record) => !query.taskId || record.identity.taskId === query.taskId)
      .filter((record) => !query.sessionId || record.identity.sessionId === query.sessionId)
      .filter((record) => !query.skillId || record.version.skillId === query.skillId)
      .filter((record) => !query.skillName || record.version.skillName === query.skillName)
      .filter((record) => statuses.size === 0 || statuses.has(record.status))
      .filter((record) => states.size === 0 || states.has(record.state))
      .filter((record) => !query.completedOnly || record.status === "completed")
      .filter((record) => !query.reusableOnly || isReusable(record, this.policy))
      .filter((record) => Date.parse(record.observedAt) >= after && Date.parse(record.observedAt) <= before)
      .filter((record) => toolNames.size === 0 || record.evidence.some((item) => item.toolName && toolNames.has(item.toolName)))
      .filter((record) => artifactIds.size === 0 || record.artifactIds.some((id) => artifactIds.has(id)))
      .sort(outcomeOrder)
      .slice(0, query.limit)
      .map(cloneJson);
  }

  reusable(query: Omit<SkillOutcomeQuery, "reusableOnly"> = {}): SkillInvocationOutcomeMemory[] {
    return this.query({ ...query, reusableOnly: true });
  }

  evidence(memoryId: string, kinds: OutcomeEvidenceReference["kind"][] = []): OutcomeEvidenceReference[] {
    const allowed = new Set(kinds);
    return this.require(memoryId).evidence
      .filter((item) => allowed.size === 0 || allowed.has(item.kind))
      .sort(evidenceOrder)
      .map(cloneJson);
  }

  matchConditions(memoryId: string, facts: JsonObject): { matches: boolean; failedConditionIds: string[] } {
    const record = this.require(memoryId);
    const failedConditionIds = record.reuseConditions
      .filter((condition) => condition.required && !conditionMatches(condition, facts))
      .map((condition) => condition.conditionId);
    return { matches: failedConditionIds.length === 0, failedConditionIds };
  }

  supersede(priorMemoryId: string, replacementMemoryId: string, reason: string, actor: string): SkillOutcomeSupersession {
    this.assertEnabled();
    const prior = this.require(priorMemoryId);
    const replacement = this.require(replacementMemoryId);
    if (prior.memoryId === replacement.memoryId) throw contractError("skill_outcome_self_supersession", "outcome cannot supersede itself");
    if (prior.version.skillId !== replacement.version.skillId) {
      throw contractError("skill_outcome_supersession_skill_mismatch", "replacement must describe the same skill id");
    }
    if (prior.state === "superseded") {
      const existing = this.supersessions.find((item) => item.priorMemoryId === prior.memoryId);
      if (existing?.replacementMemoryId === replacement.memoryId) return cloneJson(existing);
      throw contractError("skill_outcome_already_superseded", "outcome is already superseded by another record");
    }
    prior.state = "superseded";
    prior.revision += 1;
    prior.recordDigest = digest(recordProjection(prior));
    const occurredAt = nowIso(this.now);
    const unsigned = {
      supersessionId: stableId("skill-outcome-supersession", prior.memoryId, replacement.memoryId, reason),
      priorMemoryId: prior.memoryId,
      replacementMemoryId: replacement.memoryId,
      reason: required(reason, "supersession reason"),
      actor: required(actor, "supersession actor"),
      occurredAt,
    };
    const supersession: SkillOutcomeSupersession = { ...unsigned, digest: digest(unsigned) };
    this.supersessions.push(supersession);
    this.revision += 1;
    return cloneJson(supersession);
  }

  configure(policy: Partial<SkillMemoryPolicy>): SkillMemoryPolicy {
    this.policy = normalizeSkillMemoryPolicy({ ...this.policy, ...policy });
    this.enforceRetention();
    this.revision += 1;
    return cloneJson(this.policy);
  }

  purgeExpired(at = nowIso(this.now)): string[] {
    const cutoff = Date.parse(timestamp(at, "purge timestamp")) - this.policy.retentionMilliseconds;
    const removable = [...this.records.values()]
      .filter((record) => record.state === "superseded" || Date.parse(record.observedAt) < cutoff)
      .sort((left, right) => left.observedAt.localeCompare(right.observedAt));
    const removed: string[] = [];
    for (const record of removable) {
      this.deleteRecord(record.memoryId);
      removed.push(record.memoryId);
    }
    if (removed.length > 0) this.revision += 1;
    return removed;
  }

  health(): JsonObject {
    const records = [...this.records.values()];
    return {
      protocol: SKILL_OUTCOME_PROTOCOL,
      canonical_owner: "SkillOutcomeRuntime",
      identity: canonicalize(this.identity),
      revision: this.revision,
      record_count: records.length,
      reusable_count: records.filter((record) => isReusable(record, this.policy)).length,
      completed_count: records.filter((record) => record.status === "completed").length,
      failed_count: records.filter((record) => record.status === "failed").length,
      quarantined_count: this.quarantines.size,
      superseded_count: records.filter((record) => record.state === "superseded").length,
      skill_count: new Set(records.map((record) => record.version.skillId)).size,
      session_count: new Set(records.map((record) => record.identity.sessionId)).size,
      loads_skill_body: false,
      invokes_skills: false,
      owns_skill_versions: false,
      policy_evaluator: false,
      source_runtime: "03C SkillCoordinator",
      snapshot_digest: this.snapshot().checksum,
    };
  }

  snapshot(): SkillOutcomeRuntimeSnapshot {
    const unsigned: Omit<SkillOutcomeRuntimeSnapshot, "checksum"> = {
      version: "zyra.skill-outcome-runtime/v1",
      identity: cloneJson(this.identity),
      policy: cloneJson(this.policy),
      revision: this.revision,
      records: [...this.records.values()].sort(outcomeIdentityOrder).map(cloneJson),
      receipts: this.receipts.map(cloneJson),
      quarantines: [...this.quarantines.values()].sort((left, right) => left.observedAt.localeCompare(right.observedAt)).map(cloneJson),
      supersessions: this.supersessions.map(cloneJson),
      capturedAt: nowIso(this.now),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshotValue: SkillOutcomeRuntimeSnapshot): void {
    if (snapshotValue.version !== "zyra.skill-outcome-runtime/v1") throw contractError("skill_outcome_snapshot_version", "unsupported skill outcome snapshot version");
    const { checksum, ...unsigned } = snapshotValue;
    if (digest(unsigned) !== checksum) throw contractError("skill_outcome_snapshot_checksum", "skill outcome snapshot checksum mismatch");
    assertSameRuntime(snapshotValue.identity, this.identity, "skill outcome snapshot");
    this.records.clear();
    this.invocationBindings.clear();
    this.sourceDigestBindings.clear();
    this.receipts.splice(0, this.receipts.length);
    this.quarantines.clear();
    this.supersessions.splice(0, this.supersessions.length);
    this.policy = normalizeSkillMemoryPolicy(snapshotValue.policy);
    for (const recordValue of snapshotValue.records) {
      const record = validateRestoredRecord(recordValue);
      if (this.records.has(record.memoryId)) throw contractError("skill_outcome_snapshot_duplicate_memory", `duplicate memory id ${record.memoryId}`);
      if (this.invocationBindings.has(record.invocationId)) throw contractError("skill_outcome_snapshot_duplicate_invocation", `duplicate invocation id ${record.invocationId}`);
      if (this.sourceDigestBindings.has(record.sourceRecordDigest)) throw contractError("skill_outcome_snapshot_duplicate_source", "duplicate 03C source digest");
      this.records.set(record.memoryId, record);
      this.invocationBindings.set(record.invocationId, record.memoryId);
      this.sourceDigestBindings.set(record.sourceRecordDigest, record.memoryId);
    }
    for (const receipt of snapshotValue.receipts) this.receipts.push(cloneJson(receipt));
    for (const quarantine of snapshotValue.quarantines) this.quarantines.set(quarantine.quarantineId, cloneJson(quarantine));
    for (const supersession of snapshotValue.supersessions) {
      if (digest({
        supersessionId: supersession.supersessionId,
        priorMemoryId: supersession.priorMemoryId,
        replacementMemoryId: supersession.replacementMemoryId,
        reason: supersession.reason,
        actor: supersession.actor,
        occurredAt: supersession.occurredAt,
      }) !== supersession.digest) throw contractError("skill_outcome_supersession_digest", "restored supersession digest mismatch");
      this.supersessions.push(cloneJson(supersession));
    }
    this.revision = snapshotValue.revision;
  }

  private normalizeInput(input: SkillInvocationOutcomeInput): SkillInvocationOutcomeInput {
    const evidence = input.evidence.map(normalizeEvidence).sort(evidenceOrder);
    const conditions = input.reuseConditions.map(normalizeReuseCondition);
    return {
      protocol: input.protocol,
      identity: normalizeIdentity(input.identity),
      invocationId: required(input.invocationId, "skill invocation id"),
      toolCallId: required(input.toolCallId, "skill tool call id"),
      compositionId: required(input.compositionId, "skill composition id"),
      status: invocationStatus(input.status),
      version: normalizeSkillVersion(input.version),
      policy: normalizePolicyDecision(input.policy),
      evidence,
      artifacts: input.artifacts.map(sanitizeJsonObject),
      outputDigest: sha256Digest(input.outputDigest, "skill output digest"),
      summary: required(input.summary, "skill outcome summary").slice(0, this.policy.maximumSummaryCharacters),
      successReason: nullableReason(input.successReason),
      failureReason: nullableReason(input.failureReason),
      reuseConditions: conditions,
      inputTokens: nonNegativeMetric(input.inputTokens, "input tokens"),
      outputTokens: nonNegativeMetric(input.outputTokens, "output tokens"),
      toolCalls: nonNegativeMetric(input.toolCalls, "tool calls"),
      costMicros: nonNegativeMetric(input.costMicros, "cost micros"),
      startedAt: timestamp(input.startedAt, "skill started timestamp"),
      completedAt: input.completedAt ? timestamp(input.completedAt, "skill completed timestamp") : null,
      sourceRecordDigest: sha256Digest(input.sourceRecordDigest, "03C source record digest"),
      metadata: sanitizeJsonObject(input.metadata),
    };
  }

  private validateInput(input: SkillInvocationOutcomeInput): void {
    if (input.protocol !== SKILL_OUTCOME_PROTOCOL) throw contractError("skill_outcome_protocol", "unsupported skill outcome protocol");
    assertSameRuntime(this.identity, input.identity, "skill outcome");
    if (input.status !== "background" && !input.completedAt) throw contractError("skill_outcome_completion_missing", "terminal skill outcome requires completed_at");
    if (input.completedAt && Date.parse(input.completedAt) < Date.parse(input.startedAt)) throw contractError("skill_outcome_time_regression", "skill outcome completed before it started");
    if (input.status === "completed" && !input.successReason) throw contractError("skill_outcome_success_reason_missing", "completed skill outcome requires a success reason");
    if ((input.status === "failed" || input.status === "cancelled") && !input.failureReason) throw contractError("skill_outcome_failure_reason_missing", "failed/cancelled skill outcome requires a failure reason");
    if (input.policy.effect !== "allow" && input.status === "completed") throw contractError("skill_outcome_policy_contradiction", "completed skill outcome requires an allow decision");
    if (input.evidence.length > this.policy.maximumEvidencePerRecord) throw contractError("skill_outcome_evidence_limit", "skill outcome evidence exceeds policy limit");
    if (input.reuseConditions.length > this.policy.maximumReuseConditions) throw contractError("skill_outcome_reuse_condition_limit", "skill outcome reuse conditions exceed policy limit");
    const evidenceIds = new Set<string>();
    for (const evidence of input.evidence) {
      if (evidenceIds.has(evidence.evidenceId)) throw contractError("skill_outcome_duplicate_evidence", `duplicate evidence id ${evidence.evidenceId}`);
      evidenceIds.add(evidence.evidenceId);
      if (evidence.toolCallId && evidence.kind === "artifact") throw contractError("skill_outcome_artifact_tool_shape", "artifact evidence must use artifact_id instead of tool_call_id");
    }
    for (const condition of input.reuseConditions) {
      const missing = condition.sourceEvidenceIds.filter((id) => !evidenceIds.has(id));
      if (missing.length > 0) throw contractError("skill_outcome_condition_evidence_missing", "reuse condition references unknown evidence", { condition_id: condition.conditionId, missing_evidence_ids: missing });
    }
    const artifactIds = input.artifacts.map(artifactId).filter(Boolean);
    for (const evidence of input.evidence.filter((item) => item.kind === "artifact")) {
      if (evidence.artifactId && !artifactIds.includes(evidence.artifactId)) {
        throw contractError("skill_outcome_artifact_evidence_unbound", `artifact evidence ${evidence.evidenceId} is not present in the invocation result`);
      }
    }
    const toolEvidence = input.evidence.filter((item) => item.kind === "tool_call" || item.kind === "tool_result");
    if (input.toolCalls > 0 && toolEvidence.length === 0) throw contractError("skill_outcome_tool_evidence_missing", "reported tool calls require tool evidence");
    if (input.status === "completed" && input.toolCalls < this.policy.minimumSuccessfulToolCalls) {
      throw contractError("skill_outcome_minimum_tools", "completed outcome has too few successful tool calls for admission");
    }
  }

  private toRecord(input: SkillInvocationOutcomeInput, memoryId: string, observedAt: string): SkillInvocationOutcomeMemory {
    const artifactIds = uniqueStrings(input.artifacts.map(artifactId).filter(Boolean));
    const trusted = input.evidence.some((evidence) => evidence.trustedRuntime);
    const state: OutcomeMemoryState = input.status === "completed"
      && input.policy.effect === "allow"
      && (!this.policy.requireTrustedEvidence || trusted)
      ? "accepted"
      : "observed";
    const record: SkillInvocationOutcomeMemory = {
      memoryId,
      protocol: SKILL_OUTCOME_PROTOCOL,
      identity: cloneJson(input.identity),
      invocationId: input.invocationId,
      toolCallId: input.toolCallId,
      compositionId: input.compositionId,
      status: input.status,
      state,
      version: cloneJson(input.version),
      policy: cloneJson(input.policy),
      evidence: input.evidence.map(cloneJson),
      artifactIds,
      outputDigest: input.outputDigest,
      summary: input.summary,
      successReason: input.successReason,
      failureReason: input.failureReason,
      reuseConditions: input.reuseConditions.map(cloneJson),
      metrics: {
        inputTokens: input.inputTokens,
        outputTokens: input.outputTokens,
        toolCalls: input.toolCalls,
        costMicros: input.costMicros,
        durationMs: durationMilliseconds(input.startedAt, input.completedAt),
      },
      startedAt: input.startedAt,
      completedAt: input.completedAt,
      observedAt,
      sourceRecordDigest: input.sourceRecordDigest,
      recordDigest: "",
      revision: 1,
      metadata: cloneJson(input.metadata),
    };
    record.recordDigest = digest(recordProjection(record));
    return record;
  }

  private recordDigestFor(input: SkillInvocationOutcomeInput, memoryId: string, observedAt: string): string {
    return this.toRecord(input, memoryId, observedAt).recordDigest;
  }

  private quarantine(input: Partial<SkillInvocationOutcomeInput>, error: unknown): SkillOutcomeQuarantineRecord {
    const code = error && typeof error === "object" && "code" in error ? String((error as { code?: unknown }).code) : "skill_outcome_invalid";
    const message = error instanceof Error ? error.message : String(error);
    const rawIdentity = input.identity ?? this.identity;
    let identity: RuntimeIdentity;
    try { identity = normalizeIdentity(rawIdentity); } catch { identity = cloneJson(this.identity); }
    const inputDigest = digest(input);
    const quarantineId = stableId("skill-outcome-quarantine", input.invocationId, code, inputDigest);
    const existing = this.quarantines.get(quarantineId);
    if (existing) return existing;
    const quarantine: SkillOutcomeQuarantineRecord = {
      quarantineId,
      invocationId: String(input.invocationId ?? "").trim(),
      code,
      message: message.slice(0, 4_096),
      inputDigest,
      identity,
      observedAt: nowIso(this.now),
      details: error && typeof error === "object" && "details" in error ? sanitizeJsonObject((error as { details?: unknown }).details) : {},
    };
    this.quarantines.set(quarantineId, quarantine);
    return quarantine;
  }

  private receipt(input: {
    memoryId: string;
    invocationId: string;
    disposition: SkillOutcomeAdmissionReceipt["disposition"];
    reason: string;
    revisionBefore: number;
    incrementRevision?: boolean;
  }): SkillOutcomeAdmissionReceipt {
    if (input.incrementRevision !== false && this.revision === input.revisionBefore) this.revision += 1;
    const createdAt = nowIso(this.now);
    const unsigned = {
      memoryId: input.memoryId,
      invocationId: input.invocationId,
      disposition: input.disposition,
      reason: input.reason,
      revisionBefore: input.revisionBefore,
      revisionAfter: this.revision,
      stateDigest: this.stateDigest(),
      createdAt,
    };
    const receipt: SkillOutcomeAdmissionReceipt = {
      receiptId: stableId("skill-outcome-receipt", unsigned),
      ...unsigned,
    };
    this.receipts.push(receipt);
    return cloneJson(receipt);
  }

  private enforceRetention(): void {
    const ordered = [...this.records.values()].sort(outcomeIdentityOrder);
    const perSkill = new Map<string, SkillInvocationOutcomeMemory[]>();
    for (const record of ordered) {
      const values = perSkill.get(record.version.skillId) ?? [];
      values.push(record);
      perSkill.set(record.version.skillId, values);
    }
    const remove = new Set<string>();
    for (const values of perSkill.values()) {
      while (values.length > this.policy.maximumRecordsPerSkill) remove.add(values.shift()!.memoryId);
    }
    const survivors = ordered.filter((record) => !remove.has(record.memoryId));
    while (survivors.length > this.policy.maximumRecords) remove.add(survivors.shift()!.memoryId);
    for (const memoryId of remove) this.deleteRecord(memoryId);
  }

  private deleteRecord(memoryId: string): void {
    const record = this.records.get(memoryId);
    if (!record) return;
    this.records.delete(memoryId);
    this.invocationBindings.delete(record.invocationId);
    this.sourceDigestBindings.delete(record.sourceRecordDigest);
  }

  private require(memoryId: string): SkillInvocationOutcomeMemory {
    const record = this.records.get(required(memoryId, "skill outcome memory id"));
    if (!record) throw contractError("skill_outcome_not_found", `skill outcome memory not found: ${memoryId}`);
    return record;
  }

  private stateDigest(): string {
    return digest({
      identity: this.identity,
      policy: this.policy,
      revision: this.revision,
      records: [...this.records.values()].sort(outcomeIdentityOrder).map(recordProjection),
      quarantines: [...this.quarantines.values()].map((item) => item.inputDigest).sort(),
    });
  }

  private assertEnabled(): void {
    if (process.env.ZYRA_DISABLE_SKILL_MEMORY_RUNTIME === "1") {
      throw contractError("skill_memory_runtime_disabled", "06C skill outcome memory runtime is disabled");
    }
  }
}

function normalizeQuery(value: SkillOutcomeQuery): Required<SkillOutcomeQuery> {
  const occurredAfter = value.occurredAfter ? timestamp(value.occurredAfter, "outcome query after") : "";
  const occurredBefore = value.occurredBefore ? timestamp(value.occurredBefore, "outcome query before") : "";
  const limit = Number.isSafeInteger(value.limit) ? Math.max(0, Math.min(Number(value.limit), 100_000)) : 1_000;
  return {
    taskId: value.taskId?.trim() ?? "",
    sessionId: value.sessionId?.trim() ?? "",
    skillId: value.skillId?.trim() ?? "",
    skillName: value.skillName?.trim() ?? "",
    statuses: uniqueStatuses(value.statuses),
    states: uniqueStates(value.states),
    toolNames: uniqueStrings(value.toolNames),
    artifactIds: uniqueStrings(value.artifactIds),
    completedOnly: value.completedOnly ?? false,
    reusableOnly: value.reusableOnly ?? false,
    occurredAfter,
    occurredBefore,
    limit,
  };
}

function uniqueStatuses(values: SkillInvocationStatus[] | undefined): SkillInvocationStatus[] {
  return [...new Set((values ?? []).map(invocationStatus))];
}

function uniqueStates(values: OutcomeMemoryState[] | undefined): OutcomeMemoryState[] {
  return [...new Set((values ?? []).map((value) => {
    if (["observed", "accepted", "quarantined", "superseded"].includes(value)) return value;
    throw contractError("skill_outcome_state_invalid", `unsupported outcome state ${value}`);
  }))];
}

function invocationStatus(value: string): SkillInvocationStatus {
  if (["completed", "failed", "cancelled", "background"].includes(value)) return value as SkillInvocationStatus;
  throw contractError("skill_outcome_status_invalid", `unsupported invocation status ${value}`);
}

function nullableReason(value: string | null): string | null {
  const text = String(value ?? "").trim();
  return text ? text.slice(0, 8_192) : null;
}

function nonNegativeMetric(value: number, label: string): number {
  if (!Number.isSafeInteger(value) || value < 0) throw contractError("skill_outcome_metric_invalid", `${label} must be a non-negative integer`);
  return value;
}

function artifactId(value: JsonObject): string {
  return String(value.artifact_id ?? value.artifactId ?? value.id ?? "").trim();
}

function isReusable(record: SkillInvocationOutcomeMemory, policy: SkillMemoryPolicy): boolean {
  if (record.state !== "accepted") return false;
  if (policy.requireCompletedForReuse && record.status !== "completed") return false;
  if (record.policy.effect !== "allow") return false;
  if (record.metrics.toolCalls < policy.minimumSuccessfulToolCalls) return false;
  if (policy.requireTrustedEvidence && !record.evidence.some((item) => item.trustedRuntime)) return false;
  return record.reuseConditions.every((condition) => condition.sourceEvidenceIds.every((id) => record.evidence.some((item) => item.evidenceId === id)));
}

function conditionMatches(condition: ReuseCondition, facts: JsonObject): boolean {
  const value = readPath(facts, condition.key);
  if (condition.operator === "present") return value !== undefined && value !== null && value !== "";
  if (condition.operator === "absent") return value === undefined || value === null || value === "";
  if (condition.operator === "equals") return digest(value) === digest(condition.value);
  if (condition.operator === "contains") {
    if (Array.isArray(value)) return value.some((item) => digest(item) === digest(condition.value));
    return String(value ?? "").includes(String(condition.value ?? ""));
  }
  if (condition.operator === "in") {
    return Array.isArray(condition.value) && condition.value.some((item) => digest(item) === digest(value));
  }
  if (condition.operator === "matches") {
    try { return new RegExp(String(condition.value), "i").test(String(value ?? "")); } catch { return false; }
  }
  return false;
}

function readPath(value: JsonObject, path: string): unknown {
  let current: unknown = value;
  for (const segment of path.split(".").filter(Boolean)) {
    if (!current || typeof current !== "object" || Array.isArray(current)) return undefined;
    current = (current as Record<string, unknown>)[segment];
  }
  return current;
}

function outcomeOrder(left: SkillInvocationOutcomeMemory, right: SkillInvocationOutcomeMemory): number {
  const completed = (right.completedAt ?? right.observedAt).localeCompare(left.completedAt ?? left.observedAt);
  if (completed !== 0) return completed;
  const success = Number(right.status === "completed") - Number(left.status === "completed");
  if (success !== 0) return success;
  return left.memoryId.localeCompare(right.memoryId);
}

function outcomeIdentityOrder(left: SkillInvocationOutcomeMemory, right: SkillInvocationOutcomeMemory): number {
  const observed = left.observedAt.localeCompare(right.observedAt);
  return observed !== 0 ? observed : left.memoryId.localeCompare(right.memoryId);
}

function evidenceOrder(left: OutcomeEvidenceReference, right: OutcomeEvidenceReference): number {
  if (left.sequence !== right.sequence) return left.sequence - right.sequence;
  const occurred = left.occurredAt.localeCompare(right.occurredAt);
  return occurred !== 0 ? occurred : left.evidenceId.localeCompare(right.evidenceId);
}

function recordProjection(record: SkillInvocationOutcomeMemory): JsonObject {
  return canonicalize({
    memoryId: record.memoryId,
    protocol: record.protocol,
    identity: record.identity,
    invocationId: record.invocationId,
    toolCallId: record.toolCallId,
    compositionId: record.compositionId,
    status: record.status,
    state: record.state,
    version: record.version,
    policy: record.policy,
    evidence: record.evidence,
    artifactIds: record.artifactIds,
    outputDigest: record.outputDigest,
    summary: record.summary,
    successReason: record.successReason,
    failureReason: record.failureReason,
    reuseConditions: record.reuseConditions,
    metrics: record.metrics,
    startedAt: record.startedAt,
    completedAt: record.completedAt,
    observedAt: record.observedAt,
    sourceRecordDigest: record.sourceRecordDigest,
    revision: record.revision,
    metadata: record.metadata,
  }) as JsonObject;
}

function validateRestoredRecord(value: SkillInvocationOutcomeMemory): SkillInvocationOutcomeMemory {
  const record = cloneJson(value);
  if (record.protocol !== SKILL_OUTCOME_PROTOCOL) throw contractError("skill_outcome_record_protocol", "restored outcome has unsupported protocol");
  normalizeIdentity(record.identity);
  normalizeSkillVersion(record.version);
  normalizePolicyDecision(record.policy);
  record.evidence.map(normalizeEvidence);
  record.reuseConditions.map(normalizeReuseCondition);
  sha256Digest(record.outputDigest, "restored output digest");
  sha256Digest(record.sourceRecordDigest, "restored source record digest");
  if (digest(recordProjection(record)) !== record.recordDigest) throw contractError("skill_outcome_record_digest", `outcome record digest mismatch: ${record.memoryId}`);
  return record;
}
