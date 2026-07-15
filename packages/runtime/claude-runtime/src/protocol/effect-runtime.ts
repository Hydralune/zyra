import { createHash, randomUUID } from "node:crypto";

import { asBoolean, asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";

export const EFFECT_PROTOCOL_SNAPSHOT_VERSION = "zyra.effect-protocol/v2";
export const EFFECT_ENVELOPE_VERSION = "zyra.effect-envelope/v1";

export type EffectEnvelopeKind = "command" | "event" | "request" | "response" | "ack" | "nack";
export type EffectDeliveryState = "pending" | "leased" | "acknowledged" | "retry_wait" | "dead_letter" | "cancelled";
export type EffectCommitState = "prepared" | "applied" | "committed" | "rolled_back" | "quarantined";
export type StateOperation = "set" | "merge" | "append" | "remove" | "transition" | "none";

export interface EffectIdentity {
  sessionId: string;
  runId: string;
  taskId: string;
  workerId: string;
  restartEpoch: number;
}

export interface EffectEnvelope {
  version: typeof EFFECT_ENVELOPE_VERSION;
  envelopeId: string;
  kind: EffectEnvelopeKind;
  topic: string;
  aggregateId: string;
  expectedSequence: number | null;
  producerSequence: number;
  idempotencyKey: string;
  correlationId: string;
  causationId: string | null;
  identity: EffectIdentity;
  payload: JsonValue;
  payloadDigest: string;
  createdAt: string;
  expiresAt: string | null;
  checksum: string;
}

export interface StateMutation {
  mutationId: string;
  aggregateId: string;
  domain: string;
  operation: StateOperation;
  path: string[];
  value: JsonValue;
  beforeDigest: string;
  afterDigest: string;
  effective: boolean;
  envelopeId: string;
  revision: number;
  committedAt: string;
}

export interface EffectDelivery {
  deliveryId: string;
  envelopeId: string;
  state: EffectDeliveryState;
  attempt: number;
  owner: string | null;
  leasedAt: string | null;
  leaseExpiresAt: string | null;
  nextAttemptAt: string;
  acknowledgedAt: string | null;
  errorCode: string | null;
  errorMessage: string | null;
  revision: number;
}

export interface EffectTransaction {
  transactionId: string;
  envelopeId: string;
  aggregateId: string;
  expectedSequence: number | null;
  observedSequence: number;
  commitSequence: number | null;
  state: EffectCommitState;
  beforeState: JsonObject;
  afterState: JsonObject | null;
  mutations: StateMutation[];
  error: string | null;
  preparedAt: string;
  committedAt: string | null;
  revision: number;
}

export interface AggregateRecord {
  aggregateId: string;
  sequence: number;
  revision: number;
  state: JsonObject;
  stateDigest: string;
  lastEnvelopeId: string | null;
  updatedAt: string;
}

export interface EffectProtocolSnapshot {
  version: typeof EFFECT_PROTOCOL_SNAPSHOT_VERSION;
  identity: EffectIdentity;
  revision: number;
  producerSequence: number;
  restartEpoch: number;
  envelopes: EffectEnvelope[];
  deliveries: EffectDelivery[];
  transactions: EffectTransaction[];
  aggregates: AggregateRecord[];
  committedIdempotency: Record<string, string>;
  checksum: string;
}

export class EffectProtocolRuntime {
  private readonly identity: EffectIdentity;
  private readonly envelopes = new Map<string, EffectEnvelope>();
  private readonly deliveries = new Map<string, EffectDelivery>();
  private readonly transactions = new Map<string, EffectTransaction>();
  private readonly aggregates = new Map<string, AggregateRecord>();
  private readonly committedIdempotency = new Map<string, string>();
  private revision = 0;
  private producerSequence = 0;
  private restartEpoch = 0;

  constructor(identity: EffectIdentity) {
    this.identity = normalizeIdentity(identity);
    this.restartEpoch = identity.restartEpoch;
  }

  envelope_module(value: JsonObject): JsonObject {
    const envelope = this.createEnvelope({
      kind: envelopeKind(asString(value.kind, "event")),
      topic: asString(value.topic),
      aggregateId: asString(value.aggregate_id),
      expectedSequence: value.expected_sequence === null || value.expected_sequence === undefined ? null : integer(value.expected_sequence, 0),
      idempotencyKey: asString(value.idempotency_key, randomUUID()),
      correlationId: asString(value.correlation_id, randomUUID()),
      causationId: asString(value.causation_id) || null,
      payload: value.payload ?? null,
      expiresAt: asString(value.expires_at) || null,
    });
    return envelopeToJson(envelope);
  }

  transaction_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "prepare") return transactionToJson(this.prepare(asString(value.envelope_id)));
    if (action === "mutate") return mutationToJson(this.applyMutation(asString(value.transaction_id), mutationInputFromJson(value)));
    if (action === "commit") return transactionToJson(this.commit(asString(value.transaction_id)));
    if (action === "rollback") return transactionToJson(this.rollback(asString(value.transaction_id), asString(value.reason)));
    return transactionToJson(this.requireTransaction(asString(value.transaction_id)));
  }

  delivery_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "lease") return deliveryToJson(this.lease(asString(value.delivery_id), asString(value.owner), positive(value.ttl_ms, 30_000)));
    if (action === "ack") return deliveryToJson(this.ack(asString(value.delivery_id), asString(value.owner)));
    if (action === "nack") return deliveryToJson(this.nack(asString(value.delivery_id), asString(value.owner), asString(value.error_code, "delivery_failed"), asString(value.error_message), positive(value.delay_ms, 1_000)));
    return deliveryToJson(this.requireDelivery(asString(value.delivery_id)));
  }

  createEnvelope(input: {
    kind: EffectEnvelopeKind;
    topic: string;
    aggregateId: string;
    expectedSequence: number | null;
    idempotencyKey: string;
    correlationId: string;
    causationId: string | null;
    payload: JsonValue;
    expiresAt: string | null;
  }): EffectEnvelope {
    const committedId = this.committedIdempotency.get(input.idempotencyKey);
    if (committedId) return structuredClone(this.requireEnvelope(committedId));
    for (const envelope of this.envelopes.values()) {
      if (envelope.idempotencyKey !== input.idempotencyKey) continue;
      if (envelope.payloadDigest !== digest(input.payload)) throw new Error("effect envelope idempotency payload conflict");
      return structuredClone(envelope);
    }
    this.producerSequence += 1;
    const unsigned: Omit<EffectEnvelope, "checksum"> = {
      version: EFFECT_ENVELOPE_VERSION,
      envelopeId: randomUUID(),
      kind: input.kind,
      topic: required(input.topic, "effect topic"),
      aggregateId: required(input.aggregateId, "aggregate id"),
      expectedSequence: input.expectedSequence,
      producerSequence: this.producerSequence,
      idempotencyKey: required(input.idempotencyKey, "effect idempotency key"),
      correlationId: required(input.correlationId, "effect correlation id"),
      causationId: input.causationId?.trim() || null,
      identity: { ...this.identity, restartEpoch: this.restartEpoch },
      payload: structuredClone(input.payload),
      payloadDigest: digest(input.payload),
      createdAt: new Date().toISOString(),
      expiresAt: input.expiresAt ? normalizeTimestamp(input.expiresAt) : null,
    };
    const envelope: EffectEnvelope = { ...unsigned, checksum: digest(unsigned) };
    const delivery: EffectDelivery = {
      deliveryId: randomUUID(),
      envelopeId: envelope.envelopeId,
      state: "pending",
      attempt: 0,
      owner: null,
      leasedAt: null,
      leaseExpiresAt: null,
      nextAttemptAt: envelope.createdAt,
      acknowledgedAt: null,
      errorCode: null,
      errorMessage: null,
      revision: 1,
    };
    this.envelopes.set(envelope.envelopeId, envelope);
    this.deliveries.set(delivery.deliveryId, delivery);
    this.revision += 1;
    return structuredClone(envelope);
  }

  prepare(envelopeId: string): EffectTransaction {
    const envelope = this.requireEnvelope(envelopeId);
    const existing = [...this.transactions.values()].find((item) => item.envelopeId === envelopeId);
    if (existing) return structuredClone(existing);
    if (envelope.expiresAt && Date.parse(envelope.expiresAt) <= Date.now()) throw new Error("effect envelope expired");
    const aggregate = this.aggregate(envelope.aggregateId);
    if (envelope.expectedSequence !== null && aggregate.sequence !== envelope.expectedSequence) {
      throw new Error(`aggregate sequence conflict: expected ${envelope.expectedSequence}, actual ${aggregate.sequence}`);
    }
    const transaction: EffectTransaction = {
      transactionId: randomUUID(),
      envelopeId,
      aggregateId: envelope.aggregateId,
      expectedSequence: envelope.expectedSequence,
      observedSequence: aggregate.sequence,
      commitSequence: null,
      state: "prepared",
      beforeState: structuredClone(aggregate.state),
      afterState: structuredClone(aggregate.state),
      mutations: [],
      error: null,
      preparedAt: new Date().toISOString(),
      committedAt: null,
      revision: 1,
    };
    this.transactions.set(transaction.transactionId, transaction);
    this.revision += 1;
    return structuredClone(transaction);
  }

  applyMutation(transactionId: string, input: {
    domain: string;
    operation: StateOperation;
    path: readonly string[];
    value: JsonValue;
    effective: boolean;
  }): StateMutation {
    const transaction = this.requireTransaction(transactionId);
    if (transaction.state !== "prepared" && transaction.state !== "applied") throw new Error(`transaction cannot mutate from ${transaction.state}`);
    const before = structuredClone(transaction.afterState ?? transaction.beforeState);
    const after = applyStateOperation(before, input.operation, input.path, input.value);
    const mutation: StateMutation = {
      mutationId: randomUUID(),
      aggregateId: transaction.aggregateId,
      domain: required(input.domain, "mutation domain"),
      operation: input.operation,
      path: input.path.map((segment) => validPathSegment(segment)),
      value: structuredClone(input.value),
      beforeDigest: digest(before),
      afterDigest: digest(after),
      effective: input.effective && digest(before) !== digest(after),
      envelopeId: transaction.envelopeId,
      revision: transaction.mutations.length + 1,
      committedAt: new Date().toISOString(),
    };
    transaction.afterState = after;
    transaction.mutations.push(mutation);
    transaction.state = "applied";
    transaction.revision += 1;
    this.revision += 1;
    return structuredClone(mutation);
  }

  commit(transactionId: string): EffectTransaction {
    const transaction = this.requireTransaction(transactionId);
    if (transaction.state === "committed") return structuredClone(transaction);
    if (transaction.state !== "applied" && transaction.state !== "prepared") throw new Error(`transaction cannot commit from ${transaction.state}`);
    const aggregate = this.aggregate(transaction.aggregateId);
    if (aggregate.sequence !== transaction.observedSequence) {
      transaction.state = "quarantined";
      transaction.error = `aggregate changed during transaction: observed ${transaction.observedSequence}, actual ${aggregate.sequence}`;
      transaction.revision += 1;
      this.revision += 1;
      throw new Error(transaction.error);
    }
    const envelope = this.requireEnvelope(transaction.envelopeId);
    aggregate.sequence += 1;
    aggregate.revision += 1;
    aggregate.state = structuredClone(transaction.afterState ?? transaction.beforeState);
    aggregate.stateDigest = digest(aggregate.state);
    aggregate.lastEnvelopeId = envelope.envelopeId;
    aggregate.updatedAt = new Date().toISOString();
    transaction.state = "committed";
    transaction.commitSequence = aggregate.sequence;
    transaction.committedAt = aggregate.updatedAt;
    transaction.revision += 1;
    this.committedIdempotency.set(envelope.idempotencyKey, envelope.envelopeId);
    this.revision += 1;
    return structuredClone(transaction);
  }

  rollback(transactionId: string, reason: string): EffectTransaction {
    const transaction = this.requireTransaction(transactionId);
    if (transaction.state === "committed") throw new Error("committed transaction cannot roll back");
    if (transaction.state === "rolled_back") return structuredClone(transaction);
    transaction.state = "rolled_back";
    transaction.afterState = structuredClone(transaction.beforeState);
    transaction.error = reason.trim().slice(0, 8_192);
    transaction.revision += 1;
    this.revision += 1;
    return structuredClone(transaction);
  }

  pendingDeliveries(maximum = 100): EffectDelivery[] {
    const now = Date.now();
    return [...this.deliveries.values()]
      .filter((delivery) => {
        if (delivery.state === "acknowledged" || delivery.state === "dead_letter" || delivery.state === "cancelled") return false;
        if (delivery.state === "leased" && delivery.leaseExpiresAt && Date.parse(delivery.leaseExpiresAt) > now) return false;
        return Date.parse(delivery.nextAttemptAt) <= now;
      })
      .sort((left, right) => Date.parse(this.requireEnvelope(left.envelopeId).createdAt) - Date.parse(this.requireEnvelope(right.envelopeId).createdAt))
      .slice(0, Math.max(0, Math.floor(maximum)))
      .map((item) => structuredClone(item));
  }

  lease(deliveryId: string, owner: string, ttlMs = 30_000): EffectDelivery {
    const delivery = this.requireDelivery(deliveryId);
    if (delivery.state === "acknowledged") return structuredClone(delivery);
    const now = Date.now();
    if (delivery.state === "leased" && delivery.leaseExpiresAt && Date.parse(delivery.leaseExpiresAt) > now && delivery.owner !== owner) {
      throw new Error(`delivery lease held by ${delivery.owner}`);
    }
    if (delivery.state === "dead_letter" || delivery.state === "cancelled") throw new Error(`delivery cannot lease from ${delivery.state}`);
    delivery.state = "leased";
    delivery.attempt += 1;
    delivery.owner = required(owner, "delivery owner");
    delivery.leasedAt = new Date(now).toISOString();
    delivery.leaseExpiresAt = new Date(now + positive(ttlMs, 30_000)).toISOString();
    delivery.errorCode = null;
    delivery.errorMessage = null;
    delivery.revision += 1;
    this.revision += 1;
    return structuredClone(delivery);
  }

  ack(deliveryId: string, owner: string): EffectDelivery {
    const delivery = this.requireDelivery(deliveryId);
    if (delivery.state === "acknowledged") return structuredClone(delivery);
    this.assertLease(delivery, owner);
    const transaction = [...this.transactions.values()].find((item) => item.envelopeId === delivery.envelopeId);
    if (!transaction || transaction.state !== "committed") throw new Error("delivery cannot ACK before transaction commit");
    delivery.state = "acknowledged";
    delivery.acknowledgedAt = new Date().toISOString();
    delivery.owner = null;
    delivery.leaseExpiresAt = null;
    delivery.revision += 1;
    this.revision += 1;
    return structuredClone(delivery);
  }

  nack(deliveryId: string, owner: string, code: string, message: string, delayMs: number): EffectDelivery {
    const delivery = this.requireDelivery(deliveryId);
    this.assertLease(delivery, owner);
    delivery.state = delivery.attempt >= 10 ? "dead_letter" : "retry_wait";
    delivery.owner = null;
    delivery.leaseExpiresAt = null;
    delivery.nextAttemptAt = new Date(Date.now() + positive(delayMs, 1_000)).toISOString();
    delivery.errorCode = required(code, "delivery error code");
    delivery.errorMessage = message.trim().slice(0, 8_192);
    delivery.revision += 1;
    this.revision += 1;
    return structuredClone(delivery);
  }

  projectAggregate(aggregateId: string): AggregateRecord {
    return structuredClone(this.aggregate(aggregateId));
  }

  snapshot(): EffectProtocolSnapshot {
    if ([...this.transactions.values()].some((item) => item.state === "applied")) throw new Error("cannot snapshot with applied but uncommitted transaction");
    const unsigned: Omit<EffectProtocolSnapshot, "checksum"> = {
      version: EFFECT_PROTOCOL_SNAPSHOT_VERSION,
      identity: { ...this.identity, restartEpoch: this.restartEpoch },
      revision: this.revision,
      producerSequence: this.producerSequence,
      restartEpoch: this.restartEpoch,
      envelopes: structuredClone([...this.envelopes.values()]),
      deliveries: structuredClone([...this.deliveries.values()]),
      transactions: structuredClone([...this.transactions.values()]),
      aggregates: structuredClone([...this.aggregates.values()]),
      committedIdempotency: Object.fromEntries(this.committedIdempotency),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(
    snapshot: EffectProtocolSnapshot,
    options: { allowRunRebind?: boolean } = {},
  ): void {
    if (snapshot.version !== EFFECT_PROTOCOL_SNAPSHOT_VERSION) throw new Error("unsupported effect protocol snapshot version");
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("effect protocol snapshot checksum mismatch");
    const runChanged = snapshot.identity.runId !== this.identity.runId;
    if (
      snapshot.identity.sessionId !== this.identity.sessionId ||
      snapshot.identity.taskId !== this.identity.taskId ||
      snapshot.identity.workerId !== this.identity.workerId ||
      (runChanged && options.allowRunRebind !== true)
    ) {
      throw new Error("effect protocol snapshot identity mismatch");
    }
    validateSnapshot(snapshot);
    this.envelopes.clear();
    for (const envelope of snapshot.envelopes) this.envelopes.set(envelope.envelopeId, structuredClone(envelope));
    this.deliveries.clear();
    for (const delivery of snapshot.deliveries) this.deliveries.set(delivery.deliveryId, structuredClone(delivery));
    this.transactions.clear();
    for (const transaction of snapshot.transactions) this.transactions.set(transaction.transactionId, structuredClone(transaction));
    this.aggregates.clear();
    for (const aggregate of snapshot.aggregates) this.aggregates.set(aggregate.aggregateId, structuredClone(aggregate));
    this.committedIdempotency.clear();
    for (const [key, value] of Object.entries(snapshot.committedIdempotency)) this.committedIdempotency.set(key, value);
    this.revision = snapshot.revision;
    this.producerSequence = snapshot.producerSequence;
    this.restartEpoch = snapshot.restartEpoch + 1;
    this.recoverLeases();
  }

  private aggregate(aggregateId: string): AggregateRecord {
    const id = required(aggregateId, "aggregate id");
    let aggregate = this.aggregates.get(id);
    if (!aggregate) {
      aggregate = {
        aggregateId: id,
        sequence: 0,
        revision: 0,
        state: {},
        stateDigest: digest({}),
        lastEnvelopeId: null,
        updatedAt: new Date().toISOString(),
      };
      this.aggregates.set(id, aggregate);
    }
    return aggregate;
  }

  private recoverLeases(): void {
    const now = new Date().toISOString();
    for (const delivery of this.deliveries.values()) {
      if (delivery.state !== "leased") continue;
      delivery.state = "pending";
      delivery.owner = null;
      delivery.leaseExpiresAt = null;
      delivery.nextAttemptAt = now;
      delivery.errorCode = "lease_lost_on_restart";
      delivery.errorMessage = "delivery returned to pending after runtime restart";
      delivery.revision += 1;
    }
  }

  private assertLease(delivery: EffectDelivery, owner: string): void {
    if (delivery.state !== "leased") throw new Error(`delivery is not leased: ${delivery.state}`);
    if (delivery.owner !== owner) throw new Error("delivery owner mismatch");
    if (!delivery.leaseExpiresAt || Date.parse(delivery.leaseExpiresAt) <= Date.now()) throw new Error("delivery lease expired");
  }

  private requireEnvelope(envelopeId: string): EffectEnvelope {
    const envelope = this.envelopes.get(envelopeId);
    if (!envelope) throw new Error(`effect envelope not found: ${envelopeId}`);
    return envelope;
  }

  private requireDelivery(deliveryId: string): EffectDelivery {
    const delivery = this.deliveries.get(deliveryId);
    if (!delivery) throw new Error(`effect delivery not found: ${deliveryId}`);
    return delivery;
  }

  private requireTransaction(transactionId: string): EffectTransaction {
    const transaction = this.transactions.get(transactionId);
    if (!transaction) throw new Error(`effect transaction not found: ${transactionId}`);
    return transaction;
  }
}

function applyStateOperation(state: JsonObject, operation: StateOperation, path: readonly string[], value: JsonValue): JsonObject {
  if (operation === "none") return structuredClone(state);
  if (path.length === 0) {
    if (operation === "remove") return {};
    if (operation === "merge") return { ...state, ...asObject(value) };
    if (operation === "set" || operation === "transition") return asObject(value);
    if (operation === "append") throw new Error("root append requires an array state field");
  }
  const result = structuredClone(state);
  let cursor: JsonObject = result;
  for (let index = 0; index < path.length - 1; index += 1) {
    const segment = validPathSegment(path[index]);
    const next = asObject(cursor[segment]);
    cursor[segment] = next;
    cursor = next;
  }
  const key = validPathSegment(path.at(-1)!);
  if (operation === "remove") delete cursor[key];
  else if (operation === "merge") cursor[key] = { ...asObject(cursor[key]), ...asObject(value) };
  else if (operation === "append") cursor[key] = [...(Array.isArray(cursor[key]) ? cursor[key] : []), structuredClone(value)];
  else cursor[key] = structuredClone(value);
  return result;
}

function validateSnapshot(snapshot: EffectProtocolSnapshot): void {
  const envelopes = new Map(snapshot.envelopes.map((item) => [item.envelopeId, item]));
  const aggregates = new Map(snapshot.aggregates.map((item) => [item.aggregateId, item]));
  const latestCommitted = new Map<string, EffectTransaction>();
  for (const envelope of snapshot.envelopes) {
    const { checksum, ...unsigned } = envelope;
    if (digest(unsigned) !== checksum) throw new Error(`effect envelope checksum mismatch: ${envelope.envelopeId}`);
  }
  for (const delivery of snapshot.deliveries) if (!envelopes.has(delivery.envelopeId)) throw new Error(`delivery references missing envelope: ${delivery.envelopeId}`);
  for (const transaction of snapshot.transactions) {
    if (!envelopes.has(transaction.envelopeId)) throw new Error(`transaction references missing envelope: ${transaction.envelopeId}`);
    if (transaction.state !== "committed") continue;
    if (!transaction.afterState || transaction.commitSequence === null) throw new Error(`committed transaction is incomplete: ${transaction.transactionId}`);
    const aggregate = aggregates.get(transaction.aggregateId);
    if (!aggregate || transaction.commitSequence > aggregate.sequence) throw new Error(`committed transaction sequence mismatch: ${transaction.transactionId}`);
    const previous = latestCommitted.get(transaction.aggregateId);
    if (!previous || (previous.commitSequence ?? -1) < transaction.commitSequence) latestCommitted.set(transaction.aggregateId, transaction);
  }
  for (const [aggregateId, transaction] of latestCommitted) {
    const aggregate = aggregates.get(aggregateId)!;
    if (
      aggregate.sequence !== transaction.commitSequence
      || aggregate.lastEnvelopeId !== transaction.envelopeId
      || aggregate.stateDigest !== digest(transaction.afterState)
    ) {
      throw new Error(`latest committed transaction projection mismatch: ${transaction.transactionId}`);
    }
  }
}

function normalizeIdentity(value: EffectIdentity): EffectIdentity {
  return {
    sessionId: required(value.sessionId, "session id"),
    runId: required(value.runId, "run id"),
    taskId: required(value.taskId, "task id"),
    workerId: required(value.workerId, "worker id"),
    restartEpoch: Math.max(0, Math.floor(value.restartEpoch)),
  };
}

function mutationInputFromJson(value: JsonObject): Parameters<EffectProtocolRuntime["applyMutation"]>[1] {
  return {
    domain: asString(value.domain, "runtime"),
    operation: stateOperation(asString(value.operation, "none")),
    path: Array.isArray(value.path) ? value.path.map(String) : [],
    value: value.value ?? null,
    effective: asBoolean(value.effective, true),
  };
}

function envelopeToJson(value: EffectEnvelope): JsonObject {
  return {
    version: value.version,
    envelope_id: value.envelopeId,
    kind: value.kind,
    topic: value.topic,
    aggregate_id: value.aggregateId,
    expected_sequence: value.expectedSequence,
    producer_sequence: value.producerSequence,
    idempotency_key: value.idempotencyKey,
    correlation_id: value.correlationId,
    causation_id: value.causationId,
    identity: identityToJson(value.identity),
    payload: value.payload,
    payload_digest: value.payloadDigest,
    created_at: value.createdAt,
    expires_at: value.expiresAt,
    checksum: value.checksum,
  };
}

function deliveryToJson(value: EffectDelivery): JsonObject {
  return {
    delivery_id: value.deliveryId,
    envelope_id: value.envelopeId,
    state: value.state,
    attempt: value.attempt,
    owner: value.owner,
    leased_at: value.leasedAt,
    lease_expires_at: value.leaseExpiresAt,
    next_attempt_at: value.nextAttemptAt,
    acknowledged_at: value.acknowledgedAt,
    error_code: value.errorCode,
    error_message: value.errorMessage,
    revision: value.revision,
  };
}

function transactionToJson(value: EffectTransaction): JsonObject {
  return {
    transaction_id: value.transactionId,
    envelope_id: value.envelopeId,
    aggregate_id: value.aggregateId,
    expected_sequence: value.expectedSequence,
    observed_sequence: value.observedSequence,
    commit_sequence: value.commitSequence,
    state: value.state,
    before_state: value.beforeState,
    after_state: value.afterState,
    mutations: value.mutations.map(mutationToJson),
    error: value.error,
    prepared_at: value.preparedAt,
    committed_at: value.committedAt,
    revision: value.revision,
  };
}

function mutationToJson(value: StateMutation): JsonObject {
  return {
    mutation_id: value.mutationId,
    aggregate_id: value.aggregateId,
    domain: value.domain,
    operation: value.operation,
    path: value.path,
    value: value.value,
    before_digest: value.beforeDigest,
    after_digest: value.afterDigest,
    effective: value.effective,
    envelope_id: value.envelopeId,
    revision: value.revision,
    committed_at: value.committedAt,
  };
}

function identityToJson(value: EffectIdentity): JsonObject {
  return { session_id: value.sessionId, run_id: value.runId, task_id: value.taskId, worker_id: value.workerId, restart_epoch: value.restartEpoch };
}

function envelopeKind(value: string): EffectEnvelopeKind {
  if (value === "command" || value === "request" || value === "response" || value === "ack" || value === "nack") return value;
  return "event";
}

function stateOperation(value: string): StateOperation {
  if (value === "set" || value === "merge" || value === "append" || value === "remove" || value === "transition") return value;
  return "none";
}

function validPathSegment(value: string): string {
  const segment = value.trim();
  if (!segment || segment === "__proto__" || segment === "prototype" || segment === "constructor") throw new Error(`invalid state path segment: ${value}`);
  return segment;
}

function required(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${name} is required`);
  return normalized;
}

function positive(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function integer(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : fallback;
}

function normalizeTimestamp(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error(`invalid effect timestamp: ${value}`);
  return new Date(timestamp).toISOString();
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function digest(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}
