import { createHash, randomUUID } from "node:crypto";

export type V = null | boolean | number | string | V[] | { [key: string]: V };
export type O = { [key: string]: V };
export type Phase = "prepare" | "effect" | "commit" | "ack" | "quarantine";
export type TransitionStatus = "pending" | "effect_recorded" | "committed" | "acked" | "quarantined";

export interface TransitionIdentity {
  runId: string;
  sessionId: string;
  restartEpoch: number;
  expectedRevision: number;
  transitionId: string;
  attemptId: string;
  requestId: string;
  toolCallId: string;
  idempotencyKey: string;
}

export interface TransitionCommand {
  owner: "typescript";
  domain: string;
  operation: string;
  identity: TransitionIdentity;
  readSet: string[];
  writeSet: string[];
  payload: O;
  payloadDigest: string;
  requiresEffect: boolean;
}

export interface EffectReceipt {
  effectId: string;
  idempotencyKey: string;
  attemptId: string;
  payloadDigest: string;
  resultDigest: string;
  terminal: boolean;
  ok: boolean;
  reconciled: boolean;
  error: string;
}

export interface TransitionReceipt {
  owner: "typescript";
  domain: string;
  operation: string;
  phase: Phase;
  status: TransitionStatus;
  identity: TransitionIdentity;
  revisionBefore: number;
  revisionAfter: number;
  commandDigest: string;
  payloadDigest: string;
  readSet: string[];
  writeSet: string[];
  stateDigestBefore: string;
  stateDigestAfter: string;
  effect: EffectReceipt | null;
  outboxIds: string[];
  duplicate: boolean;
  error: string;
}

export interface OutboxRecord {
  outboxId: string;
  transitionId: string;
  sequence: number;
  payload: O;
  payloadDigest: string;
  delivered: boolean;
  deliveredAt: string | null;
}

export interface JournalSnapshot {
  version: "zyra.e01-journal/v3";
  owner: "typescript";
  sessionId: string;
  lastRunId: string;
  restartEpoch: number;
  revision: number;
  transitionSequence: number;
  state: O;
  pending: TransitionReceipt[];
  committed: TransitionReceipt[];
  effects: EffectReceipt[];
  outbox: OutboxRecord[];
  stateDigest: string;
  checksum: string;
}

export class InvariantError extends Error {
  readonly code: string;
  readonly details: O;

  constructor(code: string, message: string, details: O = {}) {
    super(code + ": " + message);
    this.name = "InvariantError";
    this.code = code;
    this.details = details;
  }
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return "{" + Object.keys(record).sort().map((key) => {
      return JSON.stringify(key) + ":" + canonicalJson(record[key]);
    }).join(",") + "}";
  }
  return JSON.stringify(value);
}

export function digest(value: unknown): string {
  return "sha256:" + createHash("sha256").update(canonicalJson(value)).digest("hex");
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

function merge(left: O, right: O): O {
  const output = clone(left);
  for (const [key, value] of Object.entries(right)) {
    const previous = output[key];
    if (
      value !== null
      && typeof value === "object"
      && !Array.isArray(value)
      && previous !== null
      && typeof previous === "object"
      && !Array.isArray(previous)
    ) {
      output[key] = merge(previous as O, value as O);
    } else {
      output[key] = clone(value);
    }
  }
  return output;
}

export function identity(
  journal: Journal,
  toolCallId = "",
  existingTransitionId = "",
): TransitionIdentity {
  const nextRevision = journal.revision;
  const transitionSequence = journal.nextTransitionSequence();
  const transitionId = existingTransitionId || [
    journal.sessionId,
    "epoch" + String(journal.restartEpoch),
    "revision" + String(nextRevision),
    "sequence" + String(transitionSequence),
    randomUUID(),
  ].join(":");
  return {
    runId: journal.runId,
    sessionId: journal.sessionId,
    restartEpoch: journal.restartEpoch,
    expectedRevision: nextRevision,
    transitionId,
    attemptId: transitionId + ":attempt:1",
    requestId: "request:" + randomUUID(),
    toolCallId,
    idempotencyKey: digest([journal.sessionId, transitionId]),
  };
}

export function command(
  journal: Journal,
  domain: string,
  operation: string,
  payload: O,
  options: {
    identity?: TransitionIdentity;
    readSet?: string[];
    writeSet?: string[];
    requiresEffect?: boolean;
    toolCallId?: string;
  } = {},
): TransitionCommand {
  const selectedIdentity = options.identity ?? identity(journal, options.toolCallId);
  return {
    owner: "typescript",
    domain,
    operation,
    identity: selectedIdentity,
    readSet: options.readSet ?? [domain],
    writeSet: options.writeSet ?? [domain],
    payload: clone(payload),
    payloadDigest: digest(payload),
    requiresEffect: options.requiresEffect === true,
  };
}

export class Journal {
  readonly sessionId: string;
  runId: string;
  private epochValue = 0;
  private revisionValue = 0;
  private transitionSequenceValue = 0;
  private stateValue: O = {};
  private pendingById = new Map<string, TransitionReceipt>();
  private committedById = new Map<string, TransitionReceipt>();
  private effectsByKey = new Map<string, EffectReceipt>();
  private outboxById = new Map<string, OutboxRecord>();
  private writeLocks = new Map<string, string>();

  constructor(runId: string, sessionId: string) {
    if (!runId || !sessionId) throw new InvariantError("scope_missing", "journal requires run and session");
    this.runId = runId;
    this.sessionId = sessionId;
  }

  get revision(): number {
    return this.revisionValue;
  }

  get restartEpoch(): number {
    return this.epochValue;
  }

  get state(): O {
    return clone(this.stateValue);
  }

  nextTransitionSequence(): number {
    this.transitionSequenceValue += 1;
    return this.transitionSequenceValue;
  }

  prepare(value: TransitionCommand): TransitionReceipt {
    const committed = this.committedById.get(value.identity.transitionId);
    if (committed) {
      this.assertSameCommand(committed, value);
      return { ...clone(committed), duplicate: true };
    }
    const pending = this.pendingById.get(value.identity.transitionId);
    if (pending) {
      this.assertSameCommand(pending, value);
      return { ...clone(pending), duplicate: true };
    }
    this.assertCommand(value);
    for (const path of value.writeSet) {
      const holder = this.writeLocks.get(path);
      if (holder && holder !== value.identity.transitionId) {
        throw new InvariantError("write_conflict", "write set is already reserved", {
          path,
          holder,
        });
      }
    }
    const stateDigest = digest(this.stateValue);
    const receipt: TransitionReceipt = {
      owner: "typescript",
      domain: value.domain,
      operation: value.operation,
      phase: "prepare",
      status: "pending",
      identity: clone(value.identity),
      revisionBefore: this.revisionValue,
      revisionAfter: this.revisionValue,
      commandDigest: digest(value),
      payloadDigest: value.payloadDigest,
      readSet: clone(value.readSet),
      writeSet: clone(value.writeSet),
      stateDigestBefore: stateDigest,
      stateDigestAfter: stateDigest,
      effect: null,
      outboxIds: [],
      duplicate: false,
      error: "",
    };
    this.pendingById.set(value.identity.transitionId, receipt);
    for (const path of value.writeSet) this.writeLocks.set(path, value.identity.transitionId);
    return clone(receipt);
  }

  effect(value: TransitionCommand, result: O, error = ""): EffectReceipt {
    const pending = this.requirePending(value);
    const previous = this.effectsByKey.get(value.identity.idempotencyKey);
    if (previous?.terminal) return clone(previous);
    const receipt: EffectReceipt = {
      effectId: value.identity.transitionId + ":effect",
      idempotencyKey: value.identity.idempotencyKey,
      attemptId: value.identity.attemptId,
      payloadDigest: value.payloadDigest,
      resultDigest: digest(result),
      terminal: true,
      ok: error === "",
      reconciled: false,
      error,
    };
    this.effectsByKey.set(receipt.idempotencyKey, receipt);
    pending.phase = "effect";
    pending.status = "effect_recorded";
    pending.effect = clone(receipt);
    return clone(receipt);
  }

  commit(value: TransitionCommand, patch: O, events: O[]): TransitionReceipt {
    const existing = this.committedById.get(value.identity.transitionId);
    if (existing) {
      this.assertSameCommand(existing, value);
      return { ...clone(existing), duplicate: true };
    }
    const pending = this.requirePending(value);
    if (value.identity.expectedRevision !== this.revisionValue) {
      throw new InvariantError("stale_revision", "commit compare-and-swap failed", {
        expected: value.identity.expectedRevision,
        actual: this.revisionValue,
      });
    }
    if (value.requiresEffect) {
      const effect = this.effectsByKey.get(value.identity.idempotencyKey);
      if (!effect?.terminal) throw new InvariantError("effect_missing", "terminal effect receipt required");
      if (effect.payloadDigest !== value.payloadDigest) {
        throw new InvariantError("effect_payload_mismatch", "effect belongs to another payload");
      }
      if (!effect.ok && !effect.reconciled) {
        throw new InvariantError("effect_failed", "failed effect cannot commit canonical success", {
          effectId: effect.effectId,
          error: effect.error,
        });
      }
    }
    this.stateValue = merge(this.stateValue, patch);
    this.revisionValue += 1;
    pending.phase = "commit";
    pending.status = "committed";
    pending.revisionAfter = this.revisionValue;
    pending.stateDigestAfter = digest(this.stateValue);
    pending.outboxIds = events.map((event, index) => {
      const outboxId = value.identity.transitionId + ":outbox:" + String(index + 1);
      const record: OutboxRecord = {
        outboxId,
        transitionId: value.identity.transitionId,
        sequence: index + 1,
        payload: clone(event),
        payloadDigest: digest(event),
        delivered: false,
        deliveredAt: null,
      };
      this.outboxById.set(outboxId, record);
      return outboxId;
    });
    this.pendingById.delete(value.identity.transitionId);
    this.committedById.set(value.identity.transitionId, pending);
    for (const path of value.writeSet) {
      if (this.writeLocks.get(path) === value.identity.transitionId) this.writeLocks.delete(path);
    }
    return clone(pending);
  }

  ack(transitionId: string): TransitionReceipt {
    const receipt = this.committedById.get(transitionId);
    if (!receipt) throw new InvariantError("commit_missing", "cannot acknowledge an unknown commit");
    receipt.phase = "ack";
    receipt.status = "acked";
    return clone(receipt);
  }

  project(outboxId: string, deliveredAt = new Date().toISOString()): OutboxRecord {
    const record = this.outboxById.get(outboxId);
    if (!record) throw new InvariantError("outbox_missing", "outbox record does not exist");
    if (!record.delivered) {
      record.delivered = true;
      record.deliveredAt = deliveredAt;
    }
    return clone(record);
  }

  quarantine(transitionId: string, error: string): TransitionReceipt {
    const receipt = this.pendingById.get(transitionId);
    if (!receipt) throw new InvariantError("pending_missing", "cannot quarantine unknown transition");
    receipt.phase = "quarantine";
    receipt.status = "quarantined";
    receipt.error = error;
    return clone(receipt);
  }

  pending(): TransitionReceipt[] {
    return [...this.pendingById.values()].map(clone);
  }

  committed(): TransitionReceipt[] {
    return [...this.committedById.values()].map(clone);
  }

  undelivered(): OutboxRecord[] {
    return [...this.outboxById.values()].filter((item) => !item.delivered).map(clone);
  }

  snapshot(): JournalSnapshot {
    const unsigned = {
      version: "zyra.e01-journal/v3" as const,
      owner: "typescript" as const,
      sessionId: this.sessionId,
      lastRunId: this.runId,
      restartEpoch: this.epochValue,
      revision: this.revisionValue,
      transitionSequence: this.transitionSequenceValue,
      state: clone(this.stateValue),
      pending: this.pending(),
      committed: this.committed(),
      effects: [...this.effectsByKey.values()].map(clone),
      outbox: [...this.outboxById.values()].map(clone),
      stateDigest: digest(this.stateValue),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: JournalSnapshot, nextRunId: string): void {
    if (snapshot.version !== "zyra.e01-journal/v3") {
      throw new InvariantError("snapshot_version", "unsupported E01 journal snapshot");
    }
    if (snapshot.owner !== "typescript") {
      throw new InvariantError("stale_owner", "journal snapshot is not TypeScript-owned");
    }
    if (snapshot.sessionId !== this.sessionId) {
      throw new InvariantError("session_mismatch", "journal belongs to another session");
    }
    const unsigned = { ...snapshot } as Partial<JournalSnapshot>;
    delete unsigned.checksum;
    if (digest(unsigned) !== snapshot.checksum) {
      throw new InvariantError("snapshot_checksum", "journal snapshot checksum is invalid");
    }
    if (digest(snapshot.state) !== snapshot.stateDigest) {
      throw new InvariantError("state_digest", "journal state digest is invalid");
    }
    this.runId = nextRunId;
    this.epochValue = snapshot.restartEpoch + 1;
    this.revisionValue = snapshot.revision;
    this.transitionSequenceValue = snapshot.transitionSequence;
    this.stateValue = clone(snapshot.state);
    this.pendingById.clear();
    this.committedById.clear();
    this.effectsByKey.clear();
    this.outboxById.clear();
    this.writeLocks.clear();
    for (const item of snapshot.pending) {
      this.pendingById.set(item.identity.transitionId, clone(item));
      for (const path of item.writeSet) {
        const holder = this.writeLocks.get(path);
        if (holder && holder !== item.identity.transitionId) {
          throw new InvariantError("snapshot_write_conflict", "pending snapshot write sets overlap", {
            path,
            holder,
            contender: item.identity.transitionId,
          });
        }
        this.writeLocks.set(path, item.identity.transitionId);
      }
    }
    for (const item of snapshot.committed) this.committedById.set(item.identity.transitionId, clone(item));
    for (const item of snapshot.effects) this.effectsByKey.set(item.idempotencyKey, clone(item));
    for (const item of snapshot.outbox) this.outboxById.set(item.outboxId, clone(item));
  }

  private assertCommand(value: TransitionCommand): void {
    if (value.owner !== "typescript") throw new InvariantError("stale_owner", "command owner must be TypeScript");
    if (value.identity.sessionId !== this.sessionId) throw new InvariantError("session_mismatch", "command session mismatch");
    if (value.identity.runId !== this.runId) throw new InvariantError("run_mismatch", "command run mismatch");
    if (value.identity.restartEpoch !== this.epochValue) throw new InvariantError("epoch_mismatch", "command epoch mismatch");
    if (value.identity.expectedRevision !== this.revisionValue) throw new InvariantError("stale_revision", "command revision mismatch");
    if (!value.identity.transitionId || !value.identity.idempotencyKey) throw new InvariantError("identity_missing", "stable identity required");
    if (value.payloadDigest !== digest(value.payload)) throw new InvariantError("payload_digest", "payload digest mismatch");
    if (!value.domain || !value.operation) throw new InvariantError("operation_missing", "domain and operation required");
    if (new Set(value.writeSet).size !== value.writeSet.length) throw new InvariantError("duplicate_write", "write set must be unique");
    if ((value.payload.owner as V) === "python" || (value.payload.logical_owner as V) === "python") {
      throw new InvariantError("stale_owner", "Python cannot own E01 logical state");
    }
  }

  private assertSameCommand(receipt: TransitionReceipt, value: TransitionCommand): void {
    if (receipt.commandDigest !== digest(value) || receipt.payloadDigest !== value.payloadDigest) {
      throw new InvariantError("idempotency_conflict", "transition ID was reused for another command");
    }
  }

  private requirePending(value: TransitionCommand): TransitionReceipt {
    const receipt = this.pendingById.get(value.identity.transitionId);
    if (!receipt) throw new InvariantError("prepare_missing", "prepare must precede effect and commit");
    this.assertSameCommand(receipt, value);
    return receipt;
  }
}
