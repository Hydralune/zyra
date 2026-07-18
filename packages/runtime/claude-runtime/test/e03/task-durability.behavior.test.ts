import assert from "node:assert/strict";
import { test } from "node:test";
import { AgentExecutionRuntime } from "../../src/agents/execution-runtime.ts";
import {
  digest,
  E03RuntimeError,
  response,
  sealTask,
} from "../../src/e03/contracts.ts";
import {
  TaskIdentityRuntime,
  TaskLeaseRuntime,
} from "../../src/tasks/identity-runtime.ts";
import {
  DurableTaskRegistry,
  TaskInvariantRuntime,
} from "../../src/tasks/registry.ts";
import { TaskDeadlineRuntime, TaskExecutor } from "../../src/tasks/executor.ts";
import { TaskStateMachine } from "../../src/tasks/state-machine.ts";
import {
  commitTask,
  context,
  definition,
  registry,
  runInput,
  scope,
  successfulRunResult,
  task,
  TestClock,
  TestPhysicalPort,
} from "./fixtures.ts";

const WRITER = "typescript.E03AgentControlCoordinator";

function assertCode(error: unknown, code: string): boolean {
  assert.ok(error instanceof E03RuntimeError);
  assert.equal(error.code, code);
  assert.ok(error.message.trim());
  return true;
}

function transitionInput(label: string, revision: number) {
  return {
    requestId: `request-${label}-${revision}`,
    idempotencyKey: `key-${label}-${revision}`,
    writerId: WRITER,
    expectedRevision: revision,
    eventType: `event_${label}`,
  };
}

function executionHarness(label: string) {
  const clock = new TestClock("2026-07-18T06:00:00.000Z");
  const port = new TestPhysicalPort(clock);
  const durable = new DurableTaskRegistry(
    awaitEmptySnapshot()(clock),
    port,
    clock,
  );
  let mode: "success" | "failure" | "pending" = "success";
  let release: (() => void) | null = null;
  const runtime = new AgentExecutionRuntime(
    durable,
    {
      runChild: async () => {
        if (mode === "failure")
          return {
            ...successfulRunResult({ label, failed: true }),
            ok: false,
            stoppedReason: "test-child-failed",
          };
        if (mode === "pending")
          await new Promise<void>((resolve) => {
            release = resolve;
          });
        return successfulRunResult({ label, completed: true });
      },
      abortChild: async () => {
        release?.();
      },
    },
    clock,
  );
  return {
    clock,
    port,
    durable,
    runtime,
    setMode(next: "success" | "failure" | "pending") {
      mode = next;
    },
    release() {
      release?.();
    },
  };
}

function awaitEmptySnapshot() {
  return (clock: TestClock) => {
    const now = clock.now();
    const payload = {
      schemaVersion: "3.0" as const,
      revision: 0,
      tasks: {},
      requests: {},
      effects: {},
      definitions: {},
      writerLeases: {},
      createdAt: now,
      updatedAt: now,
    };
    return { ...payload, checksum: digest(payload) };
  };
}

async function createRuntimeTask(
  runtime: AgentExecutionRuntime,
  label: string,
) {
  const capabilityScope = scope();
  return runtime.create({
    runId: "run-e03",
    sessionId: `session-${label}`,
    parentTaskId: "parent-task",
    parentSessionId: "parent-session",
    taskId: label,
    idempotencyKey: `create-${label}`,
    definition: definition(`agent-${label}`, {
      budget: {
        ...definition(`agent-${label}-budget`).budget,
        startedAt: "2026-07-18T05:00:00.000Z",
        deadlineAt: "2026-07-19T05:00:00.000Z",
      },
    }),
    scope: capabilityScope,
    context: context("parent-task", "parent-session", capabilityScope),
    prompt: `Execute ${label}`,
    executionMode: "foreground",
  });
}

test("e03.task identity derives deterministic task id", () => {
  const identities = new TaskIdentityRuntime();
  const input = {
    runId: "run-identity",
    sessionId: "session-identity",
    parentTaskId: "parent-identity",
    parentSessionId: "parent-session-identity",
    parentLineage: ["root-identity"],
    idempotencyKey: "identity-deterministic",
  };
  const first = identities.allocate(input);
  const second = identities.allocate(input);
  assert.equal(first.taskId, second.taskId);
  assert.match(first.taskId, /^agent-[a-f0-9]{24}$/);
  assert.equal(first.attempt, 1);
  assert.equal(first.attemptId, `${first.taskId}:attempt:1`);
  assert.deepEqual(first.lineage, ["root-identity", "parent-identity"]);
  assert.notEqual(first.leaseId, second.leaseId);
  identities.validate(first);
  identities.validate(second);
});

test("e03.task identity preserves an explicit lease", () => {
  const identities = new TaskIdentityRuntime();
  const identity = identities.allocate({
    runId: "run-explicit",
    sessionId: "session-explicit",
    taskId: "task-explicit",
    parentTaskId: "parent-explicit",
    parentSessionId: "parent-session-explicit",
    attempt: 3,
    leaseId: "lease-explicit",
    idempotencyKey: "identity-explicit",
  });
  assert.equal(identity.taskId, "task-explicit");
  assert.equal(identity.attempt, 3);
  assert.equal(identity.attemptId, "task-explicit:attempt:3");
  assert.equal(identity.leaseId, "lease-explicit");
  assert.deepEqual(identity.lineage, ["parent-explicit"]);
  identities.validate(identity);
});

test("e03.task identity rejects invalid attempt", () => {
  const identities = new TaskIdentityRuntime();
  const allocate = (attempt: number) =>
    identities.allocate({
      runId: "run-invalid-attempt",
      sessionId: "session-invalid-attempt",
      taskId: "task-invalid-attempt",
      parentTaskId: "parent-invalid-attempt",
      parentSessionId: "parent-session-invalid-attempt",
      attempt,
      idempotencyKey: `attempt-${attempt}`,
    });
  assert.throws(
    () => allocate(0),
    (error) => assertCode(error, "invalid_attempt"),
  );
  assert.throws(
    () => allocate(-1),
    (error) => assertCode(error, "invalid_attempt"),
  );
  assert.throws(
    () => allocate(1.5),
    (error) => assertCode(error, "invalid_attempt"),
  );
});

test("e03.task identity rejects lineage cycle", () => {
  const identities = new TaskIdentityRuntime();
  assert.throws(
    () =>
      identities.allocate({
        runId: "run-cycle",
        sessionId: "session-cycle",
        taskId: "task-cycle",
        parentTaskId: "parent-cycle",
        parentSessionId: "parent-session-cycle",
        parentLineage: ["root-cycle", "task-cycle"],
        idempotencyKey: "identity-cycle",
      }),
    (error) => assertCode(error, "task_lineage_cycle"),
  );
  assert.throws(
    () =>
      identities.allocate({
        runId: "run-cycle",
        sessionId: "session-cycle",
        taskId: "task-cycle-2",
        parentTaskId: "parent-cycle",
        parentSessionId: "parent-session-cycle",
        parentLineage: ["root-cycle", "parent-cycle"],
        idempotencyKey: "identity-cycle-2",
      }),
    (error) => assertCode(error, "task_lineage_cycle"),
  );
});

test("e03.task identity rotates attempt and lease", () => {
  const identities = new TaskIdentityRuntime();
  const original = task("identity-rotate").identity;
  const rotated = identities.nextAttempt(original, "lease-next-attempt");
  assert.equal(rotated.taskId, original.taskId);
  assert.equal(rotated.sessionId, original.sessionId);
  assert.equal(rotated.parentTaskId, original.parentTaskId);
  assert.deepEqual(rotated.lineage, original.lineage);
  assert.equal(rotated.attempt, original.attempt + 1);
  assert.equal(rotated.attemptId, `${original.taskId}:attempt:2`);
  assert.equal(rotated.leaseId, "lease-next-attempt");
  assert.notEqual(rotated.leaseId, original.leaseId);
  identities.validate(rotated);
});

test("e03.task identity rejects tampered attempt identity", () => {
  const identities = new TaskIdentityRuntime();
  const original = task("identity-tamper").identity;
  assert.throws(
    () =>
      identities.validate({
        ...original,
        attemptId: `${original.taskId}:attempt:999`,
      }),
    (error) => assertCode(error, "attempt_identity_mismatch"),
  );
  assert.throws(
    () =>
      identities.validate({
        ...original,
        lineage: ["another-parent"],
      }),
    (error) => assertCode(error, "task_parent_mismatch"),
  );
});

test("e03.task lease accepts active matching custody", () => {
  const clock = new TestClock("2026-07-18T07:00:00.000Z");
  const leases = new TaskLeaseRuntime(clock);
  const identity = task("lease-active").identity;
  const lease = leases.acquire(identity, 60_000);
  const decision = leases.validateMutation(
    lease,
    identity,
    "2026-07-18T07:00:30.000Z",
  );
  assert.equal(lease.owner, WRITER);
  assert.equal(lease.state, "active");
  assert.equal(lease.taskId, identity.taskId);
  assert.equal(lease.attemptId, identity.attemptId);
  assert.equal(decision.accepted, true);
  assert.equal(decision.code, "lease_active");
  assert.equal(decision.current.digest, lease.digest);
  leases.assertMutation(lease, identity, "2026-07-18T07:00:30.000Z");
});

test("e03.task lease rejects stale lease", () => {
  const clock = new TestClock("2026-07-18T07:05:00.000Z");
  const leases = new TaskLeaseRuntime(clock);
  const identity = task("lease-stale").identity;
  const lease = leases.acquire(identity, 60_000);
  const stale = { ...identity, leaseId: "another-lease" };
  const decision = leases.validateMutation(
    lease,
    stale,
    "2026-07-18T07:05:30.000Z",
  );
  assert.equal(decision.accepted, false);
  assert.equal(decision.code, "stale_lease");
  assert.equal(decision.candidateLeaseId, "another-lease");
  assert.throws(
    () => leases.assertMutation(lease, stale, "2026-07-18T07:05:30.000Z"),
    (error) => assertCode(error, "stale_lease"),
  );
});

test("e03.task lease rejects expired custody", () => {
  const clock = new TestClock("2026-07-18T07:10:00.000Z");
  const leases = new TaskLeaseRuntime(clock);
  const identity = task("lease-expired").identity;
  const lease = leases.acquire(identity, 1_000);
  const decision = leases.validateMutation(
    lease,
    identity,
    "2026-07-18T07:10:02.000Z",
  );
  assert.equal(decision.accepted, false);
  assert.equal(decision.code, "lease_expired");
  assert.equal(decision.current.leaseId, identity.leaseId);
  assert.throws(
    () => leases.assertMutation(lease, identity, "2026-07-18T07:10:02.000Z"),
    (error) => assertCode(error, "lease_expired"),
  );
});

test("e03.task state creates canonical initial state", () => {
  const value = task("state-created");
  assert.equal(value.status, "created");
  assert.equal(value.revision, 1);
  assert.equal(value.sequence, 1);
  assert.equal(value.prompt, "Execute state-created");
  assert.equal(value.promptDigest, digest(value.prompt));
  assert.equal(value.executionMode, "foreground");
  assert.deepEqual(value.messages, []);
  assert.deepEqual(value.deliveries, []);
  assert.deepEqual(value.transitions, []);
  assert.equal(value.result, null);
  assert.equal(value.terminalAt, null);
  assert.ok(value.checksum);
});

test("e03.task state transitions to completed with result", () => {
  const machine = new TaskStateMachine(
    new TestClock("2026-07-18T08:00:00.000Z"),
  );
  let value = task("state-complete");
  const queued = machine.transition(
    value,
    "queued",
    transitionInput("queued", value.revision),
  );
  value = queued.state;
  const running = machine.transition(
    value,
    "running",
    transitionInput("running", value.revision),
  );
  value = running.state;
  const completed = machine.transition(value, "completed", {
    ...transitionInput("completed", value.revision),
    result: { ok: true, evidence: "artifact-1" },
    usage: { turns: 2, tool_calls: 1 },
  });
  assert.equal(completed.state.status, "completed");
  assert.equal(completed.state.result?.ok, true);
  assert.equal(completed.state.usage.turns, 2);
  assert.equal(completed.state.transitions.length, 3);
  assert.equal(completed.transition.fromStatus, "running");
  assert.equal(completed.transition.toStatus, "completed");
  assert.ok(completed.state.terminalAt);
});

test("e03.task state rejects invalid transition", () => {
  const machine = new TaskStateMachine();
  const created = task("state-invalid-transition");
  assert.throws(
    () =>
      machine.transition(created, "completed", {
        ...transitionInput("invalid-completed", created.revision),
        result: { ok: true },
      }),
    (error) => assertCode(error, "invalid_task_transition"),
  );
  assert.equal(created.status, "created");
  assert.equal(created.revision, 1);
  assert.equal(created.transitions.length, 0);
});

test("e03.task state rejects missing terminal result", () => {
  const machine = new TaskStateMachine();
  const running = task("state-missing-result", { status: "running" });
  assert.throws(
    () =>
      machine.transition(
        running,
        "completed",
        transitionInput("missing-result", running.revision),
      ),
    (error) => assertCode(error, "missing_task_result"),
  );
  assert.equal(running.status, "running");
  assert.equal(running.result, null);
  assert.equal(running.terminalAt, null);
});

test("e03.task state rejects missing failure reason", () => {
  const machine = new TaskStateMachine();
  const running = task("state-missing-reason", { status: "running" });
  assert.throws(
    () =>
      machine.transition(
        running,
        "failed",
        transitionInput("missing-failure-reason", running.revision),
      ),
    (error) => assertCode(error, "missing_terminal_reason"),
  );
  assert.equal(running.status, "running");
  assert.equal(running.error, "");
  assert.equal(running.terminalAt, null);
});

test("e03.task state rejects stale transition revision", () => {
  const machine = new TaskStateMachine();
  const created = task("state-stale-revision");
  assert.throws(
    () =>
      machine.transition(created, "queued", {
        ...transitionInput("stale-queued", created.revision - 1),
        expectedRevision: created.revision - 1,
      }),
    (error) => assertCode(error, "stale_revision"),
  );
  assert.equal(created.revision, 1);
  assert.equal(created.status, "created");
});

test("e03.task state cancellation is idempotent", () => {
  const machine = new TaskStateMachine();
  const created = task("state-cancel");
  const cancelled = machine.cancel(created, {
    requestId: "cancel-request",
    idempotencyKey: "cancel-key",
    writerId: WRITER,
    expectedRevision: created.revision,
    reason: "operator-cancelled",
  });
  const replay = machine.cancel(cancelled.state, {
    requestId: "cancel-request-replay",
    idempotencyKey: "cancel-key-replay",
    writerId: WRITER,
    expectedRevision: cancelled.state.revision,
    reason: "operator-cancelled",
  });
  assert.equal(cancelled.state.status, "cancelled");
  assert.equal(cancelled.state.error, "operator-cancelled");
  assert.equal(replay.state.checksum, cancelled.state.checksum);
  assert.equal(replay.transition.digest, cancelled.transition.digest);
});

test("e03.task state denies kill outside scope", () => {
  const machine = new TaskStateMachine();
  const denied = task("state-kill-denied", {
    scope: scope({ allowKill: false }),
  });
  assert.equal(denied.scope.allowKill, false);
  assert.throws(
    () =>
      machine.kill(denied, {
        requestId: "kill-denied-request",
        idempotencyKey: "kill-denied-key",
        writerId: WRITER,
        expectedRevision: denied.revision,
        reason: "force-stop",
      }),
    (error) => assertCode(error, "kill_not_permitted"),
  );
  assert.equal(denied.status, "created");
  assert.equal(denied.error, "");
});

test("e03.task state rejects late result after kill", () => {
  const machine = new TaskStateMachine();
  const running = task("state-late-result", { status: "running" });
  const killed = machine.kill(running, {
    requestId: "late-kill-request",
    idempotencyKey: "late-kill-key",
    writerId: WRITER,
    expectedRevision: running.revision,
    reason: "lease-revoked",
  }).state;
  assert.equal(killed.status, "killed");
  assert.throws(
    () =>
      machine.rejectLateResult(
        killed,
        killed.revision,
        killed.identity.leaseId,
      ),
    (error) => assertCode(error, "late_terminal_result"),
  );
  assert.equal(killed.result, null);
  assert.equal(killed.error, "lease-revoked");
});

test("e03.task registry commits and indexes a task", async () => {
  const { registry: durable, port } = registry();
  const value = task("registry-commit-behavior");
  const committed = await commitTask(durable, value, "registry-commit-key");
  assert.equal(committed.identity.taskId, value.identity.taskId);
  assert.equal(committed.status, "created");
  assert.notEqual(committed.checksum, value.checksum);
  assert.equal(
    durable.require(value.identity.taskId).checksum,
    committed.checksum,
  );
  assert.equal(durable.snapshot().revision, 1);
  assert.equal(Object.keys(durable.snapshot().tasks).length, 1);
  assert.equal(port.snapshot?.revision, 1);
  assert.equal(durable.index.byStatus("created").length, 1);
  assert.equal(durable.journal.snapshot().length > 0, true);
});

test("e03.task registry replays prepared mutation", () => {
  const { registry: durable } = registry();
  const value = task("registry-prepare-replay");
  const proposal = {
    requestId: "prepare-replay-request",
    idempotencyKey: "prepare-replay-key",
    writerId: WRITER,
    taskId: value.identity.taskId,
    expectedRevision: 0,
    proposed: value,
  };
  const first = durable.prepare(proposal);
  const replay = durable.prepare(proposal);
  assert.equal(replay.preparedDigest, first.preparedDigest);
  assert.equal(replay.transition.digest, first.transition.digest);
  assert.equal(replay.proposed.checksum, first.proposed.checksum);
  assert.equal(replay.before, null);
  assert.equal(durable.snapshot().revision, 0);
});

test("e03.task registry rejects idempotency conflict", () => {
  const { registry: durable } = registry();
  const first = task("registry-idempotency-first");
  const second = task("registry-idempotency-second");
  durable.prepare({
    requestId: "idempotency-request",
    idempotencyKey: "shared-idempotency-key",
    writerId: WRITER,
    taskId: first.identity.taskId,
    expectedRevision: 0,
    proposed: first,
  });
  assert.throws(
    () =>
      durable.prepare({
        requestId: "changed-request",
        idempotencyKey: "shared-idempotency-key",
        writerId: WRITER,
        taskId: second.identity.taskId,
        expectedRevision: 0,
        proposed: second,
      }),
    (error) => assertCode(error, "idempotency_conflict"),
  );
  assert.equal(durable.snapshot().revision, 0);
});

test("e03.task registry rejects physical effect", async () => {
  const clock = new TestClock("2026-07-18T09:00:00.000Z");
  const port = new TestPhysicalPort(clock);
  const durable = new DurableTaskRegistry(
    awaitEmptySnapshot()(clock),
    port,
    clock,
  );
  const value = task("registry-effect-rejected");
  durable.prepare({
    requestId: "effect-rejected-request",
    idempotencyKey: "effect-rejected-key",
    writerId: WRITER,
    taskId: value.identity.taskId,
    expectedRevision: 0,
    proposed: value,
    effectKind: "persist",
    effectOperation: "persist_rejected_task",
    effectPayload: { task_id: value.identity.taskId },
  });
  port.rejectNextEffect = true;
  await assert.rejects(durable.recordReceipt("effect-rejected-key"), (error) =>
    assertCode(error, "physical_effect_rejected"),
  );
  assert.equal(port.effects.length, 1);
  assert.equal(port.receipts[0]?.accepted, false);
  assert.equal(durable.snapshot().revision, 0);
});

test("e03.task registry rejects stale CAS commit", async () => {
  const clock = new TestClock("2026-07-18T09:05:00.000Z");
  const port = new TestPhysicalPort(clock);
  const durable = new DurableTaskRegistry(
    awaitEmptySnapshot()(clock),
    port,
    clock,
  );
  const value = task("registry-cas-stale");
  durable.prepare({
    requestId: "cas-stale-request",
    idempotencyKey: "cas-stale-key",
    writerId: WRITER,
    taskId: value.identity.taskId,
    expectedRevision: 0,
    proposed: value,
  });
  port.rejectNextCas = true;
  await assert.rejects(durable.commit("cas-stale-key", null), (error) =>
    assertCode(error, "stale_registry_revision"),
  );
  assert.equal(durable.snapshot().revision, 0);
  assert.equal(port.snapshot, null);
  assert.equal(Object.keys(durable.snapshot().tasks).length, 0);
});

test("e03.task registry recovers lost ACK", async () => {
  const { registry: durable } = registry();
  const value = task("registry-lost-ack-behavior");
  await commitTask(durable, value, "lost-ack-key");
  const acknowledged = durable.acknowledge(
    "lost-ack-key",
    response({
      ok: true,
      request_id: "lost-ack-key",
      command: "agent.create",
      phase: "commit",
      revision: 1,
      state: { task_id: value.identity.taskId },
    }),
  );
  const replay = durable.recoverLostAck("lost-ack-key");
  assert.equal(acknowledged.replayed, false);
  assert.equal(replay?.replayed, true);
  assert.equal(replay?.ok, true);
  assert.equal(replay?.request_id, "lost-ack-key");
  assert.equal(replay?.state?.task_id, value.identity.taskId);
  assert.equal(durable.snapshot().requests["lost-ack-key"]?.phase, "ack");
});

test("e03.task invariant rejects checksum tamper", () => {
  const invariants = new TaskInvariantRuntime();
  const value = task("invariant-checksum-tamper");
  const tampered = { ...value, prompt: "tampered prompt" };
  assert.throws(
    () => invariants.assertTask(tampered),
    (error) => assertCode(error, "task_checksum_mismatch"),
  );
  assert.equal(value.prompt, "Execute invariant-checksum-tamper");
  assert.notEqual(value.checksum, digest(tampered));
});

test("e03.task invariant rejects transition writer tamper", () => {
  const invariants = new TaskInvariantRuntime();
  const queued = task("invariant-writer-tamper", { status: "queued" });
  const transition = queued.transitions[0]!;
  const unsigned = {
    ...transition,
    writerId: "python.AgentToolRuntime",
  };
  const { digest: _, ...payload } = unsigned;
  const altered = { ...payload, digest: digest(payload) };
  const resealed = sealTask({
    ...queued,
    transitions: [altered],
    checksum: "",
  });
  assert.throws(
    () => invariants.assertTask(resealed),
    (error) => assertCode(error, "transition_writer_mismatch"),
  );
  assert.equal(queued.transitions[0]?.writerId, WRITER);
});

test("e03.task deadline reports healthy budget", () => {
  const clock = new TestClock("2026-07-18T10:00:00.000Z");
  const deadlines = new TaskDeadlineRuntime(clock);
  const value = task("deadline-healthy", {
    definition: definition("deadline-healthy", {
      budget: {
        ...definition("deadline-template").budget,
        startedAt: "2026-07-18T09:00:00.000Z",
        deadlineAt: "2026-07-18T12:00:00.000Z",
      },
    }),
  });
  const assessment = deadlines.assess(value);
  assert.equal(assessment.disposition, "healthy");
  assert.equal(assessment.shouldCancel, false);
  assert.equal(assessment.reason, "within_deadline");
  assert.equal(assessment.startedAt, "2026-07-18T09:00:00.000Z");
  assert.equal(assessment.deadlineAt, "2026-07-18T12:00:00.000Z");
  assert.ok(assessment.remainingMs > 0);
  deadlines.assertDispatchable(value);
});

test("e03.task deadline rejects expired task", () => {
  const clock = new TestClock("2026-07-18T13:00:00.000Z");
  const deadlines = new TaskDeadlineRuntime(clock);
  const value = task("deadline-expired", {
    definition: definition("deadline-expired", {
      budget: {
        ...definition("deadline-expired-template").budget,
        startedAt: "2026-07-18T09:00:00.000Z",
        deadlineAt: "2026-07-18T12:00:00.000Z",
      },
    }),
  });
  const assessment = deadlines.assess(value);
  assert.equal(assessment.disposition, "expired");
  assert.equal(assessment.shouldCancel, true);
  assert.equal(assessment.reason, "wall_time_exhausted");
  assert.equal(assessment.remainingMs, 0);
  assert.throws(
    () => deadlines.assertDispatchable(value),
    (error) => assertCode(error, "wall_time_exhausted"),
  );
});

test("e03.task deadline rejects invalid deadline", () => {
  const deadlines = new TaskDeadlineRuntime(
    new TestClock("2026-07-18T11:00:00.000Z"),
  );
  const value = task("deadline-invalid", {
    definition: definition("deadline-invalid", {
      budget: {
        ...definition("deadline-invalid-template").budget,
        startedAt: "2026-07-18T12:00:00.000Z",
        deadlineAt: "2026-07-18T11:00:00.000Z",
      },
    }),
  });
  const assessment = deadlines.assess(value);
  assert.equal(assessment.disposition, "invalid");
  assert.equal(assessment.shouldCancel, true);
  assert.equal(assessment.reason, "invalid_deadline");
  assert.throws(
    () => deadlines.assertDispatchable(value),
    (error) => assertCode(error, "invalid_deadline"),
  );
});

test("e03.task executor completes and persists transitions", async () => {
  const clock = new TestClock("2026-07-18T12:00:00.000Z");
  const { registry: durable } = registry(clock, new TestPhysicalPort(clock));
  const created = await commitTask(durable, task("executor-complete"));
  const executor = new TaskExecutor(
    durable,
    new TaskStateMachine(clock),
    {
      runChild: async () => successfulRunResult({ executor: "complete" }),
    },
    clock,
  );
  const completed = await executor.dispatch({
    requestId: "executor-complete-request",
    idempotencyKey: "executor-complete-key",
    writerId: WRITER,
    task: created,
    parentInput: runInput(),
    argumentsValue: { prompt: "complete" },
  });
  assert.equal(completed.status, "completed");
  assert.equal(completed.result?.ok, true);
  assert.deepEqual(completed.result?.session_snapshot, {
    executor: "complete",
  });
  assert.equal(durable.require(created.identity.taskId).status, "completed");
  assert.equal(
    completed.deliveries.filter((item) => item.kind === "final").length,
    1,
  );
  assert.equal(executor.result(created.identity.taskId).ok, true);
});

test("e03.task executor rejects terminal dispatch", async () => {
  const clock = new TestClock("2026-07-18T12:30:00.000Z");
  const completed = task("executor-terminal", { status: "completed" });
  const port = new TestPhysicalPort(clock);
  port.snapshot = {
    ...awaitEmptySnapshot()(clock),
    revision: 1,
    tasks: { [completed.identity.taskId]: completed },
    writerLeases: { [completed.identity.leaseId]: WRITER },
    checksum: "",
  };
  const { checksum: _, ...payload } = port.snapshot;
  port.snapshot = { ...payload, checksum: digest(payload) };
  const durable = new DurableTaskRegistry(port.snapshot, port, clock);
  const executor = new TaskExecutor(
    durable,
    new TaskStateMachine(clock),
    { runChild: async () => successfulRunResult() },
    clock,
  );
  await assert.rejects(
    executor.dispatch({
      requestId: "terminal-dispatch-request",
      idempotencyKey: "terminal-dispatch-key",
      writerId: WRITER,
      task: completed,
      parentInput: runInput(),
      argumentsValue: {},
    }),
    (error) => assertCode(error, "terminal_task"),
  );
  assert.equal(durable.require(completed.identity.taskId).status, "completed");
});

test("e03.agent execution completes end to end", async () => {
  const harness = executionHarness("agent-end-to-end");
  const created = await createRuntimeTask(harness.runtime, "agent-end-to-end");
  const completed = await harness.runtime.run(
    created.identity.taskId,
    runInput(),
    { prompt: "finish the delegated work" },
    "agent-run-request",
    "agent-run-key",
  );
  assert.equal(created.status, "created");
  assert.equal(completed.status, "completed");
  assert.ok(completed.revision > created.revision);
  assert.equal(completed.identity.attempt, 1);
  assert.equal(completed.result?.ok, true);
  assert.deepEqual(completed.result?.session_snapshot, {
    label: "agent-end-to-end",
    completed: true,
  });
  assert.equal(harness.port.effects.length >= 4, true);
  assert.equal(
    harness.durable.require(created.identity.taskId).checksum,
    completed.checksum,
  );
});

test("e03.agent execution resumes failed task with new lease", async () => {
  const harness = executionHarness("agent-resume");
  const created = await createRuntimeTask(harness.runtime, "agent-resume");
  harness.setMode("failure");
  const failed = await harness.runtime.run(
    created.identity.taskId,
    runInput(),
    { prompt: "fail first attempt" },
    "agent-fail-request",
    "agent-fail-key",
  );
  assert.equal(failed.status, "failed");
  assert.equal(failed.identity.attempt, 1);
  assert.equal(
    failed.deliveries.filter((item) => item.kind === "final").length,
    1,
  );
  harness.setMode("success");
  const resumed = await harness.runtime.resume(
    failed.identity.taskId,
    failed.revision,
    runInput(),
    { prompt: "resume second attempt" },
    "agent-resume-request",
    "agent-resume-key",
  );
  assert.equal(resumed.status, "completed");
  assert.equal(resumed.identity.attempt, 2);
  assert.notEqual(resumed.identity.leaseId, failed.identity.leaseId);
  assert.equal(
    resumed.deliveries.filter((item) => item.kind === "final").length,
    2,
  );
  assert.equal(
    harness.durable.require(resumed.identity.taskId).checksum,
    resumed.checksum,
  );
});

test("e03.agent execution rejects stale resume revision", async () => {
  const harness = executionHarness("agent-stale-resume");
  const created = await createRuntimeTask(
    harness.runtime,
    "agent-stale-resume",
  );
  harness.setMode("failure");
  const failed = await harness.runtime.run(
    created.identity.taskId,
    runInput(),
    {},
    "stale-resume-fail-request",
    "stale-resume-fail-key",
  );
  await assert.rejects(
    harness.runtime.resume(
      failed.identity.taskId,
      failed.revision - 1,
      runInput(),
      {},
      "stale-resume-request",
      "stale-resume-key",
    ),
    (error) => assertCode(error, "stale_revision"),
  );
  assert.equal(
    harness.durable.require(failed.identity.taskId).status,
    "failed",
  );
  assert.equal(
    harness.durable.require(failed.identity.taskId).identity.attempt,
    1,
  );
});
