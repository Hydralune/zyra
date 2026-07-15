import { describe, expect, test } from "bun:test";

import {
  EFFECT_ENVELOPE_VERSION,
  EFFECT_PROTOCOL_SNAPSHOT_VERSION,
  EffectProtocolRuntime,
  type EffectEnvelopeKind,
  type EffectIdentity,
  type EffectProtocolSnapshot,
} from "../../src/protocol/effect-runtime.js";
import { digest } from "../../src/e01/kernel.js";

function effectIdentity(overrides: Partial<EffectIdentity> = {}): EffectIdentity {
  return {
    sessionId: "session-1",
    runId: "run-1",
    taskId: "task-1",
    workerId: "worker-1",
    restartEpoch: 0,
    ...overrides,
  };
}

function protocol(overrides: Partial<EffectIdentity> = {}): EffectProtocolRuntime {
  return new EffectProtocolRuntime(effectIdentity(overrides));
}

function envelope(
  runtime: EffectProtocolRuntime,
  idempotencyKey: string,
  options: {
    kind?: EffectEnvelopeKind;
    topic?: string;
    aggregateId?: string;
    expectedSequence?: number | null;
    correlationId?: string;
    causationId?: string | null;
    payload?: null | boolean | number | string | unknown[] | Record<string, unknown>;
    expiresAt?: string | null;
  } = {},
) {
  return runtime.createEnvelope({
    kind: options.kind ?? "event",
    topic: options.topic ?? "runtime.query.transition",
    aggregateId: options.aggregateId ?? "aggregate-1",
    expectedSequence: options.expectedSequence ?? null,
    idempotencyKey,
    correlationId: options.correlationId ?? `correlation:${idempotencyKey}`,
    causationId: options.causationId ?? null,
    payload: (options.payload ?? { idempotencyKey }) as never,
    expiresAt: options.expiresAt ?? null,
  });
}

function deliveryFor(runtime: EffectProtocolRuntime, envelopeId: string) {
  const delivery = runtime
    .snapshot()
    .deliveries.find((candidate) => candidate.envelopeId === envelopeId);
  if (delivery === undefined) throw new Error(`missing delivery for ${envelopeId}`);
  return delivery;
}

function resignSnapshot(snapshot: EffectProtocolSnapshot): EffectProtocolSnapshot {
  const unsigned = { ...snapshot } as Partial<EffectProtocolSnapshot>;
  delete unsigned.checksum;
  snapshot.checksum = digest(unsigned);
  return snapshot;
}

describe("effect envelope custody", () => {
  test("creates a checksummed envelope with producer identity", () => {
    const runtime = protocol();
    const value = envelope(runtime, "envelope:create", {
      kind: "command",
      topic: "runtime.session.cancel",
      aggregateId: "session-1",
      expectedSequence: 0,
      correlationId: "correlation-cancel",
      causationId: "command-parent",
      payload: { reason: "user_cancelled" },
    });

    expect(value.version).toBe(EFFECT_ENVELOPE_VERSION);
    expect(value.kind).toBe("command");
    expect(value.producerSequence).toBe(1);
    expect(value.identity).toEqual(effectIdentity());
    expect(value.payloadDigest).toBe(digest({ reason: "user_cancelled" }));
    expect(value.checksum).toMatch(/^sha256:/);
    expect(value.causationId).toBe("command-parent");
  });

  test("allocates one pending delivery per new envelope", () => {
    const runtime = protocol();
    const value = envelope(runtime, "envelope:delivery");
    const delivery = deliveryFor(runtime, value.envelopeId);

    expect(delivery.state).toBe("pending");
    expect(delivery.attempt).toBe(0);
    expect(delivery.owner).toBeNull();
    expect(delivery.acknowledgedAt).toBeNull();
    expect(runtime.pendingDeliveries()).toEqual([delivery]);
  });

  test("increments producer sequence independently from aggregate sequence", () => {
    const runtime = protocol();
    const first = envelope(runtime, "envelope:producer-1", {
      aggregateId: "aggregate-a",
    });
    const second = envelope(runtime, "envelope:producer-2", {
      aggregateId: "aggregate-b",
    });

    expect(first.producerSequence).toBe(1);
    expect(second.producerSequence).toBe(2);
    expect(runtime.projectAggregate("aggregate-a").sequence).toBe(0);
    expect(runtime.projectAggregate("aggregate-b").sequence).toBe(0);
  });

  test("deduplicates envelope creation by idempotency key", () => {
    const runtime = protocol();
    const first = envelope(runtime, "envelope:idem", {
      payload: { value: 1 },
    });
    const repeated = envelope(runtime, "envelope:idem", {
      payload: { value: 1 },
    });

    expect(repeated).toEqual(first);
    expect(runtime.snapshot().envelopes).toHaveLength(1);
    expect(runtime.snapshot().deliveries).toHaveLength(1);
    expect(runtime.snapshot().producerSequence).toBe(1);
  });

  test("rejects idempotency key reuse with different payload", () => {
    const runtime = protocol();
    envelope(runtime, "envelope:conflict", { payload: { value: 1 } });

    expect(() =>
      envelope(runtime, "envelope:conflict", { payload: { value: 2 } }),
    ).toThrow("effect envelope idempotency payload conflict");
    expect(runtime.snapshot().envelopes[0]?.payload).toEqual({ value: 1 });
  });

  test("normalizes optional causation and expiration timestamps", () => {
    const runtime = protocol();
    const value = envelope(runtime, "envelope:normalize", {
      causationId: "  ",
      expiresAt: "2026-07-15T10:00:00+08:00",
    });

    expect(value.causationId).toBeNull();
    expect(value.expiresAt).toBe("2026-07-15T02:00:00.000Z");
    expect(value.createdAt).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });

  test("rejects preparation of an expired envelope", () => {
    const runtime = protocol();
    const value = envelope(runtime, "envelope:expired", {
      expiresAt: "2020-01-01T00:00:00.000Z",
    });

    expect(() => runtime.prepare(value.envelopeId)).toThrow(
      "effect envelope expired",
    );
    expect(runtime.snapshot().transactions).toEqual([]);
  });

  test("routes the envelope module into canonical delivery state", () => {
    const runtime = protocol();
    const projected = runtime.envelope_module({
      kind: "event",
      topic: "runtime.module.event",
      aggregate_id: "aggregate-module",
      expected_sequence: 0,
      idempotency_key: "envelope:module",
      correlation_id: "correlation-module",
      payload: { source: "module" },
    });

    expect(projected.kind).toBe("event");
    expect(projected.topic).toBe("runtime.module.event");
    expect(runtime.snapshot().envelopes).toHaveLength(1);
    expect(runtime.pendingDeliveries()).toHaveLength(1);
  });
});

describe("effect transaction and aggregate CAS", () => {
  test("prepares against the current immutable aggregate snapshot", () => {
    const runtime = protocol();
    const value = envelope(runtime, "transaction:prepare", {
      expectedSequence: 0,
    });
    const transaction = runtime.prepare(value.envelopeId);

    expect(transaction.state).toBe("prepared");
    expect(transaction.expectedSequence).toBe(0);
    expect(transaction.observedSequence).toBe(0);
    expect(transaction.beforeState).toEqual({});
    expect(transaction.afterState).toEqual({});
    expect(transaction.mutations).toEqual([]);
  });

  test("returns the same prepared transaction on retry", () => {
    const runtime = protocol();
    const value = envelope(runtime, "transaction:idem");
    const first = runtime.prepare(value.envelopeId);
    const repeated = runtime.prepare(value.envelopeId);

    expect(repeated).toEqual(first);
    expect(runtime.snapshot().transactions).toHaveLength(1);
  });

  test("rejects an envelope whose expected aggregate sequence is stale", () => {
    const runtime = protocol();
    const first = envelope(runtime, "transaction:sequence-first", {
      expectedSequence: 0,
    });
    const firstTransaction = runtime.prepare(first.envelopeId);
    runtime.commit(firstTransaction.transactionId);
    const stale = envelope(runtime, "transaction:sequence-stale", {
      expectedSequence: 0,
    });

    expect(() => runtime.prepare(stale.envelopeId)).toThrow(
      "aggregate sequence conflict: expected 0, actual 1",
    );
    expect(runtime.projectAggregate("aggregate-1").sequence).toBe(1);
  });

  test("sets a nested aggregate value", () => {
    const runtime = protocol();
    const value = envelope(runtime, "mutation:set");
    const transaction = runtime.prepare(value.envelopeId);
    const mutation = runtime.applyMutation(transaction.transactionId, {
      domain: "query",
      operation: "set",
      path: ["query", "phase"],
      value: "tool",
      effective: true,
    });

    expect(mutation.operation).toBe("set");
    expect(mutation.path).toEqual(["query", "phase"]);
    expect(mutation.effective).toBe(true);
    expect(mutation.beforeDigest).not.toBe(mutation.afterDigest);
    runtime.commit(transaction.transactionId);
    expect(runtime.projectAggregate("aggregate-1").state).toEqual({
      query: { phase: "tool" },
    });
  });

  test("merges object state without deleting siblings", () => {
    const runtime = protocol();
    const first = envelope(runtime, "mutation:merge-seed");
    const seed = runtime.prepare(first.envelopeId);
    runtime.applyMutation(seed.transactionId, {
      domain: "session",
      operation: "set",
      path: [],
      value: { query: { phase: "reason", turn: 1 }, stable: true },
      effective: true,
    });
    runtime.commit(seed.transactionId);
    const next = envelope(runtime, "mutation:merge-next", {
      expectedSequence: 1,
    });
    const transaction = runtime.prepare(next.envelopeId);
    runtime.applyMutation(transaction.transactionId, {
      domain: "session",
      operation: "merge",
      path: ["query"],
      value: { phase: "observe", tools: 1 },
      effective: true,
    });
    runtime.commit(transaction.transactionId);

    expect(runtime.projectAggregate("aggregate-1").state).toEqual({
      query: { phase: "observe", turn: 1, tools: 1 },
      stable: true,
    });
  });

  test("appends ordered values to an aggregate list", () => {
    const runtime = protocol();
    const first = envelope(runtime, "mutation:append-first");
    const firstTransaction = runtime.prepare(first.envelopeId);
    runtime.applyMutation(firstTransaction.transactionId, {
      domain: "events",
      operation: "append",
      path: ["events"],
      value: { sequence: 1 },
      effective: true,
    });
    runtime.commit(firstTransaction.transactionId);
    const second = envelope(runtime, "mutation:append-second", {
      expectedSequence: 1,
    });
    const secondTransaction = runtime.prepare(second.envelopeId);
    runtime.applyMutation(secondTransaction.transactionId, {
      domain: "events",
      operation: "append",
      path: ["events"],
      value: { sequence: 2 },
      effective: true,
    });
    runtime.commit(secondTransaction.transactionId);

    expect(runtime.projectAggregate("aggregate-1").state.events).toEqual([
      { sequence: 1 },
      { sequence: 2 },
    ]);
  });

  test("removes an existing nested aggregate path", () => {
    const runtime = protocol();
    const seedEnvelope = envelope(runtime, "mutation:remove-seed");
    const seed = runtime.prepare(seedEnvelope.envelopeId);
    runtime.applyMutation(seed.transactionId, {
      domain: "context",
      operation: "set",
      path: [],
      value: { keep: true, remove: { secret: "gone" } },
      effective: true,
    });
    runtime.commit(seed.transactionId);
    const removeEnvelope = envelope(runtime, "mutation:remove", {
      expectedSequence: 1,
    });
    const remove = runtime.prepare(removeEnvelope.envelopeId);
    runtime.applyMutation(remove.transactionId, {
      domain: "context",
      operation: "remove",
      path: ["remove"],
      value: null,
      effective: true,
    });
    runtime.commit(remove.transactionId);

    expect(runtime.projectAggregate("aggregate-1").state).toEqual({ keep: true });
  });

  test("marks a requested mutation ineffective when state does not change", () => {
    const runtime = protocol();
    const value = envelope(runtime, "mutation:none");
    const transaction = runtime.prepare(value.envelopeId);
    const mutation = runtime.applyMutation(transaction.transactionId, {
      domain: "query",
      operation: "none",
      path: [],
      value: null,
      effective: true,
    });

    expect(mutation.effective).toBe(false);
    expect(mutation.beforeDigest).toBe(mutation.afterDigest);
    expect(runtime.commit(transaction.transactionId).state).toBe("committed");
  });

  test("honors an explicit non-effective observation", () => {
    const runtime = protocol();
    const value = envelope(runtime, "mutation:observation");
    const transaction = runtime.prepare(value.envelopeId);
    const mutation = runtime.applyMutation(transaction.transactionId, {
      domain: "telemetry",
      operation: "set",
      path: ["observed"],
      value: true,
      effective: false,
    });

    expect(mutation.beforeDigest).not.toBe(mutation.afterDigest);
    expect(mutation.effective).toBe(false);
    runtime.commit(transaction.transactionId);
    expect(runtime.projectAggregate("aggregate-1").state).toEqual({ observed: true });
  });

  test("rejects unsafe prototype state paths", () => {
    const runtime = protocol();
    const value = envelope(runtime, "mutation:unsafe");
    const transaction = runtime.prepare(value.envelopeId);

    expect(() =>
      runtime.applyMutation(transaction.transactionId, {
        domain: "session",
        operation: "set",
        path: ["__proto__", "polluted"],
        value: true,
        effective: true,
      }),
    ).toThrow("invalid state path segment");
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
  });

  test("commits one aggregate sequence with the after state", () => {
    const runtime = protocol();
    const value = envelope(runtime, "transaction:commit", {
      expectedSequence: 0,
    });
    const transaction = runtime.prepare(value.envelopeId);
    runtime.applyMutation(transaction.transactionId, {
      domain: "query",
      operation: "set",
      path: ["phase"],
      value: "complete",
      effective: true,
    });
    const committed = runtime.commit(transaction.transactionId);
    const aggregate = runtime.projectAggregate("aggregate-1");

    expect(committed.state).toBe("committed");
    expect(committed.commitSequence).toBe(1);
    expect(aggregate.sequence).toBe(1);
    expect(aggregate.state).toEqual({ phase: "complete" });
    expect(aggregate.stateDigest).toBe(digest({ phase: "complete" }));
    expect(aggregate.lastEnvelopeId).toBe(value.envelopeId);
  });

  test("deduplicates transaction commit and future envelope creation", () => {
    const runtime = protocol();
    const value = envelope(runtime, "transaction:commit-idem", {
      payload: { phase: "complete" },
    });
    const transaction = runtime.prepare(value.envelopeId);
    const first = runtime.commit(transaction.transactionId);
    const repeated = runtime.commit(transaction.transactionId);
    const envelopeReplay = envelope(runtime, "transaction:commit-idem", {
      payload: { phase: "complete" },
    });

    expect(repeated).toEqual(first);
    expect(envelopeReplay.envelopeId).toBe(value.envelopeId);
    expect(runtime.projectAggregate("aggregate-1").sequence).toBe(1);
    expect(runtime.snapshot().committedIdempotency["transaction:commit-idem"]).toBe(
      value.envelopeId,
    );
  });

  test("quarantines the loser of concurrent aggregate transactions", () => {
    const runtime = protocol();
    const firstEnvelope = envelope(runtime, "transaction:concurrent-first");
    const secondEnvelope = envelope(runtime, "transaction:concurrent-second");
    const first = runtime.prepare(firstEnvelope.envelopeId);
    const second = runtime.prepare(secondEnvelope.envelopeId);
    runtime.applyMutation(first.transactionId, {
      domain: "query",
      operation: "set",
      path: ["winner"],
      value: "first",
      effective: true,
    });
    runtime.applyMutation(second.transactionId, {
      domain: "query",
      operation: "set",
      path: ["winner"],
      value: "second",
      effective: true,
    });
    runtime.commit(first.transactionId);

    expect(() => runtime.commit(second.transactionId)).toThrow(
      "aggregate changed during transaction",
    );
    const quarantined = runtime
      .snapshot()
      .transactions.find((item) => item.transactionId === second.transactionId);
    expect(quarantined?.state).toBe("quarantined");
    expect(runtime.projectAggregate("aggregate-1").state).toEqual({ winner: "first" });
  });

  test("rolls back prepared state without mutating the aggregate", () => {
    const runtime = protocol();
    const value = envelope(runtime, "transaction:rollback");
    const transaction = runtime.prepare(value.envelopeId);
    runtime.applyMutation(transaction.transactionId, {
      domain: "query",
      operation: "set",
      path: ["temporary"],
      value: true,
      effective: true,
    });
    const rolledBack = runtime.rollback(transaction.transactionId, "validation failed");
    const repeated = runtime.rollback(transaction.transactionId, "ignored retry");

    expect(rolledBack.state).toBe("rolled_back");
    expect(rolledBack.afterState).toEqual(rolledBack.beforeState);
    expect(rolledBack.error).toBe("validation failed");
    expect(repeated.error).toBe("validation failed");
    expect(runtime.projectAggregate("aggregate-1").sequence).toBe(0);
  });

  test("cannot roll back a committed transaction", () => {
    const runtime = protocol();
    const value = envelope(runtime, "transaction:no-rollback");
    const transaction = runtime.prepare(value.envelopeId);
    runtime.commit(transaction.transactionId);

    expect(() => runtime.rollback(transaction.transactionId, "too late")).toThrow(
      "committed transaction cannot roll back",
    );
    expect(runtime.projectAggregate("aggregate-1").sequence).toBe(1);
  });

  test("refuses snapshots with applied uncommitted state", () => {
    const runtime = protocol();
    const value = envelope(runtime, "transaction:snapshot-fence");
    const transaction = runtime.prepare(value.envelopeId);
    runtime.applyMutation(transaction.transactionId, {
      domain: "query",
      operation: "set",
      path: ["unsafe_checkpoint"],
      value: true,
      effective: true,
    });

    expect(() => runtime.snapshot()).toThrow(
      "cannot snapshot with applied but uncommitted transaction",
    );
    runtime.rollback(transaction.transactionId, "snapshot fence");
    expect(runtime.snapshot().transactions[0]?.state).toBe("rolled_back");
  });
});

describe("effect delivery and restart", () => {
  test("leases one pending delivery to a stable owner", () => {
    const runtime = protocol();
    const value = envelope(runtime, "delivery:lease");
    const pending = deliveryFor(runtime, value.envelopeId);
    const leased = runtime.lease(pending.deliveryId, "dispatcher-a", 60_000);

    expect(leased.state).toBe("leased");
    expect(leased.owner).toBe("dispatcher-a");
    expect(leased.attempt).toBe(1);
    expect(leased.leasedAt).not.toBeNull();
    expect(Date.parse(leased.leaseExpiresAt!)).toBeGreaterThan(Date.now());
  });

  test("rejects another owner while a delivery lease is live", () => {
    const runtime = protocol();
    const value = envelope(runtime, "delivery:owner-conflict");
    const pending = deliveryFor(runtime, value.envelopeId);
    runtime.lease(pending.deliveryId, "dispatcher-a", 60_000);

    expect(() => runtime.lease(pending.deliveryId, "dispatcher-b", 60_000)).toThrow(
      "delivery lease held by dispatcher-a",
    );
    expect(runtime.pendingDeliveries()).toEqual([]);
  });

  test("does not ACK delivery before its state transaction commits", () => {
    const runtime = protocol();
    const value = envelope(runtime, "delivery:early-ack");
    const pending = deliveryFor(runtime, value.envelopeId);
    runtime.lease(pending.deliveryId, "dispatcher-a", 60_000);

    expect(() => runtime.ack(pending.deliveryId, "dispatcher-a")).toThrow(
      "delivery cannot ACK before transaction commit",
    );
    expect(runtime.snapshot().deliveries[0]?.state).toBe("leased");
  });

  test("ACKs committed delivery exactly once", () => {
    const runtime = protocol();
    const value = envelope(runtime, "delivery:ack");
    const transaction = runtime.prepare(value.envelopeId);
    runtime.commit(transaction.transactionId);
    const pending = deliveryFor(runtime, value.envelopeId);
    runtime.lease(pending.deliveryId, "dispatcher-a", 60_000);
    const acknowledged = runtime.ack(pending.deliveryId, "dispatcher-a");
    const repeated = runtime.ack(pending.deliveryId, "dispatcher-b");

    expect(acknowledged.state).toBe("acknowledged");
    expect(acknowledged.acknowledgedAt).not.toBeNull();
    expect(acknowledged.owner).toBeNull();
    expect(repeated).toEqual(acknowledged);
    expect(runtime.pendingDeliveries()).toEqual([]);
  });

  test("NACK schedules a retry and stores bounded error evidence", () => {
    const runtime = protocol();
    const value = envelope(runtime, "delivery:nack");
    const pending = deliveryFor(runtime, value.envelopeId);
    runtime.lease(pending.deliveryId, "dispatcher-a", 60_000);
    const failed = runtime.nack(
      pending.deliveryId,
      "dispatcher-a",
      "network_error",
      "edge transport disconnected",
      5_000,
    );

    expect(failed.state).toBe("retry_wait");
    expect(failed.owner).toBeNull();
    expect(failed.errorCode).toBe("network_error");
    expect(failed.errorMessage).toBe("edge transport disconnected");
    expect(Date.parse(failed.nextAttemptAt)).toBeGreaterThan(Date.now());
    expect(runtime.pendingDeliveries()).toEqual([]);
  });

  test("limits pending delivery projection without mutating queue", () => {
    const runtime = protocol();
    envelope(runtime, "delivery:limit-one");
    envelope(runtime, "delivery:limit-two");
    envelope(runtime, "delivery:limit-three");

    expect(runtime.pendingDeliveries(2)).toHaveLength(2);
    expect(runtime.pendingDeliveries(0)).toEqual([]);
    expect(runtime.snapshot().deliveries).toHaveLength(3);
  });

  test("round-trips committed aggregate and acknowledged delivery", () => {
    const before = protocol({ runId: "run-before" });
    const value = envelope(before, "restore:committed");
    const transaction = before.prepare(value.envelopeId);
    before.applyMutation(transaction.transactionId, {
      domain: "session",
      operation: "set",
      path: ["phase"],
      value: "complete",
      effective: true,
    });
    before.commit(transaction.transactionId);
    const pending = deliveryFor(before, value.envelopeId);
    before.lease(pending.deliveryId, "dispatcher-before", 60_000);
    before.ack(pending.deliveryId, "dispatcher-before");
    const snapshot = before.snapshot();
    const after = protocol({ runId: "run-after" });
    after.restore(snapshot, { allowRunRebind: true });

    expect(after.projectAggregate("aggregate-1").state).toEqual({ phase: "complete" });
    expect(after.snapshot().deliveries[0]?.state).toBe("acknowledged");
    expect(after.snapshot().restartEpoch).toBe(snapshot.restartEpoch + 1);
    expect(after.pendingDeliveries()).toEqual([]);
  });

  test("recovers a live delivery lease after process restart", () => {
    const before = protocol({ runId: "run-before" });
    const value = envelope(before, "restore:lease");
    const pending = deliveryFor(before, value.envelopeId);
    before.lease(pending.deliveryId, "dispatcher-before", 60_000);
    const snapshot = before.snapshot();
    const after = protocol({ runId: "run-after" });
    after.restore(snapshot, { allowRunRebind: true });
    const recovered = after.pendingDeliveries();

    expect(recovered).toHaveLength(1);
    expect(recovered[0]?.state).toBe("pending");
    expect(recovered[0]?.owner).toBeNull();
    expect(recovered[0]?.errorCode).toBe("lease_lost_on_restart");
    expect(after.lease(recovered[0]!.deliveryId, "dispatcher-after", 60_000).attempt).toBe(2);
  });

  test("uses the incremented restart epoch for new envelopes", () => {
    const before = protocol({ runId: "run-before" });
    envelope(before, "restore:epoch-before");
    const after = protocol({ runId: "run-after" });
    after.restore(before.snapshot(), { allowRunRebind: true });
    const fresh = envelope(after, "restore:epoch-after");

    expect(fresh.identity.runId).toBe("run-after");
    expect(fresh.identity.restartEpoch).toBe(1);
    expect(fresh.producerSequence).toBe(2);
  });

  test("rejects checksum and nested envelope digest corruption", () => {
    const source = protocol();
    envelope(source, "restore:integrity", { payload: { immutable: true } });
    const checksumTamper = source.snapshot();
    checksumTamper.revision += 1;
    expect(() => protocol().restore(checksumTamper)).toThrow(
      "effect protocol snapshot checksum mismatch",
    );

    const nestedTamper = source.snapshot();
    nestedTamper.envelopes[0]!.payload = { immutable: false };
    resignSnapshot(nestedTamper);
    expect(() => protocol().restore(nestedTamper)).toThrow(
      "effect envelope checksum mismatch",
    );
  });

  test("rejects a snapshot owned by another session", () => {
    const source = protocol({ sessionId: "session-source" });
    envelope(source, "restore:session-source");
    const target = protocol({ sessionId: "session-target", runId: "run-target" });

    expect(() => target.restore(source.snapshot())).toThrow(
      "effect protocol snapshot identity mismatch",
    );
    expect(target.snapshot().envelopes).toEqual([]);
  });

  test("routes transaction and delivery modules through the same state owners", () => {
    const runtime = protocol();
    const value = envelope(runtime, "module:transaction");
    const prepared = runtime.transaction_module({
      action: "prepare",
      envelope_id: value.envelopeId,
    });
    runtime.transaction_module({
      action: "mutate",
      transaction_id: prepared.transaction_id,
      domain: "query",
      operation: "set",
      path: ["phase"],
      value: "module",
      effective: true,
    });
    runtime.transaction_module({
      action: "commit",
      transaction_id: prepared.transaction_id,
    });
    const pending = deliveryFor(runtime, value.envelopeId);
    runtime.delivery_module({
      action: "lease",
      delivery_id: pending.deliveryId,
      owner: "dispatcher-module",
      ttl_ms: 60_000,
    });
    const acknowledged = runtime.delivery_module({
      action: "ack",
      delivery_id: pending.deliveryId,
      owner: "dispatcher-module",
    });

    expect(runtime.projectAggregate("aggregate-1").state).toEqual({ phase: "module" });
    expect(acknowledged.state).toBe("acknowledged");
  });
});
