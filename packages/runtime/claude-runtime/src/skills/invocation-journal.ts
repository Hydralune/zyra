import type { JsonObject, JsonValue } from "../contracts.ts";
import { canonicalize, cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type { SkillInvocationPlan, SkillInvocationResult } from "./contracts-v2.ts";

export type SkillJournalStatus = "prepared" | "executing" | "effect_recorded" | "committed" | "acknowledged" | "failed" | "cancelled" | "indeterminate";

export interface SkillInvocationJournalRecord {
  journalId: string;
  transitionId: string;
  invocationId: string;
  skillId: string;
  registryRevision: number;
  descriptorDigest: string;
  planDigest: string;
  idempotencyKey: string;
  status: SkillJournalStatus;
  sequence: number;
  epoch: number;
  effectId: string | null;
  effectDigest: string | null;
  effect: JsonObject | null;
  resultDigest: string | null;
  result: SkillInvocationResult | null;
  failure: JsonObject | null;
  preparedAt: string;
  startedAt: string | null;
  effectAt: string | null;
  committedAt: string | null;
  acknowledgedAt: string | null;
  metadata: JsonObject;
  previousHash: string;
  recordHash: string;
}

export interface SkillInvocationJournalSnapshot {
  version: "zyra.skill-invocation-journal/v1";
  sequence: number;
  revision: number;
  epoch: number;
  headHash: string;
  records: SkillInvocationJournalRecord[];
  digest: string;
  capturedAt: string;
}

const genesis = "sha256:zyra-skill-invocation-genesis";

export class SkillInvocationJournal {
  private readonly records = new Map<string, SkillInvocationJournalRecord>();
  private readonly byInvocation = new Map<string, string>();
  private readonly byIdempotency = new Map<string, string>();
  private readonly now: () => Date;
  private readonly maximumRecords: number;
  private sequence = 0;
  private revision = 0;
  private epoch: number;
  private headHash = genesis;
  private lastTimestamp: string | null = null;

  constructor(options: { epoch: number; now?: () => Date; maximumRecords?: number; snapshot?: SkillInvocationJournalSnapshot | null }) {
    if (!Number.isSafeInteger(options.epoch) || options.epoch < 0) throw new Error("skill journal epoch is invalid");
    this.epoch = options.epoch;
    this.now = options.now ?? (() => new Date());
    this.maximumRecords = options.maximumRecords ?? 100_000;
    if (options.snapshot) this.restore(options.snapshot, options.epoch);
  }

  prepare(planValue: SkillInvocationPlan): SkillInvocationJournalRecord {
    const plan = cloneJson(planValue);
    const planDigest = digest(plan);
    const idempotencyKey = deterministicId("skill-invocation-idempotency", {
      invocation_id: plan.invocationId,
      skill_id: plan.skillId,
      registry_revision: plan.registryRevision,
      descriptor_digest: plan.descriptorDigest,
      arguments_digest: digest(plan.arguments),
    }, 40);
    const existingId = this.byIdempotency.get(idempotencyKey);
    if (existingId) {
      const existing = this.records.get(existingId)!;
      if (existing.planDigest !== planDigest) throw new Error(`skill invocation idempotency conflict ${idempotencyKey}`);
      return cloneJson(existing);
    }
    const transitionId = deterministicId("skill-invocation-transition", {
      invocation_id: plan.invocationId,
      idempotency_key: idempotencyKey,
      epoch: this.epoch,
    }, 40);
    this.sequence += 1;
    const preparedAt = this.timestamp();
    const base = {
      transitionId,
      invocationId: plan.invocationId,
      skillId: plan.skillId,
      registryRevision: plan.registryRevision,
      descriptorDigest: plan.descriptorDigest,
      planDigest,
      idempotencyKey,
      status: "prepared" as const,
      sequence: this.sequence,
      epoch: this.epoch,
      effectId: null,
      effectDigest: null,
      effect: null,
      resultDigest: null,
      result: null,
      failure: null,
      preparedAt,
      startedAt: null,
      effectAt: null,
      committedAt: null,
      acknowledgedAt: null,
      metadata: {},
      previousHash: this.headHash,
    };
    const journalId = deterministicId("skill-invocation-journal", base, 40);
    const record: SkillInvocationJournalRecord = { journalId, ...base, recordHash: digest({ journalId, ...base }) };
    this.records.set(journalId, record);
    this.byInvocation.set(plan.invocationId, journalId);
    this.byIdempotency.set(idempotencyKey, journalId);
    this.headHash = record.recordHash;
    this.revision += 1;
    this.trim();
    return cloneJson(record);
  }

  begin(journalId: string, transitionId: string): SkillInvocationJournalRecord {
    const record = this.requireBound(journalId, transitionId);
    if (record.status === "executing") return cloneJson(record);
    if (record.status !== "prepared" && record.status !== "indeterminate") throw new Error(`skill journal ${journalId} cannot begin from ${record.status}`);
    return this.rewrite(record, { status: "executing", startedAt: this.timestamp(), failure: null });
  }

  recordEffect(journalId: string, transitionId: string, kind: string, value: JsonValue, metadata: JsonObject = {}): SkillInvocationJournalRecord {
    const record = this.requireBound(journalId, transitionId);
    const effect = canonicalize({ kind, value, metadata }) as JsonObject;
    const effectDigest = digest(effect);
    const effectId = deterministicId("skill-invocation-effect", { transition_id: transitionId, effect_digest: effectDigest }, 40);
    if (record.effectId) {
      if (record.effectId !== effectId || record.effectDigest !== effectDigest) throw new Error(`skill journal ${journalId} has conflicting effect receipt`);
      return cloneJson(record);
    }
    if (record.status !== "executing" && record.status !== "effect_recorded") throw new Error(`skill journal ${journalId} cannot record effect from ${record.status}`);
    return this.rewrite(record, { status: "effect_recorded", effectId, effectDigest, effect, effectAt: this.timestamp() });
  }

  commit(journalId: string, transitionId: string, resultValue: SkillInvocationResult): SkillInvocationJournalRecord {
    const record = this.requireBound(journalId, transitionId);
    const result = cloneJson(resultValue);
    if (result.invocationId !== record.invocationId || result.skillId !== record.skillId) throw new Error("skill result binding mismatch");
    const resultDigest = digest(result);
    if (record.status === "committed" || record.status === "acknowledged") {
      if (record.resultDigest !== resultDigest) throw new Error(`skill journal ${journalId} committed result conflict`);
      return cloneJson(record);
    }
    if (record.status !== "executing" && record.status !== "effect_recorded" && record.status !== "indeterminate") throw new Error(`skill journal ${journalId} cannot commit from ${record.status}`);
    return this.rewrite(record, { status: "committed", result, resultDigest, failure: result.failure, committedAt: this.timestamp() });
  }

  fail(journalId: string, transitionId: string, error: unknown, cancelled = false): SkillInvocationJournalRecord {
    const record = this.requireBound(journalId, transitionId);
    if (record.status === "committed" || record.status === "acknowledged") throw new Error(`cannot fail committed skill journal ${journalId}`);
    const failure = { name: error instanceof Error ? error.name : "Error", message: error instanceof Error ? error.message : String(error) };
    return this.rewrite(record, { status: cancelled ? "cancelled" : "failed", failure, committedAt: this.timestamp() });
  }

  acknowledge(journalId: string, transitionId: string, resultDigest: string): SkillInvocationJournalRecord {
    const record = this.requireBound(journalId, transitionId);
    if (record.status === "acknowledged") return cloneJson(record);
    if (record.status !== "committed" || record.resultDigest !== resultDigest) throw new Error(`skill journal ${journalId} acknowledgement mismatch`);
    return this.rewrite(record, { status: "acknowledged", acknowledgedAt: this.timestamp() });
  }

  reconcile(journalId: string, observedEffectDigest: string | null, observedResult: SkillInvocationResult | null): SkillInvocationJournalRecord {
    const record = this.records.get(journalId);
    if (!record) throw new Error(`skill journal ${journalId} was not found`);
    if (record.status !== "indeterminate" && record.status !== "executing" && record.status !== "effect_recorded") return cloneJson(record);
    if (record.effectDigest && observedEffectDigest && record.effectDigest !== observedEffectDigest) throw new Error(`skill journal ${journalId} reconciliation effect mismatch`);
    if (observedResult) return this.commit(journalId, record.transitionId, observedResult);
    if (record.effectId || observedEffectDigest) return this.rewrite(record, { status: "indeterminate", metadata: { ...record.metadata, reconciliation_required: true, observed_effect_digest: observedEffectDigest } });
    return this.rewrite(record, { status: "prepared", startedAt: null, metadata: { ...record.metadata, safe_to_retry: true } });
  }

  get(journalId: string): SkillInvocationJournalRecord | null {
    const value = this.records.get(journalId);
    return value ? cloneJson(value) : null;
  }

  findInvocation(invocationId: string): SkillInvocationJournalRecord | null {
    const id = this.byInvocation.get(invocationId);
    return id ? this.get(id) : null;
  }

  pending(): SkillInvocationJournalRecord[] {
    return [...this.records.values()].filter((record) => ["prepared", "executing", "effect_recorded", "indeterminate"].includes(record.status)).sort((left, right) => left.sequence - right.sequence).map(cloneJson);
  }

  snapshot(): SkillInvocationJournalSnapshot {
    const records = [...this.records.values()].sort((left, right) => left.sequence - right.sequence).map(cloneJson);
    let head = genesis;
    for (const record of records) {
      record.previousHash = head;
      const { recordHash: _hash, ...payload } = record;
      record.recordHash = digest(payload);
      head = record.recordHash;
    }
    const withoutDigest = {
      version: "zyra.skill-invocation-journal/v1" as const,
      sequence: this.sequence,
      revision: this.revision,
      epoch: this.epoch,
      headHash: head,
      records,
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: SkillInvocationJournalSnapshot, targetEpoch: number): void {
    if (snapshot.version !== "zyra.skill-invocation-journal/v1") throw new Error("unsupported skill invocation journal snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw new Error("skill invocation journal snapshot digest mismatch");
    this.records.clear();
    this.byInvocation.clear();
    this.byIdempotency.clear();
    let head = genesis;
    let sequence = 0;
    for (const value of [...snapshot.records].sort((left, right) => left.sequence - right.sequence)) {
      if (value.previousHash !== head || value.sequence <= sequence) throw new Error("skill invocation journal chain is broken");
      const { recordHash, ...payload } = value;
      if (digest(payload) !== recordHash) throw new Error(`skill journal ${value.journalId} hash mismatch`);
      const record = cloneJson(value);
      if (record.status === "executing" || record.status === "effect_recorded") {
        record.status = "indeterminate";
        record.metadata = { ...record.metadata, restored_from_epoch: snapshot.epoch, reconciliation_required: Boolean(record.effectId) };
      }
      this.records.set(record.journalId, record);
      this.byInvocation.set(record.invocationId, record.journalId);
      this.byIdempotency.set(record.idempotencyKey, record.journalId);
      head = value.recordHash;
      sequence = value.sequence;
    }
    if (head !== snapshot.headHash) throw new Error("skill invocation journal head mismatch");
    this.sequence = snapshot.sequence;
    this.revision = snapshot.revision;
    this.epoch = targetEpoch;
    this.headHash = snapshot.headHash;
  }

  private rewrite(record: SkillInvocationJournalRecord, patch: Partial<SkillInvocationJournalRecord>): SkillInvocationJournalRecord {
    Object.assign(record, cloneJson(patch));
    record.previousHash = this.headHash;
    const { recordHash: _hash, ...payload } = record;
    record.recordHash = digest(payload);
    this.headHash = record.recordHash;
    this.revision += 1;
    return cloneJson(record);
  }

  private requireBound(journalId: string, transitionId: string): SkillInvocationJournalRecord {
    const record = this.records.get(journalId);
    if (!record || record.transitionId !== transitionId) throw new Error(`skill journal binding mismatch for ${journalId}`);
    return record;
  }

  private trim(): void {
    if (this.records.size <= this.maximumRecords) return;
    for (const [id, record] of this.records) {
      if (this.records.size <= this.maximumRecords) break;
      if (["acknowledged", "failed", "cancelled"].includes(record.status)) {
        this.records.delete(id);
        this.byInvocation.delete(record.invocationId);
        this.byIdempotency.delete(record.idempotencyKey);
      }
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}
