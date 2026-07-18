import assert from "node:assert/strict";
import { test } from "node:test";
import {
  digest,
  E03RuntimeError,
  sealTask,
  type E03Delivery,
  type E03TaskState,
} from "../../src/e03/contracts.ts";
import {
  DeliveryOutboxRuntime,
  publishReceipt,
  TeamDelivery,
  type DeliveryOutboxRecord,
} from "../../src/team/delivery.ts";
import {
  TeamFanout,
  type FanoutPlan,
  type FanoutTarget,
} from "../../src/team/fanout.ts";
import { scope, task, TestClock } from "./fixtures.ts";

function assertRuntimeCode(error: unknown, code: string): boolean {
  assert.ok(error instanceof E03RuntimeError);
  assert.equal(error.code, code);
  assert.ok(error.message.length > 0);
  return true;
}

function targets(count = 3): FanoutTarget[] {
  return Array.from({ length: count }, (_, index) => ({
    key: `branch-${index + 1}`,
    agent: index % 2 === 0 ? "reviewer" : "implementer",
    prompt: `Execute branch ${index + 1}`,
    metadata: {
      ordinal: index + 1,
      lane: index % 2 === 0 ? "read" : "write",
    },
  }));
}

function fanoutPlan(
  label: string,
  options: {
    count?: number;
    concurrency?: number;
    failureMode?: "collect" | "fail-fast";
  } = {},
): { parent: E03TaskState; runtime: TeamFanout; plan: FanoutPlan } {
  const parent = task(`${label}-parent`);
  const runtime = new TeamFanout();
  const plan = runtime.plan(parent, targets(options.count ?? 3), {
    maximumConcurrency: options.concurrency ?? 2,
    failureMode: options.failureMode ?? "collect",
    idempotencyKey: `fanout-${label}`,
  });
  return { parent, runtime, plan };
}

function branchTask(
  parent: E03TaskState,
  target: FanoutTarget,
  index: number,
  status:
    | "created"
    | "running"
    | "completed"
    | "failed"
    | "cancelled" = "completed",
): E03TaskState {
  return task(`${parent.identity.taskId}-child-${index + 1}`, {
    status,
    parentTaskId: parent.identity.taskId,
    parentSessionId: parent.identity.sessionId,
    fanoutKey: target.key,
  });
}

function deliveredTask(
  label: string,
  status: "running" | "waiting" | "completed" | "failed" = "running",
) {
  const clock = new TestClock("2026-07-18T12:00:00.000Z");
  const delivery = new TeamDelivery(clock, 4);
  const value = task(`${label}-task`, { status });
  return { clock, delivery, value };
}

function partialDelivery(label: string) {
  const fixture = deliveredTask(label, "running");
  const published = fixture.delivery.publishPartial(fixture.value, {
    summary: `Partial result for ${label}`,
    payload: { label, percent: 50 },
    artifactIds: [`artifact-${label}`, `artifact-${label}`],
    idempotencyKey: `partial-${label}`,
  });
  return { ...fixture, published };
}

function outboxFixture(label: string, maximumAttempts = 3) {
  const clock = new TestClock("2026-07-18T13:00:00.000Z");
  const delivery = new TeamDelivery(clock, 8);
  const running = task(`${label}-task`, { status: "running" });
  const published = delivery.publishPartial(running, {
    summary: `Publish ${label}`,
    payload: { evidence: label },
    artifactIds: [`artifact-${label}`],
    idempotencyKey: `delivery-${label}`,
  });
  const outbox = new DeliveryOutboxRuntime(clock, 8);
  const record = outbox.prepare(published.task, published.delivery, {
    idempotencyKey: `outbox-${label}`,
    maximumAttempts,
  });
  return { clock, delivery, published, outbox, record };
}

function receiptFor(
  record: DeliveryOutboxRecord,
  overrides: Partial<{
    accepted: boolean;
    duplicate: boolean;
    remoteSequence: number;
    error: string;
    completedAt: string;
  }> = {},
) {
  return publishReceipt({
    outboxId: record.outboxId,
    deliveryId: record.delivery.deliveryId,
    accepted: overrides.accepted ?? true,
    duplicate: overrides.duplicate ?? false,
    remoteSequence: overrides.remoteSequence ?? 1,
    error: overrides.error ?? "",
    completedAt: overrides.completedAt ?? "2026-07-18T13:30:00.000Z",
  });
}

test("e03 fanout plan normalizes targets and caps concurrency", () => {
  const parent = task("plan-normalize-parent");
  const runtime = new TeamFanout();
  const plan = runtime.plan(
    parent,
    [
      {
        key: " alpha ",
        agent: " reviewer ",
        prompt: " inspect alpha ",
        metadata: { lane: "read" },
      },
      {
        key: "beta",
        agent: "implementer",
        prompt: "write beta",
      },
    ],
    {
      maximumConcurrency: 99,
      failureMode: "collect",
      idempotencyKey: "normalize-key",
    },
  );
  assert.equal(plan.parentTaskId, parent.identity.taskId);
  assert.equal(plan.targets.length, 2);
  assert.equal(plan.maximumConcurrency, 2);
  assert.equal(plan.failureMode, "collect");
  assert.equal(plan.targets[0]?.key, "alpha");
  assert.equal(plan.targets[0]?.agent, "reviewer");
  assert.equal(plan.targets[0]?.prompt, "inspect alpha");
  assert.deepEqual(plan.targets[0]?.metadata, { lane: "read" });
  assert.equal(plan.targets[1]?.key, "beta");
  assert.equal(plan.digest.length, 64);
});

test("e03 fanout plan is deterministic for a stable idempotency identity", () => {
  const parent = task("plan-determinism-parent");
  const runtime = new TeamFanout();
  const input = {
    maximumConcurrency: 2,
    failureMode: "fail-fast" as const,
    idempotencyKey: "stable-plan-key",
  };
  const first = runtime.plan(parent, targets(3), input);
  const second = runtime.plan(parent, targets(3), input);
  assert.equal(first.planId, second.planId);
  assert.equal(first.digest, second.digest);
  assert.deepEqual(first.targets, second.targets);
  assert.equal(first.failureMode, "fail-fast");
  assert.equal(first.maximumConcurrency, 2);
});

test("e03 fanout failure rejects a parent scope without fanout custody", () => {
  const parent = task("plan-denied-parent", {
    scope: scope({ allowFanout: false }),
  });
  const runtime = new TeamFanout();
  assert.throws(
    () =>
      runtime.plan(parent, targets(1), {
        idempotencyKey: "denied-plan",
      }),
    (error) => assertRuntimeCode(error, "fanout_not_permitted"),
  );
});

test("e03 fanout failure rejects an empty target set", () => {
  const parent = task("plan-empty-parent");
  const runtime = new TeamFanout();
  assert.throws(
    () =>
      runtime.plan(parent, [], {
        idempotencyKey: "empty-plan",
      }),
    (error) => assertRuntimeCode(error, "empty_fanout"),
  );
});

test("e03 fanout failure enforces delegated child limit", () => {
  const parent = task("plan-limit-parent", {
    scope: scope({ maxChildren: 2 }),
  });
  const runtime = new TeamFanout();
  assert.throws(
    () =>
      runtime.plan(parent, targets(3), {
        idempotencyKey: "limit-plan",
      }),
    (error) => assertRuntimeCode(error, "fanout_child_limit"),
  );
});

test("e03 fanout failure rejects duplicate normalized branch keys", () => {
  const parent = task("plan-duplicate-parent");
  const runtime = new TeamFanout();
  assert.throws(
    () =>
      runtime.plan(
        parent,
        [
          { key: "same", agent: "a", prompt: "first" },
          { key: " same ", agent: "b", prompt: "second" },
        ],
        { idempotencyKey: "duplicate-plan" },
      ),
    (error) => assertRuntimeCode(error, "duplicate_fanout_key"),
  );
});

test("e03 fanout failure rejects incomplete branch identity", () => {
  const parent = task("plan-invalid-target-parent");
  const runtime = new TeamFanout();
  assert.throws(
    () =>
      runtime.plan(parent, [{ key: "alpha", agent: " ", prompt: "run" }], {
        idempotencyKey: "invalid-target-plan",
      }),
    (error) => assertRuntimeCode(error, "invalid_fanout_target"),
  );
});

test("e03 fanout failure rejects a non-positive concurrency", () => {
  const parent = task("plan-concurrency-parent");
  const runtime = new TeamFanout();
  assert.throws(
    () =>
      runtime.plan(parent, targets(2), {
        maximumConcurrency: 0,
        idempotencyKey: "invalid-concurrency-plan",
      }),
    (error) => assertRuntimeCode(error, "invalid_fanout_concurrency"),
  );
});

test("e03 fanout dispatch collects every completed branch", async () => {
  const { parent, runtime, plan } = fanoutPlan("dispatch-complete", {
    count: 4,
    concurrency: 2,
  });
  const started: string[] = [];
  const result = await runtime.dispatch(plan, async (target, index) => {
    started.push(target.key);
    return branchTask(parent, target, index, "completed");
  });
  assert.equal(result.ok, true);
  assert.equal(result.completed.length, 4);
  assert.equal(result.failed.length, 0);
  assert.equal(result.cancelled.length, 0);
  assert.equal(result.pending.length, 0);
  assert.deepEqual(started, ["branch-1", "branch-2", "branch-3", "branch-4"]);
  assert.deepEqual(
    result.completed.map((value) => value.definition.metadata.fanout_key),
    ["branch-1", "branch-2", "branch-3", "branch-4"],
  );
  assert.equal(result.summary, "4 completed, 0 failed, 0 cancelled, 0 pending");
});

test("e03 fanout dispatch converts a thrown branch into a failed result", async () => {
  const { runtime, plan } = fanoutPlan("dispatch-throw", {
    count: 2,
    concurrency: 1,
  });
  const result = await runtime.dispatch(plan, async (target, index) => {
    if (index === 0) throw new Error(`branch exploded: ${target.key}`);
    return task(`throw-child-${index}`, {
      status: "completed",
      parentTaskId: plan.parentTaskId,
      fanoutKey: target.key,
    });
  });
  assert.equal(result.ok, false);
  assert.equal(result.completed.length, 1);
  assert.equal(result.failed.length, 1);
  assert.equal(result.cancelled.length, 0);
  assert.equal(result.failed[0]?.status, "failed");
  assert.match(result.failed[0]?.error ?? "", /branch exploded/);
  assert.equal(result.failed[0]?.definition.metadata.fanout_key, "branch-1");
});

test("e03 fanout dispatch fail-fast cancels undispatched branches", async () => {
  const { parent, runtime, plan } = fanoutPlan("dispatch-fail-fast", {
    count: 4,
    concurrency: 1,
    failureMode: "fail-fast",
  });
  const invoked: number[] = [];
  const result = await runtime.dispatch(plan, async (target, index) => {
    invoked.push(index);
    return branchTask(
      parent,
      target,
      index,
      index === 0 ? "failed" : "completed",
    );
  });
  assert.deepEqual(invoked, [0]);
  assert.equal(result.ok, false);
  assert.equal(result.failed.length, 1);
  assert.equal(result.cancelled.length, 3);
  assert.equal(result.completed.length, 0);
  assert.equal(result.pending.length, 0);
  assert.ok(result.cancelled.every((value) => value.status === "cancelled"));
  assert.deepEqual(
    result.cancelled.map((value) => value.definition.metadata.fanout_key),
    ["branch-2", "branch-3", "branch-4"],
  );
});

test("e03 fanout dispatch fail-fast invokes cancellation for active work", async () => {
  const { parent, runtime, plan } = fanoutPlan("dispatch-active-cancel", {
    count: 2,
    concurrency: 2,
    failureMode: "fail-fast",
  });
  const cancelled: string[] = [];
  const result = await runtime.dispatch(
    plan,
    async (target, index) => {
      if (index === 0) return branchTask(parent, target, index, "failed");
      return branchTask(parent, target, index, "running");
    },
    async (active, reason) => {
      cancelled.push(`${active.identity.taskId}:${reason}`);
      return branchTask(parent, plan.targets[1]!, 1, "cancelled");
    },
  );
  assert.equal(result.failed.length, 1);
  assert.equal(result.cancelled.length, 1);
  assert.equal(result.completed.length, 0);
  assert.equal(cancelled.length, 1);
  assert.match(cancelled[0]!, /fanout_fail_fast/);
  assert.match(cancelled[0]!, /child-1/);
});

test("e03 fanin collect sorts branch results by canonical key", () => {
  const { parent, runtime, plan } = fanoutPlan("collect-sort", { count: 3 });
  const values = [
    branchTask(parent, plan.targets[2]!, 2, "completed"),
    branchTask(parent, plan.targets[0]!, 0, "completed"),
    branchTask(parent, plan.targets[1]!, 1, "completed"),
  ];
  const result = runtime.collect(plan, values);
  assert.equal(result.ok, true);
  assert.deepEqual(
    result.completed.map((value) => value.definition.metadata.fanout_key),
    ["branch-1", "branch-2", "branch-3"],
  );
  assert.equal(
    result.completed[0]?.identity.parentTaskId,
    parent.identity.taskId,
  );
  assert.equal(result.completed[1]?.status, "completed");
  assert.equal(result.summary, "3 completed, 0 failed, 0 cancelled, 0 pending");
});

test("e03 fanin collect distinguishes completed failed cancelled and pending", () => {
  const { parent, runtime, plan } = fanoutPlan("collect-partitions", {
    count: 4,
  });
  const values = [
    branchTask(parent, plan.targets[0]!, 0, "completed"),
    branchTask(parent, plan.targets[1]!, 1, "failed"),
    branchTask(parent, plan.targets[2]!, 2, "cancelled"),
    branchTask(parent, plan.targets[3]!, 3, "running"),
  ];
  const result = runtime.collect(plan, values);
  assert.equal(result.ok, false);
  assert.equal(result.completed.length, 1);
  assert.equal(result.failed.length, 1);
  assert.equal(result.cancelled.length, 1);
  assert.equal(result.pending.length, 1);
  assert.equal(result.failed[0]?.definition.metadata.fanout_key, "branch-2");
  assert.equal(result.cancelled[0]?.definition.metadata.fanout_key, "branch-3");
  assert.equal(result.pending[0]?.definition.metadata.fanout_key, "branch-4");
  assert.equal(result.summary, "1 completed, 1 failed, 1 cancelled, 1 pending");
});

test("e03 fanin collect deduplicates byte-identical task results", () => {
  const { parent, runtime, plan } = fanoutPlan("collect-dedupe", { count: 1 });
  const first = branchTask(parent, plan.targets[0]!, 0, "completed");
  const result = runtime.collect(plan, [first, structuredClone(first)]);
  assert.equal(result.ok, true);
  assert.equal(result.completed.length, 1);
  assert.equal(result.completed[0]?.identity.taskId, first.identity.taskId);
  assert.equal(result.failed.length, 0);
});

test("e03 fanin failure rejects an unknown branch key", () => {
  const { parent, runtime, plan } = fanoutPlan("collect-unknown", { count: 1 });
  const unknown = task("collect-unknown-child", {
    status: "completed",
    parentTaskId: parent.identity.taskId,
    fanoutKey: "not-in-plan",
  });
  assert.throws(
    () => runtime.collect(plan, [unknown]),
    (error) => assertRuntimeCode(error, "fanout_result_mismatch"),
  );
});

test("e03 fanin failure rejects conflicting results for one task identity", () => {
  const { parent, runtime, plan } = fanoutPlan("collect-conflict", {
    count: 1,
  });
  const first = branchTask(parent, plan.targets[0]!, 0, "completed");
  const conflicting = sealTask({
    ...first,
    result: { ok: false, conflict: true },
    checksum: "",
  });
  assert.notEqual(first.checksum, conflicting.checksum);
  assert.throws(
    () => runtime.collect(plan, [first, conflicting]),
    (error) => assertRuntimeCode(error, "fanout_result_conflict"),
  );
});

test("e03 fanout failure rejects a tampered plan before dispatch", async () => {
  const { runtime, plan } = fanoutPlan("tampered-dispatch", { count: 2 });
  const tampered = { ...plan, maximumConcurrency: 1 };
  await assert.rejects(
    () => runtime.dispatch(tampered, async () => task("unused")),
    (error) => assertRuntimeCode(error, "fanout_plan_checksum"),
  );
});

test("e03 fanout fail-fast decision reports every terminal failure", () => {
  const { parent, runtime, plan } = fanoutPlan("decision-fail-fast", {
    count: 3,
    failureMode: "fail-fast",
  });
  const failed = branchTask(parent, plan.targets[0]!, 0, "failed");
  const cancelled = branchTask(parent, plan.targets[1]!, 1, "cancelled");
  const completed = branchTask(parent, plan.targets[2]!, 2, "completed");
  const decision = runtime.failFast(plan, [completed, failed, cancelled]);
  assert.equal(decision.stop, true);
  assert.deepEqual(decision.failingTaskIds, [
    failed.identity.taskId,
    cancelled.identity.taskId,
  ]);
  assert.match(decision.reason, /failed/);
  assert.match(decision.reason, /cancelled/);
});

test("e03 fanout collect-mode decision does not stop after failure", () => {
  const { parent, runtime, plan } = fanoutPlan("decision-collect", {
    count: 1,
    failureMode: "collect",
  });
  const failed = branchTask(parent, plan.targets[0]!, 0, "failed");
  const decision = runtime.failFast(plan, [failed]);
  assert.equal(decision.stop, false);
  assert.deepEqual(decision.failingTaskIds, [failed.identity.taskId]);
  assert.equal(decision.reason, "fanout member ended: failed");
});

test("e03 partial delivery enters canonical task state", () => {
  const { value, published } = partialDelivery("partial-canonical");
  assert.equal(published.delivery.taskId, value.identity.taskId);
  assert.equal(published.delivery.kind, "partial");
  assert.equal(published.delivery.sequence, 1);
  assert.equal(
    published.delivery.summary,
    "Partial result for partial-canonical",
  );
  assert.deepEqual(published.delivery.payload, {
    label: "partial-canonical",
    percent: 50,
  });
  assert.deepEqual(published.delivery.artifactIds, [
    "artifact-partial-canonical",
  ]);
  assert.equal(published.delivery.acknowledgedAt, null);
  assert.equal(published.delivery.digest.length, 64);
  assert.equal(published.task.deliveries.length, 1);
  assert.equal(published.task.deliveries[0]?.digest, published.delivery.digest);
  assert.equal(published.task.sequence, value.sequence + 1);
  assert.notEqual(published.task.checksum, value.checksum);
});

test("e03 partial delivery replay returns the canonical prior envelope", () => {
  const { delivery, published } = partialDelivery("partial-replay");
  const replay = delivery.publishPartial(published.task, {
    summary: "Partial result for partial-replay",
    payload: { changed: "ignored by canonical identity" },
    artifactIds: ["different-artifact"],
    idempotencyKey: "partial-partial-replay",
  });
  assert.equal(replay.delivery.deliveryId, published.delivery.deliveryId);
  assert.equal(replay.delivery.digest, published.delivery.digest);
  assert.equal(replay.task.checksum, published.task.checksum);
  assert.deepEqual(replay.delivery.payload, published.delivery.payload);
  assert.deepEqual(replay.delivery.artifactIds, published.delivery.artifactIds);
});

test("e03 delivery failure rejects idempotency reuse with another summary", () => {
  const { delivery, published } = partialDelivery("partial-conflict");
  assert.throws(
    () =>
      delivery.publishPartial(published.task, {
        summary: "Different summary",
        idempotencyKey: "partial-partial-conflict",
      }),
    (error) => assertRuntimeCode(error, "delivery_idempotency_conflict"),
  );
});

test("e03 delivery failure rejects partial output after terminal state", () => {
  const { delivery, value } = deliveredTask("partial-terminal", "completed");
  assert.throws(
    () =>
      delivery.publishPartial(value, {
        summary: "too late",
        idempotencyKey: "late-partial",
      }),
    (error) => assertRuntimeCode(error, "partial_delivery_phase"),
  );
});

test("e03 final delivery represents a completed child outcome", () => {
  const { delivery, value } = deliveredTask("final-completed", "completed");
  const output = delivery.publishFinal(value, {
    summary: "Child completed with evidence",
    payload: { verdict: "pass" },
    artifactIds: ["artifact-final"],
    idempotencyKey: "final-completed-key",
  });
  assert.equal(output.delivery.kind, "final");
  assert.equal(output.delivery.sequence, 1);
  assert.equal(output.delivery.taskId, value.identity.taskId);
  assert.equal(output.delivery.summary, "Child completed with evidence");
  assert.deepEqual(output.delivery.payload, { verdict: "pass" });
  assert.deepEqual(output.delivery.artifactIds, ["artifact-final"]);
  assert.equal(output.task.deliveries.length, 1);
  assert.equal(
    output.task.deliveries[0]?.deliveryId,
    output.delivery.deliveryId,
  );
});

test("e03 final delivery represents a failed child outcome", () => {
  const { delivery, value } = deliveredTask("final-failed", "failed");
  const output = delivery.publishFinal(value, {
    summary: "Child failed deterministically",
    payload: { error: value.error },
    idempotencyKey: "final-failed-key",
  });
  assert.equal(output.delivery.kind, "final");
  assert.equal(output.delivery.taskId, value.identity.taskId);
  assert.match(String(output.delivery.payload.error), /test-failed/);
  assert.equal(output.delivery.digest.length, 64);
  assert.equal(output.task.status, "failed");
});

test("e03 final delivery replay preserves one terminal envelope per attempt", () => {
  const { delivery, value } = deliveredTask("final-replay", "completed");
  const first = delivery.publishFinal(value, {
    summary: "Stable terminal result",
    idempotencyKey: "final-replay-key",
  });
  const replay = delivery.publishFinal(first.task, {
    summary: "Stable terminal result",
    idempotencyKey: "final-replay-key",
  });
  assert.equal(replay.delivery.deliveryId, first.delivery.deliveryId);
  assert.equal(replay.delivery.digest, first.delivery.digest);
  assert.equal(replay.task.checksum, first.task.checksum);
  assert.equal(replay.task.deliveries.length, 1);
});

test("e03 delivery failure rejects a second final envelope in one attempt", () => {
  const { delivery, value } = deliveredTask("final-duplicate", "completed");
  const first = delivery.publishFinal(value, {
    summary: "First final",
    idempotencyKey: "first-final-key",
  });
  assert.throws(
    () =>
      delivery.publishFinal(first.task, {
        summary: "Second final",
        idempotencyKey: "second-final-key",
      }),
    (error) => assertRuntimeCode(error, "duplicate_final_delivery"),
  );
});

test("e03 delivery failure rejects final output from a running task", () => {
  const { delivery, value } = deliveredTask("final-running", "running");
  assert.throws(
    () =>
      delivery.publishFinal(value, {
        summary: "not final yet",
        idempotencyKey: "invalid-final-phase",
      }),
    (error) => assertRuntimeCode(error, "final_delivery_phase"),
  );
});

test("e03 delivery acknowledgment seals parent-visible receipt state", () => {
  const { delivery, published } = partialDelivery("delivery-ack");
  const acknowledged = delivery.acknowledge(
    published.task,
    published.delivery.deliveryId,
  );
  assert.ok(acknowledged.deliveries[0]?.acknowledgedAt);
  assert.equal(
    acknowledged.deliveries[0]?.deliveryId,
    published.delivery.deliveryId,
  );
  assert.notEqual(
    acknowledged.deliveries[0]?.digest,
    published.delivery.digest,
  );
  assert.equal(acknowledged.sequence, published.task.sequence + 1);
  assert.notEqual(acknowledged.checksum, published.task.checksum);
  const replay = delivery.acknowledge(
    acknowledged,
    published.delivery.deliveryId,
  );
  assert.equal(replay.checksum, acknowledged.checksum);
  assert.equal(replay.sequence, acknowledged.sequence);
});

test("e03 delivery failure rejects acknowledgment of an unknown envelope", () => {
  const { delivery, published } = partialDelivery("delivery-unknown-ack");
  assert.throws(
    () => delivery.acknowledge(published.task, "missing-delivery"),
    (error) => assertRuntimeCode(error, "unknown_delivery"),
  );
});

test("e03 delivery backpressure exposes pending count and limit", () => {
  const clock = new TestClock("2026-07-18T12:10:00.000Z");
  const delivery = new TeamDelivery(clock, 1);
  const running = task("delivery-pressure", { status: "running" });
  const first = delivery.publishPartial(running, {
    summary: "first pending",
    idempotencyKey: "pressure-first",
  });
  const pressure = delivery.applyBackpressure(first.task);
  assert.equal(pressure.accepted, false);
  assert.equal(pressure.pending, 1);
  assert.equal(pressure.limit, 1);
  assert.equal(pressure.reason, "delivery_backpressure");
  assert.throws(
    () =>
      delivery.publishPartial(first.task, {
        summary: "second pending",
        idempotencyKey: "pressure-second",
      }),
    (error) => assertRuntimeCode(error, "delivery_backpressure"),
  );
  const acknowledged = delivery.acknowledge(
    first.task,
    first.delivery.deliveryId,
  );
  assert.equal(delivery.applyBackpressure(acknowledged).accepted, true);
});

test("e03 delivery failure validates late-kind revision and terminal policy", () => {
  const { delivery, value } = deliveredTask("delivery-late", "completed");
  assert.throws(
    () => delivery.rejectLate(value, "partial", value.revision - 1),
    (error) => assertRuntimeCode(error, "stale_revision"),
  );
  assert.throws(
    () => delivery.rejectLate(value, "partial", value.revision),
    (error) => assertRuntimeCode(error, "late_delivery"),
  );
  assert.doesNotThrow(() =>
    delivery.rejectLate(value, "final", value.revision),
  );
  assert.doesNotThrow(() =>
    delivery.rejectLate(value, "artifact", value.revision),
  );
});

test("e03 delivery failure rejects an empty or oversized summary", () => {
  const { delivery, value } = deliveredTask("delivery-summary", "running");
  assert.throws(
    () =>
      delivery.publishPartial(value, {
        summary: "   ",
        idempotencyKey: "empty-summary",
      }),
    (error) => assertRuntimeCode(error, "invalid_delivery_summary"),
  );
  assert.throws(
    () =>
      delivery.publishPartial(value, {
        summary: "x".repeat(256_001),
        idempotencyKey: "oversized-summary",
      }),
    (error) => assertRuntimeCode(error, "invalid_delivery_summary"),
  );
});

test("e03 outbox prepare captures canonical delivery custody", () => {
  const { published, outbox, record } = outboxFixture("prepare-custody");
  assert.equal(record.taskId, published.task.identity.taskId);
  assert.equal(record.parentTaskId, published.task.identity.parentTaskId);
  assert.equal(record.taskLeaseId, published.task.identity.leaseId);
  assert.equal(record.taskRevision, published.task.revision);
  assert.equal(record.delivery.digest, published.delivery.digest);
  assert.equal(record.phase, "prepared");
  assert.equal(record.attempt, 0);
  assert.equal(record.maximumAttempts, 3);
  assert.equal(record.idempotencyKey, "outbox-prepare-custody");
  assert.equal(record.digest.length, 64);
  assert.deepEqual(outbox.pending(published.task.identity.taskId), [record]);
  assert.equal(outbox.projection().prepared, 1);
});

test("e03 outbox prepare replays the same custody record", () => {
  const { published, outbox, record } = outboxFixture("prepare-replay");
  const replay = outbox.prepare(published.task, published.delivery, {
    idempotencyKey: "outbox-prepare-replay",
    maximumAttempts: 99,
  });
  assert.equal(replay.outboxId, record.outboxId);
  assert.equal(replay.digest, record.digest);
  assert.equal(replay.maximumAttempts, 3);
  assert.equal(outbox.snapshot().length, 1);
  assert.equal(outbox.pending().length, 1);
});

test("e03 outbox failure rejects a delivery from another task", () => {
  const { published, outbox } = outboxFixture("prepare-task-mismatch");
  const other = task("other-outbox-task", { status: "running" });
  assert.throws(
    () =>
      outbox.prepare(other, published.delivery, {
        idempotencyKey: "outbox-task-mismatch",
      }),
    (error) => assertRuntimeCode(error, "delivery_task_mismatch"),
  );
});

test("e03 outbox failure rejects a delivery absent from canonical task state", () => {
  const { published, outbox } = outboxFixture("prepare-uncommitted");
  const prior = task(published.task.identity.taskId, { status: "running" });
  assert.throws(
    () =>
      outbox.prepare(prior, published.delivery, {
        idempotencyKey: "outbox-uncommitted",
      }),
    (error) => assertRuntimeCode(error, "delivery_not_committed"),
  );
});

test("e03 outbox failure rejects a corrupt delivery digest", () => {
  const { published, outbox } = outboxFixture("prepare-corrupt-delivery");
  const corrupted: E03Delivery = {
    ...published.delivery,
    digest: digest("tampered-delivery"),
  };
  assert.throws(
    () =>
      outbox.prepare(published.task, corrupted, {
        idempotencyKey: "outbox-corrupt-delivery",
      }),
    (error) => assertRuntimeCode(error, "delivery_digest_mismatch"),
  );
});

test("e03 outbox failure rejects idempotency reuse across task revision", () => {
  const { delivery, published, outbox } = outboxFixture("prepare-conflict");
  outbox.prepare(published.task, published.delivery, {
    idempotencyKey: "outbox-prepare-conflict",
  });
  const acknowledged = delivery.acknowledge(
    published.task,
    published.delivery.deliveryId,
  );
  assert.throws(
    () =>
      outbox.prepare(acknowledged, acknowledged.deliveries[0]!, {
        idempotencyKey: "outbox-prepare-conflict",
      }),
    (error) => assertRuntimeCode(error, "outbox_idempotency_conflict"),
  );
});

test("e03 outbox claim selects available records in deterministic order", () => {
  const first = outboxFixture("claim-order-first");
  const secondDelivery = first.delivery.publishPartial(first.published.task, {
    summary: "second ordered delivery",
    idempotencyKey: "delivery-claim-order-second",
  });
  const second = first.outbox.prepare(
    secondDelivery.task,
    secondDelivery.delivery,
    {
      idempotencyKey: "outbox-claim-order-second",
      availableAt: "2026-07-18T12:59:59.000Z",
    },
  );
  const batch = first.outbox.claim({
    maximumRecords: 10,
    maximumBytes: 1_000_000,
    now: "2026-07-18T14:00:00.000Z",
  });
  assert.equal(batch.records.length, 2);
  assert.equal(batch.records[0]?.outboxId, second.outboxId);
  assert.equal(batch.records[1]?.outboxId, first.record.outboxId);
  assert.ok(batch.byteSize > 0);
  assert.equal(batch.hasMore, false);
  assert.match(batch.batchId, /^outbox-batch-/);
});

test("e03 outbox claim honors record and byte backpressure", () => {
  const fixture = outboxFixture("claim-limit");
  const secondDelivery = fixture.delivery.publishPartial(
    fixture.published.task,
    {
      summary: "another delivery for a limited batch",
      idempotencyKey: "delivery-claim-limit-second",
    },
  );
  fixture.outbox.prepare(secondDelivery.task, secondDelivery.delivery, {
    idempotencyKey: "outbox-claim-limit-second",
  });
  const batch = fixture.outbox.claim({
    maximumRecords: 1,
    maximumBytes: 1_000_000,
    now: "2026-07-18T14:00:00.000Z",
  });
  assert.equal(batch.records.length, 1);
  assert.equal(batch.hasMore, true);
  assert.ok(batch.byteSize > 0);
  assert.equal(fixture.outbox.pending().length, 2);
});

test("e03 outbox failure rejects invalid claim limits", () => {
  const { outbox } = outboxFixture("claim-invalid");
  assert.throws(
    () => outbox.claim({ maximumRecords: 0, maximumBytes: 1024 }),
    (error) => assertRuntimeCode(error, "invalid_outbox_batch"),
  );
  assert.throws(
    () => outbox.claim({ maximumRecords: 1, maximumBytes: 0 }),
    (error) => assertRuntimeCode(error, "invalid_outbox_batch"),
  );
});

test("e03 outbox failure rejects a record larger than a fresh batch", () => {
  const { outbox } = outboxFixture("claim-too-large");
  assert.throws(
    () =>
      outbox.claim({
        maximumRecords: 1,
        maximumBytes: 1,
        now: "2026-07-18T14:00:00.000Z",
      }),
    (error) => assertRuntimeCode(error, "outbox_record_too_large"),
  );
});

test("e03 outbox publish and acknowledgment close delivery custody", () => {
  const { outbox, record } = outboxFixture("publish-ack");
  const receipt = receiptFor(record, { remoteSequence: 41 });
  const published = outbox.markPublished(record.outboxId, receipt);
  assert.equal(published.phase, "published");
  assert.equal(published.attempt, 1);
  assert.equal(published.publishedAt, receipt.completedAt);
  assert.equal(published.lastError, "");
  assert.equal(outbox.projection().published, 1);
  const acknowledged = outbox.acknowledge(record.outboxId, receipt);
  assert.equal(acknowledged.phase, "acknowledged");
  assert.equal(acknowledged.acknowledgedAt, receipt.completedAt);
  assert.equal(outbox.pending().length, 0);
  assert.equal(outbox.projection().acknowledged, 1);
  assert.deepEqual(outbox.projection().pending_delivery_ids, []);
});

test("e03 outbox duplicate receipt may acknowledge before local publish", () => {
  const { outbox, record } = outboxFixture("duplicate-ack");
  const receipt = receiptFor(record, {
    duplicate: true,
    remoteSequence: 99,
  });
  const acknowledged = outbox.acknowledge(record.outboxId, receipt);
  assert.equal(acknowledged.phase, "acknowledged");
  assert.equal(acknowledged.attempt, 0);
  assert.equal(acknowledged.publishedAt, receipt.completedAt);
  assert.equal(acknowledged.acknowledgedAt, receipt.completedAt);
  assert.equal(outbox.pending().length, 0);
});

test("e03 outbox failure rejects acknowledgment before publish", () => {
  const { outbox, record } = outboxFixture("ack-before-publish");
  const receipt = receiptFor(record, { duplicate: false });
  assert.throws(
    () => outbox.acknowledge(record.outboxId, receipt),
    (error) => assertRuntimeCode(error, "delivery_ack_before_publish"),
  );
});

test("e03 outbox failure rejects a corrupt publish receipt", () => {
  const { outbox, record } = outboxFixture("receipt-corrupt");
  const receipt = { ...receiptFor(record), remoteSequence: 77 };
  assert.throws(
    () => outbox.markPublished(record.outboxId, receipt),
    (error) => assertRuntimeCode(error, "publish_receipt_digest_mismatch"),
  );
});

test("e03 outbox failure rejects a receipt for another custody record", () => {
  const first = outboxFixture("receipt-first");
  const second = outboxFixture("receipt-second");
  const receipt = receiptFor(second.record);
  assert.throws(
    () => first.outbox.markPublished(first.record.outboxId, receipt),
    (error) => assertRuntimeCode(error, "publish_receipt_custody_mismatch"),
  );
});

test("e03 outbox rejected publish schedules deterministic retry", () => {
  const { outbox, record } = outboxFixture("publish-rejected", 4);
  const receipt = receiptFor(record, {
    accepted: false,
    error: "remote unavailable",
  });
  const retry = outbox.markPublished(record.outboxId, receipt);
  assert.equal(retry.phase, "prepared");
  assert.equal(retry.attempt, 1);
  assert.equal(retry.lastError, "remote unavailable");
  assert.ok(retry.availableAt > record.availableAt);
  assert.equal(retry.publishedAt, null);
  assert.equal(outbox.pending().length, 1);
});

test("e03 outbox retry exhausts attempts into dead letter", () => {
  const { outbox, record } = outboxFixture("retry-exhausted", 2);
  const first = outbox.retry(record.outboxId, "first failure", 1);
  assert.equal(first.phase, "prepared");
  assert.equal(first.attempt, 1);
  assert.equal(first.lastError, "first failure");
  const second = outbox.retry(record.outboxId, "second failure", 1);
  assert.equal(second.phase, "dead-lettered");
  assert.equal(second.attempt, 2);
  assert.equal(second.maximumAttempts, 2);
  assert.equal(second.lastError, "second failure");
  assert.equal(outbox.pending().length, 0);
  assert.equal(outbox.projection().dead_lettered, 1);
});

test("e03 outbox failure requires a retry reason", () => {
  const { outbox, record } = outboxFixture("retry-reason");
  assert.throws(
    () => outbox.retry(record.outboxId, "   "),
    (error) => assertRuntimeCode(error, "outbox_retry_reason_missing"),
  );
});

test("e03 outbox dead letter cannot publish or be acknowledged", () => {
  const { outbox, record } = outboxFixture("dead-letter-guards");
  const dead = outbox.deadLetter(record.outboxId, "policy rejected");
  assert.equal(dead.phase, "dead-lettered");
  assert.equal(dead.lastError, "policy rejected");
  const receipt = receiptFor(record);
  assert.throws(
    () => outbox.markPublished(record.outboxId, receipt),
    (error) => assertRuntimeCode(error, "outbox_dead_lettered"),
  );
  assert.throws(
    () => outbox.acknowledge(record.outboxId, receipt),
    (error) => assertRuntimeCode(error, "delivery_ack_before_publish"),
  );
});

test("e03 outbox acknowledged record is stable under duplicate settlement", () => {
  const { outbox, record } = outboxFixture("settlement-replay");
  const receipt = receiptFor(record);
  const published = outbox.markPublished(record.outboxId, receipt);
  const acknowledged = outbox.acknowledge(record.outboxId, receipt);
  const publishReplay = outbox.markPublished(record.outboxId, receipt);
  const acknowledgeReplay = outbox.acknowledge(record.outboxId, receipt);
  assert.equal(published.phase, "published");
  assert.equal(acknowledged.phase, "acknowledged");
  assert.equal(publishReplay.digest, acknowledged.digest);
  assert.equal(acknowledgeReplay.digest, acknowledged.digest);
  assert.equal(acknowledgeReplay.attempt, 1);
});

test("e03 outbox snapshot restores pending and settled projections", () => {
  const first = outboxFixture("snapshot-first");
  const secondDelivery = first.delivery.publishPartial(first.published.task, {
    summary: "second snapshot delivery",
    idempotencyKey: "delivery-snapshot-second",
  });
  const second = first.outbox.prepare(
    secondDelivery.task,
    secondDelivery.delivery,
    {
      idempotencyKey: "outbox-snapshot-second",
    },
  );
  const receipt = receiptFor(second, { remoteSequence: 2 });
  first.outbox.markPublished(second.outboxId, receipt);
  first.outbox.acknowledge(second.outboxId, receipt);
  const snapshot = first.outbox.snapshot();
  const restored = new DeliveryOutboxRuntime(
    new TestClock("2026-07-18T15:00:00.000Z"),
    8,
  );
  restored.restore(snapshot);
  assert.deepEqual(restored.snapshot(), snapshot);
  assert.equal(restored.pending().length, 1);
  assert.equal(restored.pending()[0]?.outboxId, first.record.outboxId);
  assert.equal(restored.projection().prepared, 1);
  assert.equal(restored.projection().acknowledged, 1);
  assert.equal(restored.projection().total, 2);
});

test("e03 outbox failure rejects a corrupt restored record", () => {
  const { outbox, record } = outboxFixture("restore-corrupt");
  const corrupted = { ...record, attempt: 1 };
  assert.throws(
    () => outbox.restore([corrupted]),
    (error) => assertRuntimeCode(error, "outbox_digest_mismatch"),
  );
});

test("e03 outbox failure rejects duplicate restored record identity", () => {
  const { outbox, record } = outboxFixture("restore-duplicate");
  assert.throws(
    () => outbox.restore([record, structuredClone(record)]),
    (error) => assertRuntimeCode(error, "duplicate_outbox_record"),
  );
});

test("e03 outbox failure rejects restored idempotency alias collision", () => {
  const { outbox, record } = outboxFixture("restore-key-collision");
  const payload = {
    ...record,
    outboxId: `${record.outboxId}-other`,
    digest: "",
  };
  const { digest: _discarded, ...unsigned } = payload;
  const other = { ...unsigned, digest: digest(unsigned) };
  assert.throws(
    () => outbox.restore([record, other]),
    (error) => assertRuntimeCode(error, "outbox_idempotency_conflict"),
  );
});

test("e03 outbox failure rejects invalid construction and attempt bounds", () => {
  assert.throws(
    () => new DeliveryOutboxRuntime(new TestClock(), 0),
    (error) => assertRuntimeCode(error, "invalid_outbox_limit"),
  );
  const { published } = outboxFixture("invalid-attempts-source");
  const runtime = new DeliveryOutboxRuntime(new TestClock(), 2);
  assert.throws(
    () =>
      runtime.prepare(published.task, published.delivery, {
        idempotencyKey: "invalid-attempts-zero",
        maximumAttempts: 0,
      }),
    (error) => assertRuntimeCode(error, "invalid_outbox_attempts"),
  );
  assert.throws(
    () =>
      runtime.prepare(published.task, published.delivery, {
        idempotencyKey: "invalid-attempts-high",
        maximumAttempts: 101,
      }),
    (error) => assertRuntimeCode(error, "invalid_outbox_attempts"),
  );
});

test("e03 delivery failure rejects invalid backpressure construction", () => {
  assert.throws(
    () => new TeamDelivery(new TestClock(), 0),
    (error) => assertRuntimeCode(error, "invalid_backpressure_limit"),
  );
});
