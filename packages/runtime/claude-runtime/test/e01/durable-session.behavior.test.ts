import { describe, expect, test } from "bun:test";

import {
  DURABLE_CHECKPOINT_VERSION,
  DURABLE_SESSION_SNAPSHOT_VERSION,
  DurableSessionRuntime,
  type DurableSessionIdentity,
  type DurableSessionSnapshot,
} from "../../src/session/durable-runtime.js";
import { digest } from "../../src/e01/kernel.js";

function sessionIdentity(
  overrides: Partial<DurableSessionIdentity> = {},
): DurableSessionIdentity {
  return {
    sessionId: "session-1",
    runId: "run-1",
    taskId: "task-1",
    workerRequestId: "worker-request-1",
    tenantId: "tenant-1",
    parentSessionId: null,
    branchId: "main",
    ...overrides,
  };
}

function session(
  overrides: Partial<DurableSessionIdentity> = {},
  signingSecret = "e01-test-signing-secret",
): DurableSessionRuntime {
  return new DurableSessionRuntime(sessionIdentity(overrides), signingSecret);
}

function appendUser(
  runtime: DurableSessionRuntime,
  messageId: string,
  content: string,
) {
  return runtime.appendMessage({
    messageId,
    role: "user",
    content,
    turnId: `turn:${messageId}`,
    toolCallId: null,
    correlationId: `correlation:${messageId}`,
    causationId: null,
    metadata: { source: "behavior-test" },
    createdAt: "2026-07-15T00:00:00.000Z",
  });
}

function resignSnapshot(snapshot: DurableSessionSnapshot): DurableSessionSnapshot {
  const unsigned = { ...snapshot } as Partial<DurableSessionSnapshot>;
  delete unsigned.checksum;
  snapshot.checksum = digest(unsigned);
  return snapshot;
}

describe("durable session lifecycle", () => {
  test("starts as a scoped created session", () => {
    const runtime = session();
    const snapshot = runtime.snapshot();

    expect(snapshot.version).toBe(DURABLE_SESSION_SNAPSHOT_VERSION);
    expect(snapshot.identity).toEqual(sessionIdentity());
    expect(snapshot.status).toBe("created");
    expect(snapshot.restartEpoch).toBe(0);
    expect(snapshot.revision).toBe(0);
    expect(snapshot.sequence).toBe(0);
    expect(snapshot.messages).toEqual([]);
    expect(snapshot.state).toEqual({});
  });

  test("activates once and records the lifecycle mutation as a message", () => {
    const runtime = session();
    runtime.activate("correlation:activate");
    const first = runtime.snapshot();
    runtime.activate("correlation:duplicate");
    const repeated = runtime.snapshot();

    expect(first.status).toBe("active");
    expect(first.messages).toHaveLength(1);
    expect(first.messages[0]?.content).toEqual({
      event: "session_activated",
      restart_epoch: 0,
    });
    expect(first.messages[0]?.metadata.effective).toBe(true);
    expect(repeated.revision).toBe(first.revision);
    expect(repeated.messages).toHaveLength(1);
  });

  test("pauses and resumes an active session with distinct events", () => {
    const runtime = session();
    runtime.activate("correlation:activate");
    runtime.pause("correlation:pause");
    expect(runtime.project().status).toBe("paused");
    runtime.activate("correlation:resume");
    const snapshot = runtime.snapshot();

    expect(snapshot.status).toBe("active");
    expect(snapshot.messages.map((message) => message.content)).toEqual([
      { event: "session_activated", restart_epoch: 0 },
      { event: "session_paused" },
      { event: "session_activated", restart_epoch: 0 },
    ]);
    expect(snapshot.sequence).toBe(3);
  });

  test("cannot pause a session before activation", () => {
    const runtime = session();

    expect(() => runtime.pause("correlation:invalid-pause")).toThrow(
      "cannot pause session from created",
    );
    expect(runtime.project().status).toBe("created");
    expect(runtime.snapshot().messages).toEqual([]);
  });

  test("finishes an active session exactly once", () => {
    const runtime = session();
    runtime.activate("correlation:activate");
    runtime.finish("completed", "correlation:complete");
    const completed = runtime.snapshot();
    runtime.finish("failed", "correlation:late-failure");

    expect(completed.status).toBe("completed");
    expect(completed.messages.at(-1)?.content).toEqual({
      event: "session_completed",
    });
    expect(runtime.snapshot().status).toBe("completed");
    expect(runtime.snapshot().revision).toBe(completed.revision);
  });

  test("cannot reactivate a terminal session", () => {
    const runtime = session();
    runtime.activate("correlation:activate");
    runtime.finish("cancelled", "correlation:cancel");

    expect(() => runtime.activate("correlation:restart")).toThrow(
      "cannot activate session from cancelled",
    );
    expect(runtime.project().status).toBe("cancelled");
  });

  test("refuses terminal completion while an external effect executes", () => {
    const runtime = session();
    runtime.activate("correlation:activate");
    const effect = runtime.prepareEffect({
      effectKind: "tool.write",
      idempotencyKey: "effect:running",
      request: { path: "runtime.ts" },
    });
    runtime.startEffect(effect.effectId, "worker-1", 60_000);

    expect(() => runtime.finish("completed", "correlation:complete")).toThrow(
      "cannot finish session while external effects are executing",
    );
    expect(runtime.project().status).toBe("active");
  });
});

describe("durable session messages and state", () => {
  test("appends a stable message with causal identity", () => {
    const runtime = session();
    const message = runtime.appendMessage({
      messageId: "message-1",
      role: "assistant",
      content: { type: "analysis", text: "inspect owner state" },
      turnId: "turn-1",
      toolCallId: null,
      correlationId: "correlation-1",
      causationId: "message-parent",
      metadata: { model: "claude-sonnet" },
      createdAt: "2026-07-15T01:00:00.000Z",
    });

    expect(message.messageId).toBe("message-1");
    expect(message.contentDigest).toMatch(/^sha256:/);
    expect(message.correlationId).toBe("correlation-1");
    expect(message.causationId).toBe("message-parent");
    expect(message.metadata.model).toBe("claude-sonnet");
    expect(runtime.project().message_count).toBe(1);
  });

  test("deduplicates a message ID only for identical content", () => {
    const runtime = session();
    const first = appendUser(runtime, "message-stable", "same content");
    const repeated = appendUser(runtime, "message-stable", "same content");

    expect(repeated).toEqual(first);
    expect(runtime.snapshot().messages).toHaveLength(1);
    expect(() =>
      appendUser(runtime, "message-stable", "different content"),
    ).toThrow("message id conflict");
    expect(runtime.snapshot().messages[0]?.content).toBe("same content");
  });

  test("normalizes optional message identifiers", () => {
    const runtime = session();
    const message = runtime.appendMessage({
      role: "tool",
      content: { ok: true },
      turnId: "  ",
      toolCallId: "  tool-call-1  ",
      correlationId: "correlation-tool",
      causationId: "  ",
      metadata: {},
    });

    expect(message.turnId).toBeNull();
    expect(message.toolCallId).toBe("tool-call-1");
    expect(message.causationId).toBeNull();
    expect(message.createdAt).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });

  test("replaces the root state using compare-and-swap", () => {
    const runtime = session();
    const initialDigest = String(runtime.project().state_digest);
    const state = runtime.updateState(
      [],
      { phase: "reason", counters: { turns: 1 } },
      initialDigest,
    );

    expect(state).toEqual({ phase: "reason", counters: { turns: 1 } });
    expect(runtime.snapshot().state).toEqual(state);
    expect(() => runtime.updateState([], { phase: "stale" }, initialDigest)).toThrow(
      "session state compare-and-swap conflict",
    );
    expect(runtime.snapshot().state).toEqual(state);
  });

  test("updates a nested state path without deleting siblings", () => {
    const runtime = session();
    runtime.updateState([], {
      query: { phase: "reason", turn: 1 },
      provider: { model: "claude-sonnet" },
    });
    runtime.updateState(["query", "phase"], "tool");

    expect(runtime.snapshot().state).toEqual({
      query: { phase: "tool", turn: 1 },
      provider: { model: "claude-sonnet" },
    });
  });

  test("compares the digest at the exact nested state path", () => {
    const runtime = session();
    runtime.updateState(["query", "budget"], { tokens: 100 });
    const expected = digest({ tokens: 100 });
    runtime.updateState(["query", "budget"], { tokens: 80 }, expected);

    expect(runtime.snapshot().state.query).toEqual({ budget: { tokens: 80 } });
    expect(() =>
      runtime.updateState(["query", "budget"], { tokens: 60 }, expected),
    ).toThrow("session state compare-and-swap conflict");
  });

  test("rejects prototype-bearing state paths", () => {
    const runtime = session();

    expect(() => runtime.updateState(["__proto__", "polluted"], true)).toThrow(
      "invalid session state path",
    );
    expect(() => runtime.updateState(["constructor"], "unsafe")).toThrow(
      "invalid session state path",
    );
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
  });

  test("projects counts and state digest without exposing mutable state", () => {
    const runtime = session();
    appendUser(runtime, "message-projection", "projection");
    runtime.updateState(["status", "phase"], "observe");
    const snapshot = runtime.snapshot();
    snapshot.state.status = { phase: "tampered" };

    expect(runtime.project().message_count).toBe(1);
    expect(runtime.project().state_digest).not.toBe(digest(snapshot.state));
    expect(runtime.snapshot().state.status).toEqual({ phase: "observe" });
  });
});

describe("durable external effect fencing", () => {
  test("prepares an effect without executing it", () => {
    const runtime = session();
    const effect = runtime.prepareEffect({
      effectKind: "tool.shell",
      idempotencyKey: "effect:prepare",
      request: { command: "bun test" },
    });

    expect(effect.state).toBe("prepared");
    expect(effect.attempt).toBe(0);
    expect(effect.leaseOwner).toBeNull();
    expect(effect.requestDigest).toBe(digest({ command: "bun test" }));
    expect(runtime.project().pending_effect_count).toBe(1);
  });

  test("deduplicates effect preparation by idempotency key", () => {
    const runtime = session();
    const first = runtime.prepareEffect({
      effectKind: "tool.read",
      idempotencyKey: "effect:idem",
      request: { path: "runtime.ts" },
    });
    const repeated = runtime.prepareEffect({
      effectKind: "tool.read",
      idempotencyKey: "effect:idem",
      request: { path: "runtime.ts" },
    });

    expect(repeated.effectId).toBe(first.effectId);
    expect(runtime.snapshot().effects).toHaveLength(1);
    expect(() =>
      runtime.prepareEffect({
        effectKind: "tool.read",
        idempotencyKey: "effect:idem",
        request: { path: "different.ts" },
      }),
    ).toThrow("effect idempotency request conflict");
  });

  test("leases a prepared effect to one owner", () => {
    const runtime = session();
    const prepared = runtime.prepareEffect({
      effectKind: "tool.write",
      idempotencyKey: "effect:lease",
      request: { path: "runtime.ts" },
    });
    const leased = runtime.startEffect(prepared.effectId, "worker-a", 60_000);

    expect(leased.state).toBe("executing");
    expect(leased.attempt).toBe(1);
    expect(leased.leaseOwner).toBe("worker-a");
    expect(Date.parse(leased.leaseExpiresAt!)).toBeGreaterThan(Date.now());
    expect(() => runtime.startEffect(prepared.effectId, "worker-b", 60_000)).toThrow(
      "effect lease held by worker-a",
    );
  });

  test("commits an executing effect and fences its response", () => {
    const runtime = session();
    const prepared = runtime.prepareEffect({
      effectKind: "tool.write",
      idempotencyKey: "effect:commit",
      request: { path: "runtime.ts" },
    });
    runtime.startEffect(prepared.effectId, "worker-a", 60_000);
    const committed = runtime.commitEffect(prepared.effectId, {
      bytesWritten: 512,
    });
    const replay = runtime.commitEffect(prepared.effectId, {
      bytesWritten: 512,
    });

    expect(committed.state).toBe("committed");
    expect(committed.responseDigest).toBe(digest({ bytesWritten: 512 }));
    expect(committed.leaseOwner).toBeNull();
    expect(replay).toEqual(committed);
    expect(runtime.project().pending_effect_count).toBe(0);
  });

  test("rejects conflicting response replay after effect commit", () => {
    const runtime = session();
    const prepared = runtime.prepareEffect({
      effectKind: "provider.request",
      idempotencyKey: "effect:response-conflict",
      request: { requestId: "request-1" },
    });
    runtime.startEffect(prepared.effectId, "worker-a", 60_000);
    runtime.commitEffect(prepared.effectId, { status: 200 });

    expect(() => runtime.commitEffect(prepared.effectId, { status: 500 })).toThrow(
      "committed effect response conflict",
    );
    expect(runtime.snapshot().effects[0]?.responseDigest).toBe(
      digest({ status: 200 }),
    );
  });

  test("cannot commit an effect that was never started", () => {
    const runtime = session();
    const prepared = runtime.prepareEffect({
      effectKind: "tool.read",
      idempotencyKey: "effect:not-started",
      request: { path: "runtime.ts" },
    });

    expect(() => runtime.commitEffect(prepared.effectId, { ok: true })).toThrow(
      "effect cannot commit from prepared",
    );
  });

  test("records a failed effect and permits a new execution attempt", () => {
    const runtime = session();
    const prepared = runtime.prepareEffect({
      effectKind: "provider.request",
      idempotencyKey: "effect:retry",
      request: { requestId: "request-2" },
    });
    runtime.startEffect(prepared.effectId, "worker-a", 60_000);
    const failed = runtime.failEffect(
      prepared.effectId,
      "provider_timeout",
      "upstream timed out",
    );
    const retried = runtime.startEffect(prepared.effectId, "worker-b", 60_000);

    expect(failed.state).toBe("failed");
    expect(failed.errorCode).toBe("provider_timeout");
    expect(retried.state).toBe("executing");
    expect(retried.attempt).toBe(2);
    expect(retried.errorCode).toBeNull();
  });

  test("returns the committed effect for future preparation retries", () => {
    const runtime = session();
    const prepared = runtime.prepareEffect({
      effectKind: "tool.read",
      idempotencyKey: "effect:committed-idem",
      request: { path: "runtime.ts" },
    });
    runtime.startEffect(prepared.effectId, "worker-a", 60_000);
    const committed = runtime.commitEffect(prepared.effectId, { content: "source" });
    const repeated = runtime.prepareEffect({
      effectKind: "tool.read",
      idempotencyKey: "effect:committed-idem",
      request: { path: "runtime.ts" },
    });

    expect(repeated).toEqual(committed);
    expect(runtime.snapshot().committedIdempotency["effect:committed-idem"]).toBe(
      prepared.effectId,
    );
  });
});

describe("durable outbox delivery", () => {
  test("enqueues a pending ordered delivery", () => {
    const runtime = session();
    const item = runtime.enqueue({
      topic: "runtime.transition",
      partitionKey: "session-1",
      payload: { transitionId: "transition-1" },
      idempotencyKey: "outbox:transition-1",
    });

    expect(item.state).toBe("pending");
    expect(item.attempt).toBe(0);
    expect(item.payloadDigest).toBe(digest({ transitionId: "transition-1" }));
    expect(runtime.project().pending_outbox_count).toBe(1);
  });

  test("deduplicates outbox enqueue and rejects payload conflicts", () => {
    const runtime = session();
    const first = runtime.enqueue({
      topic: "runtime.event",
      partitionKey: "session-1",
      payload: { sequence: 1 },
      idempotencyKey: "outbox:stable",
    });
    const repeated = runtime.enqueue({
      topic: "runtime.event",
      partitionKey: "session-1",
      payload: { sequence: 1 },
      idempotencyKey: "outbox:stable",
    });

    expect(repeated.outboxId).toBe(first.outboxId);
    expect(runtime.snapshot().outbox).toHaveLength(1);
    expect(() =>
      runtime.enqueue({
        topic: "runtime.event",
        partitionKey: "session-1",
        payload: { sequence: 2 },
        idempotencyKey: "outbox:stable",
      }),
    ).toThrow("outbox idempotency payload conflict");
  });

  test("leases pending deliveries in creation order with a maximum", () => {
    const runtime = session();
    const first = runtime.enqueue({
      topic: "runtime.event",
      partitionKey: "session-1",
      payload: { sequence: 1 },
      idempotencyKey: "outbox:one",
    });
    runtime.enqueue({
      topic: "runtime.event",
      partitionKey: "session-1",
      payload: { sequence: 2 },
      idempotencyKey: "outbox:two",
    });
    const leased = runtime.leaseOutbox("dispatcher-a", 1, 60_000);

    expect(leased).toHaveLength(1);
    expect(leased[0]?.outboxId).toBe(first.outboxId);
    expect(leased[0]?.state).toBe("leased");
    expect(leased[0]?.attempt).toBe(1);
    expect(runtime.project().pending_outbox_count).toBe(2);
  });

  test("does not lease work already held by another live owner", () => {
    const runtime = session();
    runtime.enqueue({
      topic: "runtime.event",
      partitionKey: "session-1",
      payload: { sequence: 1 },
      idempotencyKey: "outbox:held",
    });
    runtime.leaseOutbox("dispatcher-a", 1, 60_000);

    expect(runtime.leaseOutbox("dispatcher-b", 10, 60_000)).toEqual([]);
    expect(runtime.snapshot().outbox[0]?.leaseOwner).toBe("dispatcher-a");
  });

  test("ACKs only for the active lease owner and remains idempotent", () => {
    const runtime = session();
    const item = runtime.enqueue({
      topic: "runtime.result",
      partitionKey: "session-1",
      payload: { resultId: "result-1" },
      idempotencyKey: "outbox:result",
    });
    runtime.leaseOutbox("dispatcher-a", 1, 60_000);

    expect(() => runtime.ackOutbox(item.outboxId, "dispatcher-b")).toThrow(
      "outbox lease owner mismatch",
    );
    const delivered = runtime.ackOutbox(item.outboxId, "dispatcher-a");
    const repeated = runtime.ackOutbox(item.outboxId, "dispatcher-b");
    expect(delivered.state).toBe("delivered");
    expect(delivered.deliveredAt).not.toBeNull();
    expect(repeated).toEqual(delivered);
    expect(runtime.project().pending_outbox_count).toBe(0);
  });

  test("NACK returns a leased delivery to delayed pending state", () => {
    const runtime = session();
    const item = runtime.enqueue({
      topic: "runtime.event",
      partitionKey: "session-1",
      payload: { sequence: 1 },
      idempotencyKey: "outbox:nack",
    });
    runtime.leaseOutbox("dispatcher-a", 1, 60_000);
    const failed = runtime.nackOutbox(
      item.outboxId,
      "dispatcher-a",
      "network disconnected",
      5_000,
    );

    expect(failed.state).toBe("pending");
    expect(failed.leaseOwner).toBeNull();
    expect(failed.lastError).toBe("network disconnected");
    expect(Date.parse(failed.nextAttemptAt)).toBeGreaterThan(Date.now());
    expect(runtime.leaseOutbox("dispatcher-b", 1, 60_000)).toEqual([]);
  });
});

describe("durable checkpoint and resume", () => {
  test("checkpoints state pending effects outbox and message head", () => {
    const runtime = session();
    runtime.activate("correlation:activate");
    const message = appendUser(runtime, "message-checkpoint", "checkpoint me");
    const effect = runtime.prepareEffect({
      effectKind: "tool.read",
      idempotencyKey: "effect:checkpoint",
      request: { path: "runtime.ts" },
    });
    const outbox = runtime.enqueue({
      topic: "runtime.event",
      partitionKey: "session-1",
      payload: { phase: "checkpoint" },
      idempotencyKey: "outbox:checkpoint",
    });
    const checkpoint = runtime.checkpoint({ phase: "checkpointed" });

    expect(checkpoint.version).toBe(DURABLE_CHECKPOINT_VERSION);
    expect(checkpoint.pendingEffectIds).toEqual([effect.effectId]);
    expect(checkpoint.pendingOutboxIds).toEqual([outbox.outboxId]);
    expect(checkpoint.messageHeadId).toBe(message.messageId);
    expect(checkpoint.state).toEqual({ phase: "checkpointed" });
    expect(checkpoint.stateDigest).toBe(digest({ phase: "checkpointed" }));
  });

  test("chains checkpoints through their parent identity", () => {
    const runtime = session();
    const first = runtime.checkpoint({ generation: 1 });
    const second = runtime.checkpoint({ generation: 2 });

    expect(first.parentCheckpointId).toBeNull();
    expect(second.parentCheckpointId).toBe(first.checkpointId);
    expect(runtime.project().checkpoint_count).toBe(2);
    expect(runtime.project().last_checkpoint_id).toBe(second.checkpointId);
  });

  test("issues a signed token for the latest checkpoint", () => {
    const runtime = session();
    const checkpoint = runtime.checkpoint({ phase: "paused" });
    const token = runtime.issueResumeToken("correlation:resume", 60_000);

    expect(token.sessionId).toBe("session-1");
    expect(token.checkpointId).toBe(checkpoint.checkpointId);
    expect(token.restartEpoch).toBe(0);
    expect(token.signature).toMatch(/^sha256:/);
    expect(token.nonce).toMatch(/^[a-f0-9]{32}$/);
    expect(token.consumedAt).toBeNull();
  });

  test("creates a checkpoint automatically before issuing the first token", () => {
    const runtime = session();
    runtime.updateState(["query", "phase"], "reason");
    const token = runtime.issueResumeToken("correlation:auto-checkpoint", 60_000);
    const snapshot = runtime.snapshot();

    expect(snapshot.checkpoints).toHaveLength(1);
    expect(token.checkpointId).toBe(snapshot.checkpoints[0]?.checkpointId);
    expect(snapshot.checkpoints[0]?.state).toEqual({
      query: { phase: "reason" },
    });
  });

  test("consumes a token once and advances restart epoch", () => {
    const runtime = session();
    const checkpoint = runtime.checkpoint({ phase: "before-resume" });
    const token = runtime.issueResumeToken("correlation:resume", 60_000);
    const resumed = runtime.consumeResumeToken(token);

    expect(resumed.checkpointId).toBe(checkpoint.checkpointId);
    expect(runtime.project().restart_epoch).toBe(1);
    expect(runtime.snapshot().resumeTokens[0]?.consumedAt).not.toBeNull();
    expect(() => runtime.consumeResumeToken(token)).toThrow(
      "resume token already consumed",
    );
  });

  test("rejects a token whose signed fields were changed", () => {
    const runtime = session();
    const token = runtime.issueResumeToken("correlation:resume", 60_000);
    token.correlationId = "correlation:forged";

    expect(() => runtime.consumeResumeToken(token)).toThrow(
      "resume token signature mismatch",
    );
    expect(runtime.project().restart_epoch).toBe(0);
  });

  test("rejects an expired signed token", async () => {
    const runtime = session();
    const token = runtime.issueResumeToken("correlation:short", 1);
    await new Promise((resolve) => setTimeout(resolve, 5));

    expect(() => runtime.consumeResumeToken(token)).toThrow("resume token expired");
    expect(runtime.project().restart_epoch).toBe(0);
  });

  test("restores state and increments restart epoch", () => {
    const before = session({ runId: "run-before" });
    before.activate("correlation:activate");
    appendUser(before, "message-before", "before restart");
    before.updateState(["query", "phase"], "observe");
    before.checkpoint();
    const snapshot = before.snapshot();
    const after = session({ runId: "run-after" });
    after.restore(snapshot);

    expect(after.snapshot().identity.runId).toBe("run-after");
    expect(after.snapshot().restartEpoch).toBe(snapshot.restartEpoch + 1);
    expect(after.snapshot().messages).toHaveLength(snapshot.messages.length);
    expect(after.snapshot().state).toEqual(snapshot.state);
    expect(after.project().status).toBe("active");
  });

  test("returns expired effect and outbox leases to recoverable state", () => {
    const before = session({ runId: "run-before" });
    const effect = before.prepareEffect({
      effectKind: "tool.write",
      idempotencyKey: "effect:lease-restore",
      request: { path: "runtime.ts" },
    });
    before.startEffect(effect.effectId, "worker-before", 60_000);
    const outbox = before.enqueue({
      topic: "runtime.event",
      partitionKey: "session-1",
      payload: { phase: "restore" },
      idempotencyKey: "outbox:lease-restore",
    });
    before.leaseOutbox("dispatcher-before", 1, 60_000);
    const snapshot = before.snapshot();
    snapshot.effects[0]!.leaseExpiresAt = "2020-01-01T00:00:00.000Z";
    snapshot.outbox[0]!.leaseExpiresAt = "2020-01-01T00:00:00.000Z";
    resignSnapshot(snapshot);
    const after = session({ runId: "run-after" });
    after.restore(snapshot);

    expect(after.snapshot().effects[0]?.state).toBe("prepared");
    expect(after.snapshot().effects[0]?.errorCode).toBe("lease_lost_on_restore");
    expect(after.snapshot().outbox[0]?.state).toBe("pending");
    expect(after.snapshot().outbox[0]?.leaseOwner).toBeNull();
    expect(after.leaseOutbox("dispatcher-after", 1, 60_000)[0]?.outboxId).toBe(
      outbox.outboxId,
    );
  });

  test("rejects a snapshot from another session or branch", () => {
    const source = session({ sessionId: "session-source" });
    const snapshot = source.snapshot();

    expect(() => session({ sessionId: "session-target" }).restore(snapshot)).toThrow(
      "durable session snapshot identity mismatch",
    );
    expect(() => session({ sessionId: "session-source", branchId: "other" }).restore(snapshot)).toThrow(
      "durable session snapshot identity mismatch",
    );
  });

  test("rejects checksum and nested content corruption", () => {
    const source = session();
    appendUser(source, "message-integrity", "immutable content");
    const checksumTamper = source.snapshot();
    checksumTamper.revision += 1;
    expect(() => session().restore(checksumTamper)).toThrow(
      "durable session snapshot checksum mismatch",
    );

    const nestedTamper = source.snapshot();
    nestedTamper.messages[0]!.content = "forged content";
    resignSnapshot(nestedTamper);
    expect(() => session().restore(nestedTamper)).toThrow(
      "durable message digest mismatch",
    );
  });

  test("routes lifecycle and snapshot module actions into canonical state", () => {
    const runtime = session();
    const activated = runtime.lifecycle_module({
      action: "activate",
      correlation_id: "correlation:module",
    });
    const checkpoint = runtime.snapshot_module({
      action: "checkpoint",
      state: { phase: "module-checkpoint" },
    });
    const inspection = runtime.lifecycle_module({ action: "inspect" });

    expect(activated.status).toBe("active");
    expect(checkpoint.version).toBe(DURABLE_CHECKPOINT_VERSION);
    expect(inspection.checkpoint_count).toBe(1);
    expect(runtime.snapshot().state).toEqual({ phase: "module-checkpoint" });
  });
});
