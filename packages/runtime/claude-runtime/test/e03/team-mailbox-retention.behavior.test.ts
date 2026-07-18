import assert from "node:assert/strict";
import { test } from "node:test";
import { digest, E03RuntimeError } from "../../src/e03/contracts.ts";
import {
  MailboxConsumerGroupRuntime,
  MailboxRetentionRuntime,
  TeamMailbox,
} from "../../src/team/mailbox.ts";
import { task, TestClock } from "./fixtures.ts";

function teamPair(label: string) {
  const parent = task(`${label}-parent`, {
    parentTaskId: `${label}-root`,
    parentSessionId: `${label}-root-session`,
  });
  const child = task(`${label}-child`, {
    parentTaskId: parent.identity.taskId,
    parentSessionId: parent.identity.sessionId,
  });
  return { parent, child };
}

function retainedMessage(label: string) {
  const clock = new TestClock("2026-07-18T01:00:00.000Z");
  const mailbox = new TeamMailbox(clock);
  const pair = teamPair(label);
  const sent = mailbox.send(pair.parent, pair.child, {
    body: `retained payload ${label}`,
    kind: "prompt",
    idempotencyKey: `message-${label}`,
  });
  return { clock, mailbox, pair, sent };
}

function retentionRuntime(
  label: string,
  options: { max?: number; acknowledgedOnly?: boolean } = {},
) {
  const clock = new TestClock("2026-07-18T02:00:00.000Z");
  const runtime = new MailboxRetentionRuntime(clock);
  const policy = runtime.registerPolicy({
    policyId: `policy-${label}`,
    topicId: `topic-${label}`,
    retentionMs: 60_000,
    maxEventsPerSegment: options.max ?? 2,
    minSegmentsToRetain: 1,
    compactAcknowledgedOnly: options.acknowledgedOnly ?? true,
  });
  return { clock, runtime, policy };
}

function groupWithMember(
  label: string,
  partitionCount = 2,
  capacity = partitionCount,
) {
  const clock = new TestClock("2026-07-18T03:00:00.000Z");
  const runtime = new MailboxConsumerGroupRuntime(clock);
  let group = runtime.createGroup({
    groupId: `group-${label}`,
    topicId: `topic-${label}`,
    partitionCount,
  });
  const joined = runtime.join({
    groupId: group.groupId,
    expectedRevision: group.revision,
    ownerId: `owner-${label}`,
    capacity,
    leaseMs: 60_000,
  });
  group = joined.group;
  return { clock, runtime, group, member: joined.member };
}

function assertRuntimeCode(error: unknown, code: string): boolean {
  assert.ok(error instanceof E03RuntimeError);
  assert.equal(error.code, code);
  assert.ok(error.message.length > 0);
  return true;
}

test("e03.team mailbox sends and receives an owned child message", () => {
  const clock = new TestClock("2026-07-18T01:00:00.000Z");
  const mailbox = new TeamMailbox(clock);
  const { parent, child } = teamPair("send-receive");
  const sent = mailbox.send(parent, child, {
    body: "Inspect the artifact and report evidence.",
    kind: "prompt",
    idempotencyKey: "send-receive-key",
  });
  assert.equal(sent.message.senderTaskId, parent.identity.taskId);
  assert.equal(sent.message.recipientTaskId, child.identity.taskId);
  assert.equal(sent.message.taskId, child.identity.taskId);
  assert.equal(sent.message.sequence, 1);
  assert.equal(sent.message.kind, "prompt");
  assert.equal(sent.message.deliveredAt, null);
  assert.equal(sent.message.acknowledgedAt, null);
  assert.equal(sent.recipient.messages.length, 1);
  const received = mailbox.receive(sent.recipient, 1);
  assert.equal(received.messages.length, 1);
  assert.equal(received.messages[0]?.messageId, sent.message.messageId);
  assert.ok(received.messages[0]?.deliveredAt);
  assert.equal(mailbox.pending(received.task).length, 1);
  const acknowledged = mailbox.acknowledge(
    received.task,
    sent.message.messageId,
  );
  assert.ok(acknowledged.messages[0]?.acknowledgedAt);
  assert.equal(mailbox.pending(acknowledged).length, 0);
});

test("e03.team mailbox replays an exact duplicate send", () => {
  const clock = new TestClock("2026-07-18T01:05:00.000Z");
  const mailbox = new TeamMailbox(clock);
  const { parent, child } = teamPair("duplicate-replay");
  const first = mailbox.send(parent, child, {
    body: "same body",
    idempotencyKey: "duplicate-replay-key",
  });
  const replay = mailbox.send(parent, first.recipient, {
    body: "same body",
    idempotencyKey: "duplicate-replay-key",
  });
  assert.equal(replay.message.messageId, first.message.messageId);
  assert.equal(replay.message.digest, first.message.digest);
  assert.equal(replay.recipient.checksum, first.recipient.checksum);
  assert.equal(replay.recipient.messages.length, 1);
  assert.deepEqual(replay.recipient.messages, first.recipient.messages);
  const selected = mailbox.rejectDuplicate(
    replay.recipient,
    "duplicate-replay-key",
    digest("same body"),
  );
  assert.equal(selected?.messageId, first.message.messageId);
  assert.equal(selected?.body, "same body");
});

test("e03.team mailbox rejects duplicate content conflict", () => {
  const clock = new TestClock("2026-07-18T01:10:00.000Z");
  const mailbox = new TeamMailbox(clock);
  const { parent, child } = teamPair("duplicate-conflict");
  const first = mailbox.send(parent, child, {
    body: "original",
    idempotencyKey: "duplicate-conflict-key",
  });
  assert.throws(
    () =>
      mailbox.send(parent, first.recipient, {
        body: "changed",
        idempotencyKey: "duplicate-conflict-key",
      }),
    (error) => assertRuntimeCode(error, "message_idempotency_conflict"),
  );
  assert.throws(
    () =>
      mailbox.rejectDuplicate(
        first.recipient,
        "duplicate-conflict-key",
        digest("changed"),
      ),
    (error) => assertRuntimeCode(error, "message_idempotency_conflict"),
  );
  assert.equal(first.recipient.messages.length, 1);
  assert.equal(first.recipient.messages[0]?.body, "original");
});

test("e03.team mailbox denies a cross-run escape", () => {
  const mailbox = new TeamMailbox(new TestClock("2026-07-18T01:15:00.000Z"));
  const { parent } = teamPair("cross-run-parent");
  const child = task("cross-run-child", {
    runId: "another-run",
    parentTaskId: parent.identity.taskId,
    parentSessionId: parent.identity.sessionId,
  });
  assert.notEqual(parent.identity.runId, child.identity.runId);
  assert.throws(
    () =>
      mailbox.send(parent, child, {
        body: "must not cross authority",
        idempotencyKey: "cross-run-denied",
      }),
    (error) => assertRuntimeCode(error, "cross_run_message"),
  );
  assert.equal(child.messages.length, 0);
  assert.equal(child.sequence, 1);
});

test("e03.team mailbox denies unrelated task ownership", () => {
  const mailbox = new TeamMailbox(new TestClock("2026-07-18T01:20:00.000Z"));
  const sender = task("unrelated-sender", {
    parentTaskId: "sender-root",
  });
  const recipient = task("unrelated-recipient", {
    parentTaskId: "recipient-root",
  });
  assert.equal(sender.identity.runId, recipient.identity.runId);
  assert.notEqual(
    sender.identity.parentTaskId,
    recipient.identity.parentTaskId,
  );
  assert.throws(
    () =>
      mailbox.send(sender, recipient, {
        body: "ownership must be checked",
        idempotencyKey: "ownership-denied",
      }),
    (error) => assertRuntimeCode(error, "message_ownership_denied"),
  );
  assert.equal(recipient.messages.length, 0);
});

test("e03.team mailbox rejects a terminal recipient", () => {
  const mailbox = new TeamMailbox(new TestClock("2026-07-18T01:25:00.000Z"));
  const parent = task("terminal-parent", {
    parentTaskId: "terminal-root",
  });
  const terminal = task("terminal-child", {
    parentTaskId: parent.identity.taskId,
    status: "completed",
  });
  assert.equal(terminal.status, "completed");
  assert.throws(
    () =>
      mailbox.send(parent, terminal, {
        body: "too late",
        idempotencyKey: "terminal-message",
      }),
    (error) => assertRuntimeCode(error, "terminal_recipient"),
  );
  assert.equal(terminal.messages.length, 0);
});

test("e03.team mailbox rejects invalid receive limits", () => {
  const { mailbox, sent } = retainedMessage("invalid-receive");
  assert.throws(
    () => mailbox.receive(sent.recipient, 0),
    (error) => assertRuntimeCode(error, "invalid_receive_limit"),
  );
  assert.throws(
    () => mailbox.receive(sent.recipient, 10_001),
    (error) => assertRuntimeCode(error, "invalid_receive_limit"),
  );
  assert.throws(
    () => mailbox.receive(sent.recipient, 1.5),
    (error) => assertRuntimeCode(error, "invalid_receive_limit"),
  );
  assert.equal(sent.recipient.messages[0]?.deliveredAt, null);
});

test("e03.team mailbox rejects ACK before delivery", () => {
  const { mailbox, sent } = retainedMessage("ack-before-delivery");
  assert.equal(sent.message.deliveredAt, null);
  assert.throws(
    () => mailbox.acknowledge(sent.recipient, sent.message.messageId),
    (error) => assertRuntimeCode(error, "message_not_delivered"),
  );
  assert.equal(sent.recipient.messages[0]?.acknowledgedAt, null);
  const received = mailbox.receive(sent.recipient);
  const acknowledged = mailbox.acknowledge(
    received.task,
    sent.message.messageId,
  );
  assert.ok(acknowledged.messages[0]?.acknowledgedAt);
});

test("e03.team mailbox rejects unknown ACK identity", () => {
  const { mailbox, sent } = retainedMessage("unknown-ack");
  const received = mailbox.receive(sent.recipient);
  assert.throws(
    () => mailbox.acknowledge(received.task, "missing-message"),
    (error) => assertRuntimeCode(error, "unknown_message"),
  );
  assert.equal(received.task.messages.length, 1);
  assert.equal(received.task.messages[0]?.acknowledgedAt, null);
});

test("e03.team mailbox steers an active child", () => {
  const clock = new TestClock("2026-07-18T01:35:00.000Z");
  const mailbox = new TeamMailbox(clock);
  const parent = task("steer-parent", { parentTaskId: "steer-root" });
  const running = task("steer-child", {
    parentTaskId: parent.identity.taskId,
    status: "running",
  });
  const steered = mailbox.steer(parent, running, {
    body: "Prioritize verification before implementation.",
    idempotencyKey: "steer-active",
  });
  assert.equal(steered.message.kind, "steer");
  assert.equal(steered.message.sequence, 1);
  assert.equal(steered.recipient.status, "running");
  assert.equal(steered.recipient.messages.length, 1);
  assert.equal(steered.recipient.messages[0]?.digest, steered.message.digest);
});

test("e03.team mailbox rejects late steering", () => {
  const mailbox = new TeamMailbox(new TestClock("2026-07-18T01:40:00.000Z"));
  const parent = task("late-steer-parent", { parentTaskId: "steer-root" });
  const failed = task("late-steer-child", {
    parentTaskId: parent.identity.taskId,
    status: "failed",
  });
  assert.throws(
    () =>
      mailbox.steer(parent, failed, {
        body: "This must not revive a terminal task.",
        idempotencyKey: "late-steer",
      }),
    (error) => assertRuntimeCode(error, "task_not_steerable"),
  );
  assert.equal(failed.messages.length, 0);
  assert.equal(failed.status, "failed");
});

test("e03.mailbox retention appends idempotently", () => {
  const { runtime, policy } = retentionRuntime("append-idempotent");
  const { sent } = retainedMessage("append-idempotent");
  const first = runtime.append({
    policyId: policy.policyId,
    message: sent.message,
  });
  const replay = runtime.append({
    policyId: policy.policyId,
    message: sent.message,
  });
  assert.equal(first.event.retentionEventId, replay.event.retentionEventId);
  assert.equal(first.event.digest, replay.event.digest);
  assert.equal(first.segment.segmentId, replay.segment.segmentId);
  assert.equal(first.event.sequence, 1);
  assert.equal(first.event.acknowledged, false);
  assert.equal(runtime.snapshot().events.length, 1);
  assert.equal(runtime.snapshot().segments.length, 1);
});

test("e03.mailbox retention seals full segments", () => {
  const { runtime, policy } = retentionRuntime("segment-seal", { max: 1 });
  const first = retainedMessage("segment-seal-first").sent.message;
  const second = retainedMessage("segment-seal-second").sent.message;
  const appendedFirst = runtime.append({
    policyId: policy.policyId,
    message: first,
  });
  const appendedSecond = runtime.append({
    policyId: policy.policyId,
    message: second,
  });
  assert.notEqual(
    appendedFirst.segment.segmentId,
    appendedSecond.segment.segmentId,
  );
  const snapshot = runtime.snapshot();
  assert.equal(snapshot.events.length, 2);
  assert.equal(snapshot.segments.length, 2);
  assert.equal(snapshot.segments[0]?.state, "sealed");
  assert.equal(snapshot.segments[0]?.eventCount, 1);
  assert.equal(snapshot.segments[1]?.state, "open");
  assert.equal(snapshot.segments[1]?.eventCount, 1);
});

test("e03.mailbox retention acknowledges exactly once", () => {
  const { runtime, policy } = retentionRuntime("ack-once");
  const message = retainedMessage("ack-once").sent.message;
  const appended = runtime.append({
    policyId: policy.policyId,
    message,
  });
  const acknowledged = runtime.acknowledge({
    retentionEventId: appended.event.retentionEventId,
    expectedMessageDigest: message.digest,
  });
  const replay = runtime.acknowledge({
    retentionEventId: appended.event.retentionEventId,
    expectedMessageDigest: message.digest,
  });
  assert.equal(acknowledged.acknowledged, true);
  assert.equal(replay.digest, acknowledged.digest);
  const snapshot = runtime.snapshot();
  assert.equal(snapshot.events[0]?.acknowledged, true);
  assert.equal(snapshot.segments[0]?.acknowledgedCount, 1);
  assert.ok(snapshot.segments[0]?.eventDigests.includes(acknowledged.digest));
});

test("e03.mailbox retention rejects ACK digest tamper", () => {
  const { runtime, policy } = retentionRuntime("ack-tamper");
  const message = retainedMessage("ack-tamper").sent.message;
  const appended = runtime.append({
    policyId: policy.policyId,
    message,
  });
  assert.throws(
    () =>
      runtime.acknowledge({
        retentionEventId: appended.event.retentionEventId,
        expectedMessageDigest: digest("tampered"),
      }),
    (error) =>
      assertRuntimeCode(error, "mailbox_retention_message_digest_mismatch"),
  );
  const snapshot = runtime.snapshot();
  assert.equal(snapshot.events[0]?.acknowledged, false);
  assert.equal(snapshot.segments[0]?.acknowledgedCount, 0);
});

test("e03.mailbox retention compacts acknowledged sealed events", () => {
  const { runtime, policy } = retentionRuntime("compact-ack", { max: 1 });
  const first = retainedMessage("compact-ack-first").sent.message;
  const second = retainedMessage("compact-ack-second").sent.message;
  const a = runtime.append({ policyId: policy.policyId, message: first });
  runtime.append({ policyId: policy.policyId, message: second });
  runtime.acknowledge({
    retentionEventId: a.event.retentionEventId,
    expectedMessageDigest: first.digest,
  });
  const sealed = runtime
    .snapshot()
    .segments.find((segment) => segment.segmentId === a.segment.segmentId)!;
  assert.equal(sealed.state, "sealed");
  const receipt = runtime.compact({
    policyId: policy.policyId,
    sourceSegmentIds: [sealed.segmentId],
    expectedRevisions: { [sealed.segmentId]: sealed.revision },
  });
  assert.deepEqual(receipt.removedEventIds, [a.event.retentionEventId]);
  assert.deepEqual(receipt.retainedEventIds, []);
  assert.equal(receipt.sourceSegmentIds.length, 1);
  assert.ok(receipt.targetSegmentId);
  assert.equal(runtime.snapshot().events.length, 1);
});

test("e03.mailbox retention rejects compaction of open segment", () => {
  const { runtime, policy } = retentionRuntime("compact-open", { max: 3 });
  const message = retainedMessage("compact-open").sent.message;
  const appended = runtime.append({
    policyId: policy.policyId,
    message,
  });
  assert.equal(appended.segment.state, "open");
  assert.throws(
    () =>
      runtime.compact({
        policyId: policy.policyId,
        sourceSegmentIds: [appended.segment.segmentId],
        expectedRevisions: {
          [appended.segment.segmentId]: appended.segment.revision,
        },
      }),
    (error) => assertRuntimeCode(error, "mailbox_compaction_source_not_sealed"),
  );
  assert.equal(runtime.snapshot().receipts.length, 0);
});

test("e03.mailbox retention legal hold denies compaction", () => {
  const { runtime, policy } = retentionRuntime("legal-hold", { max: 1 });
  const first = retainedMessage("legal-hold-first").sent.message;
  const second = retainedMessage("legal-hold-second").sent.message;
  const appended = runtime.append({
    policyId: policy.policyId,
    message: first,
  });
  runtime.append({ policyId: policy.policyId, message: second });
  const held = runtime.setLegalHold({
    policyId: policy.policyId,
    expectedRevision: policy.revision,
    enabled: true,
  });
  assert.equal(held.legalHold, true);
  assert.equal(held.revision, policy.revision + 1);
  const sealed = runtime
    .snapshot()
    .segments.find(
      (segment) => segment.segmentId === appended.segment.segmentId,
    )!;
  assert.throws(
    () =>
      runtime.compact({
        policyId: policy.policyId,
        sourceSegmentIds: [sealed.segmentId],
        expectedRevisions: { [sealed.segmentId]: sealed.revision },
      }),
    (error) => assertRuntimeCode(error, "mailbox_retention_legal_hold"),
  );
});

test("e03.mailbox retention rejects stale legal-hold revision", () => {
  const { runtime, policy } = retentionRuntime("stale-hold");
  const held = runtime.setLegalHold({
    policyId: policy.policyId,
    expectedRevision: policy.revision,
    enabled: true,
  });
  assert.equal(held.revision, 2);
  assert.throws(
    () =>
      runtime.setLegalHold({
        policyId: policy.policyId,
        expectedRevision: policy.revision,
        enabled: false,
      }),
    (error) =>
      assertRuntimeCode(error, "mailbox_retention_policy_stale_revision"),
  );
  assert.equal(runtime.snapshot().policies[0]?.legalHold, true);
});

test("e03.mailbox retention restores an exact snapshot", () => {
  const { runtime, policy } = retentionRuntime("snapshot-restore", { max: 1 });
  const first = retainedMessage("snapshot-restore-first").sent.message;
  const second = retainedMessage("snapshot-restore-second").sent.message;
  runtime.append({ policyId: policy.policyId, message: first });
  runtime.append({ policyId: policy.policyId, message: second });
  const snapshot = runtime.snapshot();
  const restored = new MailboxRetentionRuntime(
    new TestClock("2026-07-18T04:00:00.000Z"),
  );
  restored.restore(snapshot);
  assert.deepEqual(restored.snapshot(), snapshot);
  assert.equal(restored.snapshot().policies.length, 1);
  assert.equal(restored.snapshot().events.length, 2);
  assert.equal(restored.snapshot().segments.length, 2);
  assert.deepEqual(restored.snapshot().nextSequenceByTopic, [
    [policy.topicId, 3],
  ]);
});

test("e03.mailbox retention rejects corrupt snapshot", () => {
  const { runtime, policy } = retentionRuntime("corrupt-snapshot");
  runtime.append({
    policyId: policy.policyId,
    message: retainedMessage("corrupt-snapshot").sent.message,
  });
  const snapshot = runtime.snapshot();
  const tampered = {
    ...snapshot,
    nextSequenceByTopic: [[policy.topicId, 999]] as Array<[string, number]>,
  };
  const restored = new MailboxRetentionRuntime();
  assert.throws(
    () => restored.restore(tampered),
    (error) => assertRuntimeCode(error, "mailbox_retention_snapshot_corrupt"),
  );
  assert.equal(restored.snapshot().policies.length, 0);
  assert.equal(restored.snapshot().events.length, 0);
});

test("e03.mailbox consumer group creates and joins idempotently", () => {
  const { runtime, group, member } = groupWithMember("join-idempotent");
  const replay = runtime.join({
    groupId: group.groupId,
    expectedRevision: group.revision,
    ownerId: member.ownerId,
    capacity: member.capacity,
    leaseMs: 60_000,
  });
  assert.equal(replay.member.memberId, member.memberId);
  assert.equal(replay.member.digest, member.digest);
  assert.equal(replay.group.digest, group.digest);
  assert.equal(replay.group.memberIds.length, 1);
  assert.equal(replay.member.state, "joining");
  assert.equal(runtime.snapshot().members.length, 1);
  assert.equal(runtime.snapshot().groups.length, 1);
});

test("e03.mailbox consumer group rejects conflicting identity", () => {
  const runtime = new MailboxConsumerGroupRuntime(
    new TestClock("2026-07-18T03:05:00.000Z"),
  );
  const original = runtime.createGroup({
    groupId: "group-conflict",
    topicId: "topic-original",
    partitionCount: 2,
  });
  assert.throws(
    () =>
      runtime.createGroup({
        groupId: original.groupId,
        topicId: "topic-changed",
        partitionCount: 3,
      }),
    (error) => assertRuntimeCode(error, "mailbox_consumer_group_id_conflict"),
  );
  assert.equal(runtime.snapshot().groups[0]?.topicId, "topic-original");
  assert.equal(runtime.snapshot().groups[0]?.partitionCount, 2);
});

test("e03.mailbox consumer group commits a first rebalance", () => {
  const { runtime, group, member } = groupWithMember("first-rebalance", 3, 3);
  const planned = runtime.planRebalance({
    groupId: group.groupId,
    expectedRevision: group.revision,
  });
  assert.equal(planned.plan.state, "ready");
  assert.equal(planned.plan.assignments.length, 1);
  assert.deepEqual(planned.plan.assignments[0]?.partitions, [0, 1, 2]);
  const committed = runtime.commitRebalance({
    groupId: group.groupId,
    expectedGroupRevision: planned.group.revision,
    planId: planned.plan.planId,
    expectedPlanRevision: planned.plan.revision,
  });
  assert.equal(committed.group.state, "stable");
  assert.equal(committed.group.generation, 2);
  assert.equal(committed.plan.state, "committed");
  assert.equal(committed.members[0]?.memberId, member.memberId);
  assert.deepEqual(committed.members[0]?.assignedPartitions, [0, 1, 2]);
  assert.equal(committed.members[0]?.state, "active");
});

test("e03.mailbox consumer group rejects insufficient capacity", () => {
  const { runtime, group } = groupWithMember("capacity-fail", 4, 2);
  assert.throws(
    () =>
      runtime.planRebalance({
        groupId: group.groupId,
        expectedRevision: group.revision,
      }),
    (error) =>
      assertRuntimeCode(error, "mailbox_rebalance_capacity_insufficient"),
  );
  const snapshot = runtime.snapshot();
  assert.equal(snapshot.plans.length, 0);
  assert.equal(snapshot.groups[0]?.state, "stable");
  assert.equal(snapshot.members[0]?.state, "joining");
});

test("e03.mailbox consumer group rejects stale heartbeat", () => {
  const { runtime, member } = groupWithMember("stale-heartbeat");
  const renewed = runtime.heartbeat({
    memberId: member.memberId,
    expectedRevision: member.revision,
    generation: member.generation,
    leaseMs: 120_000,
  });
  assert.equal(renewed.revision, member.revision + 1);
  assert.ok(renewed.leaseExpiresAt > member.leaseExpiresAt);
  assert.throws(
    () =>
      runtime.heartbeat({
        memberId: member.memberId,
        expectedRevision: member.revision,
        generation: member.generation,
        leaseMs: 120_000,
      }),
    (error) => assertRuntimeCode(error, "mailbox_group_member_stale_revision"),
  );
  assert.equal(runtime.snapshot().members[0]?.revision, renewed.revision);
});

test("e03.mailbox consumer group expires a lost member", () => {
  const { runtime, member } = groupWithMember("member-expired");
  const expired = runtime.expireMembers("2026-07-18T04:30:00.000Z");
  assert.equal(expired.length, 1);
  assert.equal(expired[0]?.memberId, member.memberId);
  assert.equal(expired[0]?.state, "expired");
  assert.deepEqual(expired[0]?.pendingRevocations, []);
  const rejoined = runtime.join({
    groupId: member.groupId,
    expectedRevision: runtime.snapshot().groups[0]!.revision,
    ownerId: member.ownerId,
    capacity: member.capacity,
    leaseMs: 60_000,
  });
  assert.notEqual(rejoined.member.memberId, member.memberId);
  assert.equal(rejoined.member.state, "joining");
  assert.equal(runtime.snapshot().members.length, 2);
});

test("e03.mailbox consumer group rejects stale leave generation", () => {
  const { runtime, member } = groupWithMember("stale-leave");
  assert.throws(
    () =>
      runtime.leave({
        memberId: member.memberId,
        expectedRevision: member.revision,
        generation: member.generation + 1,
      }),
    (error) =>
      assertRuntimeCode(error, "mailbox_group_member_leave_generation_stale"),
  );
  assert.equal(runtime.snapshot().members[0]?.state, "joining");
  assert.equal(runtime.snapshot().members[0]?.assignedPartitions.length, 0);
});

test("e03.mailbox consumer group leaves idempotently", () => {
  const { runtime, member } = groupWithMember("leave-idempotent");
  const left = runtime.leave({
    memberId: member.memberId,
    expectedRevision: member.revision,
    generation: member.generation,
  });
  assert.equal(left.state, "left");
  assert.equal(left.revision, member.revision + 1);
  assert.deepEqual(left.assignedPartitions, []);
  assert.deepEqual(left.pendingRevocations, []);
  const replay = runtime.leave({
    memberId: left.memberId,
    expectedRevision: left.revision,
    generation: left.generation,
  });
  assert.equal(replay.digest, left.digest);
  assert.equal(replay.revision, left.revision);
});

test("e03.mailbox consumer group aborts an uncommitted rebalance", () => {
  const { runtime, group } = groupWithMember("abort-plan", 2, 2);
  const planned = runtime.planRebalance({
    groupId: group.groupId,
    expectedRevision: group.revision,
  });
  assert.equal(planned.plan.state, "ready");
  const aborted = runtime.abortRebalance({
    groupId: group.groupId,
    expectedGroupRevision: planned.group.revision,
    planId: planned.plan.planId,
    expectedPlanRevision: planned.plan.revision,
    reason: "operator-superseded-plan",
  });
  assert.equal(aborted.plan.state, "aborted");
  assert.equal(aborted.plan.abortReason, "operator-superseded-plan");
  assert.equal(aborted.group.state, "stable");
  assert.equal(aborted.group.activePlanId, "");
  assert.equal(aborted.group.generation, group.generation);
});

test("e03.mailbox consumer group rejects abort without reason", () => {
  const { runtime, group } = groupWithMember("abort-no-reason", 2, 2);
  const planned = runtime.planRebalance({
    groupId: group.groupId,
    expectedRevision: group.revision,
  });
  assert.throws(
    () =>
      runtime.abortRebalance({
        groupId: group.groupId,
        expectedGroupRevision: planned.group.revision,
        planId: planned.plan.planId,
        expectedPlanRevision: planned.plan.revision,
        reason: "",
      }),
    (error) =>
      assertRuntimeCode(error, "mailbox_rebalance_abort_reason_required"),
  );
  assert.equal(runtime.snapshot().plans[0]?.state, "ready");
  assert.equal(runtime.snapshot().groups[0]?.state, "assigning");
});

test("e03.mailbox consumer group rejects abort after commit", () => {
  const { runtime, group } = groupWithMember("abort-committed", 2, 2);
  const planned = runtime.planRebalance({
    groupId: group.groupId,
    expectedRevision: group.revision,
  });
  const committed = runtime.commitRebalance({
    groupId: group.groupId,
    expectedGroupRevision: planned.group.revision,
    planId: planned.plan.planId,
    expectedPlanRevision: planned.plan.revision,
  });
  assert.throws(
    () =>
      runtime.abortRebalance({
        groupId: committed.group.groupId,
        expectedGroupRevision: committed.group.revision,
        planId: committed.plan.planId,
        expectedPlanRevision: committed.plan.revision,
        reason: "too-late",
      }),
    (error) => assertRuntimeCode(error, "mailbox_rebalance_abort_committed"),
  );
  assert.equal(runtime.snapshot().plans[0]?.state, "committed");
});

test("e03.mailbox consumer group restores stable assignment", () => {
  const { runtime, group } = groupWithMember("restore-assignment", 2, 2);
  const planned = runtime.planRebalance({
    groupId: group.groupId,
    expectedRevision: group.revision,
  });
  const committed = runtime.commitRebalance({
    groupId: group.groupId,
    expectedGroupRevision: planned.group.revision,
    planId: planned.plan.planId,
    expectedPlanRevision: planned.plan.revision,
  });
  const snapshot = runtime.snapshot();
  const restored = new MailboxConsumerGroupRuntime(
    new TestClock("2026-07-18T05:00:00.000Z"),
  );
  restored.restore(snapshot);
  assert.deepEqual(restored.snapshot(), snapshot);
  assert.equal(restored.snapshot().groups[0]?.generation, 2);
  assert.equal(restored.snapshot().groups[0]?.state, "stable");
  assert.deepEqual(restored.snapshot().members[0]?.assignedPartitions, [0, 1]);
  assert.equal(restored.snapshot().plans[0]?.state, "committed");
  assert.equal(committed.group.digest, restored.snapshot().groups[0]?.digest);
});

test("e03.mailbox consumer group rejects corrupt restore", () => {
  const { runtime } = groupWithMember("corrupt-restore");
  const snapshot = runtime.snapshot();
  const tampered = {
    ...snapshot,
    activeMemberIdByOwner: [["orphan", "missing-member"]] as Array<
      [string, string]
    >,
  };
  const restored = new MailboxConsumerGroupRuntime();
  assert.throws(
    () => restored.restore(tampered),
    (error) =>
      assertRuntimeCode(error, "mailbox_consumer_group_snapshot_corrupt"),
  );
  assert.equal(restored.snapshot().groups.length, 0);
  assert.equal(restored.snapshot().members.length, 0);
});
