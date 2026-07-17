import type { JsonObject } from "../contracts.ts";
import {
  canonicalize,
  cloneJson,
  constantTimeDigestEquals,
  deterministicId,
  digest,
  optionalObject,
} from "./canonical.ts";
import type {
  CommitReceipt,
  E02Clock,
  E02RuntimeIdentity,
  EffectReceipt,
  EntityRevisionState,
  TransitionCommitInput,
  TransitionJournalSnapshot,
  TransitionPrepareInput,
  TransitionPrepareResult,
  TransitionRecord,
} from "./contracts.ts";

const EMPTY_STATE_HASH = digest({});

export class TransitionConflictError extends Error {
  readonly code: string;
  readonly transitionId: string | null;
  readonly entityId: string | null;

  constructor(code: string, message: string, transitionId: string | null = null, entityId: string | null = null) {
    super(message);
    this.name = "TransitionConflictError";
    this.code = code;
    this.transitionId = transitionId;
    this.entityId = entityId;
  }
}

export class DurableTransitionJournal {
  readonly runtime: E02RuntimeIdentity;
  private readonly clock: E02Clock;
  private sequence = 0;
  private readonly entities = new Map<string, EntityRevisionState>();
  private readonly pending = new Map<string, TransitionRecord>();
  private readonly committed = new Map<string, TransitionRecord>();
  private readonly rejected = new Map<string, TransitionRecord>();
  private readonly idempotency = new Map<string, string>();
  private readonly transitionHashes = new Map<string, string>();

  constructor(runtime: E02RuntimeIdentity, clock: E02Clock = () => new Date().toISOString()) {
    this.runtime = cloneJson(runtime);
    this.clock = clock;
  }

  prepare(input: TransitionPrepareInput): TransitionPrepareResult {
    this.validatePrepare(input);
    const payload = canonicalize(input.payload) as JsonObject;
    const payloadHash = digest(payload);
    const idempotencyKey = input.idempotencyKey || deterministicId("e02-idempotency", {
      domain: input.domain,
      operation: input.operation,
      binding: input.binding,
      payloadHash,
    }, 40);
    const existingTransitionId = this.idempotency.get(idempotencyKey);
    if (existingTransitionId) {
      const existing = this.pending.get(existingTransitionId)
        ?? this.committed.get(existingTransitionId)
        ?? this.rejected.get(existingTransitionId);
      if (!existing) throw new TransitionConflictError("idempotency_index_corrupt", "idempotency key points to a missing transition");
      if (!constantTimeDigestEquals(existing.payloadHash, payloadHash)) {
        throw new TransitionConflictError(
          "idempotency_payload_mismatch",
          "idempotency key was reused with a different payload",
          existing.transitionId,
          existing.binding.entityId,
        );
      }
      this.assertSameBinding(existing, input);
      if (existing.phase === "committed" || existing.phase === "acknowledged") {
        if (input.replayPolicy !== "return-committed" && input.replayPolicy !== "resume-pending-or-return-committed") {
          throw new TransitionConflictError(
            "duplicate_committed_transition",
            "committed transition cannot be prepared again under this replay policy",
            existing.transitionId,
            existing.binding.entityId,
          );
        }
        return { kind: "return_committed", transition: cloneJson(existing), committedReceipt: cloneJson(existing.commitReceipt) };
      }
      if (existing.phase === "rejected" || existing.phase === "cancelled") {
        throw new TransitionConflictError(
          "terminal_transition_replay",
          `transition is already ${existing.phase}`,
          existing.transitionId,
          existing.binding.entityId,
        );
      }
      if (input.replayPolicy !== "resume-pending" && input.replayPolicy !== "resume-pending-or-return-committed") {
        throw new TransitionConflictError(
          "duplicate_pending_transition",
          "pending transition cannot be prepared again under this replay policy",
          existing.transitionId,
          existing.binding.entityId,
        );
      }
      return { kind: "resume_pending", transition: cloneJson(existing), committedReceipt: null };
    }
    const entity = this.entity(input.binding.entityId);
    if (input.binding.expectedRevision !== entity.revision) {
      throw new TransitionConflictError(
        "revision_conflict",
        `expected entity revision ${input.binding.expectedRevision}, observed ${entity.revision}`,
        null,
        entity.entityId,
      );
    }
    const transitionId = deterministicId("e02-transition", {
      runtimeId: this.runtime.runtimeId,
      sequence: this.sequence + 1,
      idempotencyKey,
      payloadHash,
      entity,
    }, 48);
    const now = this.clock();
    const transition: TransitionRecord = {
      transitionId,
      idempotencyKey,
      domain: input.domain,
      operation: input.operation,
      binding: cloneJson(input.binding),
      payload,
      payloadHash,
      replayPolicy: input.replayPolicy,
      nonIdempotentEffect: input.nonIdempotentEffect,
      phase: "prepared",
      revisionBefore: entity.revision,
      revisionAfter: null,
      stateHashBefore: entity.stateHash,
      stateHashAfter: null,
      effectReceipt: null,
      commitReceipt: null,
      rejectionCode: null,
      rejectionMessage: null,
      preparedAt: now,
      updatedAt: now,
      acknowledgedAt: null,
      epochPrepared: this.runtime.epoch,
      epochCommitted: null,
      metadata: optionalObject(input.metadata),
    };
    this.sequence += 1;
    this.pending.set(transitionId, transition);
    this.idempotency.set(idempotencyKey, transitionId);
    this.transitionHashes.set(transitionId, this.recordHash(transition));
    return { kind: "prepared", transition: cloneJson(transition), committedReceipt: null };
  }

  beginEffect(transitionId: string): TransitionRecord {
    const transition = this.requiredPending(transitionId);
    if (transition.phase === "effect_started") return cloneJson(transition);
    if (transition.phase !== "prepared") {
      throw new TransitionConflictError(
        "effect_phase_invalid",
        `cannot begin effect from ${transition.phase}`,
        transitionId,
        transition.binding.entityId,
      );
    }
    transition.phase = "effect_started";
    transition.updatedAt = this.clock();
    this.refreshHash(transition);
    return cloneJson(transition);
  }

  recordEffect(
    transitionId: string,
    input: Omit<EffectReceipt, "transitionId" | "effectId" | "performedAt"> & {
      effectId?: string;
      performedAt?: string;
    },
  ): EffectReceipt {
    const transition = this.requiredPending(transitionId);
    if (transition.effectReceipt) {
      const candidate = this.effectReceipt(transition, input);
      if (!constantTimeDigestEquals(digest(transition.effectReceipt), digest(candidate))) {
        throw new TransitionConflictError(
          "effect_receipt_conflict",
          "effect receipt differs from the already recorded receipt",
          transitionId,
          transition.binding.entityId,
        );
      }
      return cloneJson(transition.effectReceipt);
    }
    if (transition.phase !== "effect_started" && transition.phase !== "prepared") {
      throw new TransitionConflictError(
        "effect_record_phase_invalid",
        `cannot record effect from ${transition.phase}`,
        transitionId,
        transition.binding.entityId,
      );
    }
    const receipt = this.effectReceipt(transition, input);
    if (!constantTimeDigestEquals(receipt.requestHash, transition.payloadHash)) {
      throw new TransitionConflictError(
        "effect_request_hash_mismatch",
        "effect receipt is not bound to the prepared payload",
        transitionId,
        transition.binding.entityId,
      );
    }
    transition.effectReceipt = receipt;
    transition.phase = "effect_recorded";
    transition.updatedAt = this.clock();
    this.refreshHash(transition);
    return cloneJson(receipt);
  }

  recordReceipt(transitionId: string, metadata: JsonObject = {}): TransitionRecord {
    const transition = this.requiredPending(transitionId);
    if (transition.nonIdempotentEffect && !transition.effectReceipt) {
      throw new TransitionConflictError(
        "missing_effect_receipt",
        "non-idempotent transition cannot record logical receipt before effect receipt",
        transitionId,
        transition.binding.entityId,
      );
    }
    if (!["prepared", "effect_recorded", "receipt_recorded"].includes(transition.phase)) {
      throw new TransitionConflictError(
        "receipt_phase_invalid",
        `cannot record receipt from ${transition.phase}`,
        transitionId,
        transition.binding.entityId,
      );
    }
    transition.phase = "receipt_recorded";
    transition.metadata = { ...transition.metadata, ...cloneJson(metadata) };
    transition.updatedAt = this.clock();
    this.refreshHash(transition);
    return cloneJson(transition);
  }

  commit(input: TransitionCommitInput): CommitReceipt {
    const alreadyCommitted = this.committed.get(input.transitionId);
    if (alreadyCommitted?.commitReceipt) {
      const outputHash = digest(input.output);
      const stateHash = digest(input.nextState);
      if (
        !constantTimeDigestEquals(outputHash, alreadyCommitted.commitReceipt.outputHash)
        || !constantTimeDigestEquals(stateHash, alreadyCommitted.commitReceipt.stateHashAfter)
      ) {
        throw new TransitionConflictError(
          "commit_replay_mismatch",
          "commit replay differs from the committed output or state",
          input.transitionId,
          alreadyCommitted.binding.entityId,
        );
      }
      return cloneJson(alreadyCommitted.commitReceipt);
    }
    const transition = this.requiredPending(input.transitionId);
    if (transition.nonIdempotentEffect && !transition.effectReceipt) {
      throw new TransitionConflictError(
        "commit_without_effect_receipt",
        "non-idempotent transition cannot commit without an effect receipt",
        transition.transitionId,
        transition.binding.entityId,
      );
    }
    if (!["prepared", "effect_recorded", "receipt_recorded"].includes(transition.phase)) {
      throw new TransitionConflictError(
        "commit_phase_invalid",
        `cannot commit transition from ${transition.phase}`,
        transition.transitionId,
        transition.binding.entityId,
      );
    }
    const entity = this.entity(transition.binding.entityId);
    const expectedRevision = input.expectedRevision ?? transition.binding.expectedRevision;
    if (entity.revision !== expectedRevision || transition.revisionBefore !== expectedRevision) {
      throw new TransitionConflictError(
        "commit_revision_conflict",
        `commit expected revision ${expectedRevision}, observed ${entity.revision}`,
        transition.transitionId,
        entity.entityId,
      );
    }
    if (!constantTimeDigestEquals(entity.stateHash, transition.stateHashBefore)) {
      throw new TransitionConflictError(
        "commit_state_hash_conflict",
        "entity state changed after transition prepare",
        transition.transitionId,
        entity.entityId,
      );
    }
    const nextState = canonicalize(input.nextState) as JsonObject;
    const output = canonicalize(input.output) as JsonObject;
    const revisionAfter = entity.revision + 1;
    const stateHashAfter = digest(nextState);
    const outputHash = digest(output);
    const effectReceiptHash = transition.effectReceipt ? digest(transition.effectReceipt) : null;
    const committedAt = this.clock();
    const receiptBase = {
      transitionId: transition.transitionId,
      entityId: entity.entityId,
      domain: transition.domain,
      operation: transition.operation,
      revisionBefore: entity.revision,
      revisionAfter,
      stateHashBefore: entity.stateHash,
      stateHashAfter,
      payloadHash: transition.payloadHash,
      effectReceiptHash,
      outputHash,
      output,
      committedAt,
    };
    const receipt: CommitReceipt = {
      ...receiptBase,
      commitHash: digest(receiptBase),
    };
    transition.phase = "committed";
    transition.revisionAfter = revisionAfter;
    transition.stateHashAfter = stateHashAfter;
    transition.commitReceipt = receipt;
    transition.epochCommitted = this.runtime.epoch;
    transition.updatedAt = committedAt;
    transition.metadata = { ...transition.metadata, ...optionalObject(input.metadata) };
    this.entities.set(entity.entityId, {
      entityId: entity.entityId,
      revision: revisionAfter,
      stateHash: stateHashAfter,
      lastTransitionId: transition.transitionId,
      updatedAt: committedAt,
    });
    this.pending.delete(transition.transitionId);
    this.committed.set(transition.transitionId, transition);
    this.refreshHash(transition);
    return cloneJson(receipt);
  }

  acknowledge(transitionId: string, commitHash: string): CommitReceipt {
    const transition = this.committed.get(transitionId);
    if (!transition?.commitReceipt) {
      throw new TransitionConflictError("ack_unknown_transition", "cannot acknowledge unknown committed transition", transitionId);
    }
    if (!constantTimeDigestEquals(transition.commitReceipt.commitHash, commitHash)) {
      throw new TransitionConflictError(
        "ack_commit_hash_mismatch",
        "acknowledgement commit hash does not match the canonical receipt",
        transitionId,
        transition.binding.entityId,
      );
    }
    if (transition.phase !== "acknowledged") {
      transition.phase = "acknowledged";
      transition.acknowledgedAt = this.clock();
      transition.updatedAt = transition.acknowledgedAt;
      this.refreshHash(transition);
    }
    return cloneJson(transition.commitReceipt);
  }

  reject(transitionId: string, code: string, message: string): TransitionRecord {
    const transition = this.requiredPending(transitionId);
    if (transition.effectReceipt?.ok && transition.nonIdempotentEffect) {
      throw new TransitionConflictError(
        "reject_after_successful_effect",
        "successful non-idempotent effect must be reconciled and committed, not rejected",
        transitionId,
        transition.binding.entityId,
      );
    }
    transition.phase = "rejected";
    transition.rejectionCode = code;
    transition.rejectionMessage = message;
    transition.updatedAt = this.clock();
    this.pending.delete(transitionId);
    this.rejected.set(transitionId, transition);
    this.refreshHash(transition);
    return cloneJson(transition);
  }

  cancel(transitionId: string, reason: string): TransitionRecord {
    const transition = this.requiredPending(transitionId);
    if (transition.effectReceipt?.ok && transition.nonIdempotentEffect) {
      throw new TransitionConflictError(
        "cancel_after_successful_effect",
        "successful non-idempotent effect must be reconciled before cancellation",
        transitionId,
        transition.binding.entityId,
      );
    }
    transition.phase = "cancelled";
    transition.rejectionCode = "cancelled";
    transition.rejectionMessage = reason;
    transition.updatedAt = this.clock();
    this.pending.delete(transitionId);
    this.rejected.set(transitionId, transition);
    this.refreshHash(transition);
    return cloneJson(transition);
  }

  committedReceiptByIdempotency(idempotencyKey: string): CommitReceipt | null {
    const transitionId = this.idempotency.get(idempotencyKey);
    const transition = transitionId ? this.committed.get(transitionId) : undefined;
    return transition?.commitReceipt ? cloneJson(transition.commitReceipt) : null;
  }

  transition(transitionId: string): TransitionRecord | null {
    const transition = this.pending.get(transitionId)
      ?? this.committed.get(transitionId)
      ?? this.rejected.get(transitionId);
    return transition ? cloneJson(transition) : null;
  }

  entityState(entityId: string): EntityRevisionState {
    return cloneJson(this.entity(entityId));
  }

  pendingTransitions(): TransitionRecord[] {
    return [...this.pending.values()].map(cloneJson).sort((left, right) => left.preparedAt.localeCompare(right.preparedAt));
  }

  recoverableTransitions(): TransitionRecord[] {
    return this.pendingTransitions().filter((transition) =>
      transition.phase === "effect_recorded"
      || transition.phase === "receipt_recorded"
      || (!transition.nonIdempotentEffect && transition.phase === "prepared")
    );
  }

  snapshot(): TransitionJournalSnapshot {
    const base = {
      version: "zyra.e02-transition-journal/v1" as const,
      runtime: cloneJson(this.runtime),
      sequence: this.sequence,
      entities: [...this.entities.values()].map(cloneJson).sort((left, right) => left.entityId.localeCompare(right.entityId)),
      pending: [...this.pending.values()].map(cloneJson).sort((left, right) => left.transitionId.localeCompare(right.transitionId)),
      committed: [...this.committed.values()].map(cloneJson).sort((left, right) => left.transitionId.localeCompare(right.transitionId)),
      rejected: [...this.rejected.values()].map(cloneJson).sort((left, right) => left.transitionId.localeCompare(right.transitionId)),
      idempotency: [...this.idempotency.entries()].sort(([left], [right]) => left.localeCompare(right)),
      transitionHashes: [...this.transitionHashes.entries()].sort(([left], [right]) => left.localeCompare(right)),
    };
    return { ...base, snapshotHash: digest(base) };
  }

  restore(snapshot: TransitionJournalSnapshot, targetEpoch = this.runtime.epoch): void {
    if (snapshot.version !== "zyra.e02-transition-journal/v1") throw new Error("unsupported transition journal snapshot");
    const expectedHash = digest({ ...snapshot, snapshotHash: undefined });
    const normalizedExpected = digest({
      version: snapshot.version,
      runtime: snapshot.runtime,
      sequence: snapshot.sequence,
      entities: snapshot.entities,
      pending: snapshot.pending,
      committed: snapshot.committed,
      rejected: snapshot.rejected,
      idempotency: snapshot.idempotency,
      transitionHashes: snapshot.transitionHashes,
    });
    void expectedHash;
    if (!constantTimeDigestEquals(normalizedExpected, snapshot.snapshotHash)) throw new Error("transition journal snapshot hash mismatch");
    if (snapshot.runtime.sessionId !== this.runtime.sessionId) throw new Error("transition journal session mismatch");
    if (snapshot.runtime.runId !== this.runtime.runId) throw new Error("transition journal run mismatch");
    if (targetEpoch <= snapshot.runtime.epoch) throw new Error("restore target epoch must advance the source epoch");
    this.sequence = snapshot.sequence;
    this.entities.clear();
    this.pending.clear();
    this.committed.clear();
    this.rejected.clear();
    this.idempotency.clear();
    this.transitionHashes.clear();
    for (const entity of snapshot.entities) this.entities.set(entity.entityId, cloneJson(entity));
    for (const transition of snapshot.pending) this.pending.set(transition.transitionId, cloneJson(transition));
    for (const transition of snapshot.committed) this.committed.set(transition.transitionId, cloneJson(transition));
    for (const transition of snapshot.rejected) this.rejected.set(transition.transitionId, cloneJson(transition));
    for (const [key, transitionId] of snapshot.idempotency) this.idempotency.set(key, transitionId);
    for (const [transitionId, hash] of snapshot.transitionHashes) this.transitionHashes.set(transitionId, hash);
    this.verifyIndexes();
  }

  verifyIndexes(): void {
    const all = new Map<string, TransitionRecord>();
    for (const transition of [...this.pending.values(), ...this.committed.values(), ...this.rejected.values()]) {
      if (all.has(transition.transitionId)) throw new Error(`duplicate transition ${transition.transitionId}`);
      all.set(transition.transitionId, transition);
      const expectedHash = this.recordHash(transition);
      const observedHash = this.transitionHashes.get(transition.transitionId);
      if (!observedHash || !constantTimeDigestEquals(expectedHash, observedHash)) {
        throw new Error(`transition hash mismatch for ${transition.transitionId}`);
      }
    }
    for (const [key, transitionId] of this.idempotency) {
      const transition = all.get(transitionId);
      if (!transition || transition.idempotencyKey !== key) throw new Error(`invalid idempotency index ${key}`);
    }
    for (const entity of this.entities.values()) {
      if (entity.lastTransitionId && !this.committed.has(entity.lastTransitionId)) {
        throw new Error(`entity ${entity.entityId} points to a non-committed transition`);
      }
    }
  }

  private validatePrepare(input: TransitionPrepareInput): void {
    if (!input.domain || !input.operation) throw new Error("transition domain and operation are required");
    const binding = input.binding;
    for (const [label, value] of Object.entries({
      runId: binding.runId,
      taskId: binding.taskId,
      sessionId: binding.sessionId,
      workerRequestId: binding.workerRequestId,
      requestId: binding.requestId,
      entityId: binding.entityId,
    })) {
      if (!value) throw new Error(`transition binding ${label} is required`);
    }
    if (binding.runId !== this.runtime.runId) throw new Error("transition run binding mismatch");
    if (binding.sessionId !== this.runtime.sessionId) throw new Error("transition session binding mismatch");
    if (binding.workerRequestId !== this.runtime.workerRequestId) throw new Error("transition worker request binding mismatch");
    if (!Number.isSafeInteger(binding.expectedRevision) || binding.expectedRevision < 0) {
      throw new Error("transition expected revision must be a non-negative safe integer");
    }
  }

  private assertSameBinding(existing: TransitionRecord, input: TransitionPrepareInput): void {
    const { expectedRevision: _existingExpectedRevision, ...existingBinding } = existing.binding;
    const { expectedRevision: _requestedExpectedRevision, ...requestedBinding } = input.binding;
    if (
      existing.domain !== input.domain
      || existing.operation !== input.operation
      || digest(existingBinding) !== digest(requestedBinding)
      || existing.nonIdempotentEffect !== input.nonIdempotentEffect
    ) {
      throw new TransitionConflictError(
        "idempotency_binding_mismatch",
        "idempotency key was reused across a different transition binding",
        existing.transitionId,
        existing.binding.entityId,
      );
    }
  }

  private entity(entityId: string): EntityRevisionState {
    return this.entities.get(entityId) ?? {
      entityId,
      revision: 0,
      stateHash: EMPTY_STATE_HASH,
      lastTransitionId: null,
      updatedAt: this.clock(),
    };
  }

  private requiredPending(transitionId: string): TransitionRecord {
    const transition = this.pending.get(transitionId);
    if (!transition) {
      if (this.committed.has(transitionId)) {
        throw new TransitionConflictError("transition_already_committed", "transition is already committed", transitionId);
      }
      if (this.rejected.has(transitionId)) {
        throw new TransitionConflictError("transition_terminal", "transition is already terminal", transitionId);
      }
      throw new TransitionConflictError("transition_unknown", "transition does not exist", transitionId);
    }
    return transition;
  }

  private effectReceipt(
    transition: TransitionRecord,
    input: Omit<EffectReceipt, "transitionId" | "effectId" | "performedAt"> & {
      effectId?: string;
      performedAt?: string;
    },
  ): EffectReceipt {
    const performedAt = input.performedAt ?? this.clock();
    const output = canonicalize(input.output) as JsonObject;
    const effectId = input.effectId ?? deterministicId("e02-effect", {
      transitionId: transition.transitionId,
      requestHash: input.requestHash,
      responseHash: input.responseHash,
      performedAt,
    }, 40);
    return {
      effectId,
      transitionId: transition.transitionId,
      effectKind: input.effectKind,
      requestHash: input.requestHash,
      responseHash: input.responseHash,
      ok: input.ok,
      output,
      error: input.error,
      performedAt,
      providerReceiptId: input.providerReceiptId,
      metadata: cloneJson(input.metadata),
    };
  }

  private recordHash(transition: TransitionRecord): string {
    return digest(transition);
  }

  private refreshHash(transition: TransitionRecord): void {
    this.transitionHashes.set(transition.transitionId, this.recordHash(transition));
  }
}
