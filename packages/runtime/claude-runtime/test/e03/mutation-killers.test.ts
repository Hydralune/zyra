import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { AgentDefinitionLoader } from "../../src/agents/definition-loader.ts";
import { AgentDefinitionRegistry } from "../../src/agents/definition-registry.ts";
import { AgentContextFork } from "../../src/agents/context-fork.ts";
import { AgentExecutionRuntime } from "../../src/agents/execution-runtime.ts";
import { AgentMemoryRuntime } from "../../src/agents/memory-runtime.ts";
import { AgentScopeLattice } from "../../src/agents/scope-lattice.ts";
import {
  digest,
  response,
  sealTask,
  type E03TaskState,
} from "../../src/e03/contracts.ts";
import { TaskExecutor } from "../../src/tasks/executor.ts";
import { TaskIdentityRuntime } from "../../src/tasks/identity-runtime.ts";
import { DurableTaskRegistry } from "../../src/tasks/registry.ts";
import { TaskStateMachine } from "../../src/tasks/state-machine.ts";
import { TeamFanout } from "../../src/team/fanout.ts";
import { TeamMailbox } from "../../src/team/mailbox.ts";
import {
  commitTask,
  context,
  definition,
  registry,
  runInput,
  scope,
  snapshotWithTask,
  successfulRunResult,
  task,
  TestClock,
  TestPhysicalPort,
} from "./fixtures.ts";

function creationInput(taskId: string) {
  const identity = new TaskIdentityRuntime().allocate({
    runId: "run-e03",
    sessionId: `session-${taskId}`,
    taskId,
    parentTaskId: "parent-task",
    parentSessionId: "parent-session",
    idempotencyKey: `identity-${taskId}`,
  });
  const capabilityScope = scope();
  return {
    identity,
    definition: definition(`definition-${taskId}`),
    scope: capabilityScope,
    context: context(taskId, identity.sessionId, capabilityScope),
    prompt: `Execute ${taskId}`,
    executionMode: "foreground" as const,
  };
}

async function preparedRegistry(
  taskId: string,
  options: { effect?: boolean; status?: E03TaskState["status"] } = {},
): Promise<{
  durable: DurableTaskRegistry;
  port: TestPhysicalPort;
  value: E03TaskState;
  key: string;
}> {
  const clock = new TestClock();
  const port = new TestPhysicalPort(clock);
  const durable = new DurableTaskRegistry(
    (await import("../../src/e03/contracts.ts")).emptySnapshot(clock),
    port,
    clock,
  );
  const value = task(taskId, { status: options.status ?? "created" });
  const key = `mutation-${taskId}`;
  durable.prepare({
    requestId: key,
    idempotencyKey: key,
    writerId: "typescript.E03AgentControlCoordinator",
    taskId,
    expectedRevision: 0,
    proposed: value,
    effectKind: options.effect ? "persist" : undefined,
    effectOperation: options.effect ? "persist-test-task" : undefined,
    effectPayload: options.effect ? { task_id: taskId } : undefined,
  });
  return { durable, port, value, key };
}

function executionHarness() {
  const clock = new TestClock();
  const port = new TestPhysicalPort(clock);
  const durable = new DurableTaskRegistry(
    requireSnapshotFactory()(clock),
    port,
    clock,
  );
  let mode: "success" | "failure" | "pending" = "success";
  let resolvePending:
    | ((value: ReturnType<typeof successfulRunResult>) => void)
    | null = null;
  const host = {
    runChild: async () => {
      if (mode === "failure") {
        return {
          ...successfulRunResult({ failure: true }),
          ok: false,
          stoppedReason: "child-failed",
        };
      }
      if (mode === "pending") {
        return new Promise<ReturnType<typeof successfulRunResult>>(
          (resolve) => {
            resolvePending = resolve;
          },
        );
      }
      return successfulRunResult({ completed: true });
    },
    abortChild: async () => undefined,
  };
  return {
    clock,
    port,
    durable,
    host,
    runtime: new AgentExecutionRuntime(durable, host, clock),
    setMode(value: "success" | "failure" | "pending") {
      mode = value;
    },
    settlePending() {
      resolvePending?.(successfulRunResult({ released: true }));
    },
  };
}

function requireSnapshotFactory(): typeof import("../../src/e03/contracts.ts").emptySnapshot {
  return ((clock: TestClock) => {
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
  }) as typeof import("../../src/e03/contracts.ts").emptySnapshot;
}

async function createAgent(
  runtime: AgentExecutionRuntime,
  taskId: string,
): Promise<E03TaskState> {
  const capabilityScope = scope();
  return runtime.create({
    runId: "run-e03",
    sessionId: `session-${taskId}`,
    parentTaskId: "parent-task",
    parentSessionId: "parent-session",
    taskId,
    idempotencyKey: `create-${taskId}`,
    definition: definition(`agent-${taskId}`),
    scope: capabilityScope,
    context: context("parent-task", "parent-session", capabilityScope),
    prompt: `Execute ${taskId}`,
    executionMode: "foreground",
  });
}

test("e03.mutation.definition-registry", () => {
  const registry = new AgentDefinitionRegistry();
  const registered = registry.register({
    name: "reviewer",
    description: "Reviews implementation evidence",
    source: "project",
    version: "1",
  });
  assert.equal(registered.name, "reviewer");
  assert.equal(registered.source, "project");
  assert.equal(registry.list("reviewer").length, 1);
  assert.equal(registry.generation().length, 64);
});

test("e03.mutation.definition-precedence", () => {
  const registry = new AgentDefinitionRegistry();
  registry.register({
    name: "analyst",
    description: "Built in analyst definition",
    source: "builtin",
    version: "1",
  });
  registry.register({
    name: "analyst",
    description: "Project analyst definition",
    source: "project",
    version: "2",
  });
  const selected = registry.resolve("analyst");
  assert.equal(selected.source, "project");
  assert.equal(selected.version, "2");
  assert.match(selected.description, /Project/);
});

test("e03.mutation.definition-load", async () => {
  const root = await mkdtemp(join(tmpdir(), "zyra-e03-definition-"));
  try {
    await writeFile(
      join(root, "worker.json"),
      JSON.stringify({
        name: "filesystem-worker",
        description: "Loaded from a controlled filesystem root",
        version: "1",
        tools: ["read"],
      }),
      "utf8",
    );
    const registry = new AgentDefinitionRegistry();
    const loader = new AgentDefinitionLoader(registry);
    const report = await loader.loadDirectory({
      root,
      source: "project",
      maximumDepth: 2,
      maximumFiles: 10,
    });
    assert.equal(report.errors.length, 0);
    assert.equal(report.loaded.length, 1);
    assert.equal(report.loaded[0]?.definition.name, "filesystem-worker");
    assert.equal(registry.resolve("filesystem-worker").source, "project");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("e03.mutation.definition-validation", () => {
  const registry = new AgentDefinitionRegistry();
  const loader = new AgentDefinitionLoader(registry);
  const validated = loader.validate(
    {
      name: "validated-worker",
      description: "Definition accepted by the loader boundary",
      version: "7",
      tools: ["read", "write"],
      metadata: { suite: "mutation" },
    },
    "user",
    "memory://validated-worker",
  );
  assert.equal(validated.name, "validated-worker");
  assert.equal(validated.version, "7");
  assert.equal(validated.source, "user");
  assert.equal(validated.metadata.definition_path, "memory://validated-worker");
});

test("e03.mutation.scope-derive", () => {
  const lattice = new AgentScopeLattice();
  const parent = scope({
    tools: ["read", "write", "shell"],
    deniedTools: ["shell"],
    maxDepth: 5,
    maxChildren: 6,
  });
  const child = lattice.derive({
    parent,
    definition: definition("scoped", {
      tools: ["read"],
      deniedTools: ["write"],
    }),
    requestedTools: ["read", "write"],
    requestedSkills: ["analysis"],
    requestedMcpServers: ["local"],
    requestedWorkspaceRoots: [process.cwd()],
    requestedIsolation: "workspace",
    depth: 1,
  });
  assert.deepEqual(child.tools, ["read"]);
  assert.ok(child.deniedTools.includes("write"));
  assert.equal(child.maxDepth, 4);
  assert.equal(child.permissionCeilingDigest, parent.permissionCeilingDigest);
});

test("e03.mutation.scope-ceiling", () => {
  const lattice = new AgentScopeLattice();
  const parent = scope({
    tools: ["read", "write"],
    deniedTools: ["shell"],
    maxDepth: 4,
  });
  const child = lattice.derive({
    parent,
    definition: definition("bounded", { tools: ["read"] }),
    requestedTools: ["read"],
    requestedIsolation: "workspace",
    depth: 2,
  });
  lattice.assertMonotonic(parent, child);
  assert.deepEqual(child.tools, ["read"]);
  assert.ok(child.deniedTools.includes("shell"));
  assert.ok(child.maxDepth <= parent.maxDepth);
});

test("e03.mutation.context-fork", () => {
  const capabilityScope = scope();
  const parent = context("parent-task", "parent-session", capabilityScope, {
    messageRefs: ["message-1"],
    artifactRefs: ["artifact-1"],
    memoryRefs: ["memory-1"],
  });
  const runtime = new AgentContextFork(new TestClock());
  const child = runtime.fork({
    parent,
    childTaskId: "child-task",
    childSessionId: "child-session",
    mode: "fork",
    scope: capabilityScope,
    messageRefs: ["message-2"],
    memoryRefs: ["memory-2"],
  });
  assert.equal(child.parentSnapshotId, parent.snapshotId);
  assert.deepEqual(child.messageRefs, ["message-1", "message-2"]);
  assert.deepEqual(child.memoryRefs, ["memory-1", "memory-2"]);
  assert.equal(child.permissionDigest, capabilityScope.permissionCeilingDigest);
});

test("e03.mutation.context-restore", () => {
  const capabilityScope = scope();
  const snapshot = context("restore-task", "restore-session", capabilityScope, {
    messageRefs: ["message-restore"],
    topologyRevision: 9,
  });
  const runtime = new AgentContextFork(new TestClock());
  const restored = runtime.restore(snapshot, {
    sessionId: "restore-session",
    taskId: "restore-task",
    permissionDigest: capabilityScope.permissionCeilingDigest,
  });
  assert.deepEqual(restored, snapshot);
  assert.notEqual(restored, snapshot);
  assert.equal(restored.topologyRevision, 9);
  assert.deepEqual(restored.messageRefs, ["message-restore"]);
});

test("e03.mutation.memory-capture", () => {
  const runtime = new AgentMemoryRuntime(
    "memory-task",
    "memory-session",
    new TestClock(),
  );
  const record = runtime.capture({
    kind: "decision",
    content: "Use the revision-fenced TypeScript owner.",
    importance: 0.9,
    sourceMessageIds: ["message-a", "message-a"],
    sourceArtifactIds: ["artifact-a"],
  });
  assert.equal(record.taskId, "memory-task");
  assert.equal(record.kind, "decision");
  assert.deepEqual(record.sourceMessageIds, ["message-a"]);
  assert.ok(record.tokenEstimate > 0);
  assert.equal(runtime.snapshot().records.length, 1);
});

test("e03.mutation.memory-restore", () => {
  const source = new AgentMemoryRuntime(
    "memory-task",
    "memory-session",
    new TestClock(),
  );
  source.capture({
    kind: "instruction",
    content: "Preserve the task lease across restore.",
    importance: 1,
  });
  const snapshot = source.snapshot();
  const restored = new AgentMemoryRuntime(
    "memory-task",
    "memory-session",
    new TestClock(),
  );
  restored.restore(snapshot, {
    taskId: "memory-task",
    sessionId: "memory-session",
  });
  assert.deepEqual(restored.snapshot(), snapshot);
  assert.equal(restored.snapshot().revision, 1);
  assert.equal(restored.snapshot().records[0]?.importance, 1);
});

test("e03.mutation.agent-create", async () => {
  const harness = executionHarness();
  const created = await createAgent(harness.runtime, "create-agent");
  assert.equal(created.identity.taskId, "create-agent");
  assert.equal(created.status, "created");
  assert.equal(created.revision, 1);
  assert.equal(harness.port.effects.length, 1);
  assert.equal(
    harness.durable.require("create-agent").checksum,
    created.checksum,
  );
});

test("e03.mutation.agent-run", async () => {
  const harness = executionHarness();
  const created = await createAgent(harness.runtime, "run-agent");
  const completed = await harness.runtime.run(
    created.identity.taskId,
    runInput(),
    { prompt: "complete" },
    "run-agent-request",
    "run-agent-key",
  );
  assert.equal(completed.status, "completed");
  assert.ok(completed.revision > created.revision);
  assert.equal(completed.result?.ok, true);
  assert.equal(harness.durable.require("run-agent").status, "completed");
});

test("e03.mutation.agent-resume", async () => {
  const harness = executionHarness();
  const created = await createAgent(harness.runtime, "resume-agent");
  harness.setMode("failure");
  const failed = await harness.runtime.run(
    created.identity.taskId,
    runInput(),
    { prompt: "fail first" },
    "resume-first-request",
    "resume-first-key",
  );
  assert.equal(failed.status, "failed");
  harness.setMode("success");
  const resumed = await harness.runtime.resume(
    failed.identity.taskId,
    failed.revision,
    runInput(),
    { prompt: "resume" },
    "resume-second-request",
    "resume-second-key",
  );
  assert.equal(resumed.status, "completed");
  assert.equal(resumed.identity.attempt, 2);
  assert.notEqual(resumed.identity.leaseId, failed.identity.leaseId);
});

test("e03.mutation.agent-abort", async () => {
  const harness = executionHarness();
  const created = await createAgent(harness.runtime, "abort-agent");
  const cancelled = await harness.runtime.abort(
    created.identity.taskId,
    created.revision,
    "operator-cancelled",
    "cancel",
    "abort-agent-request",
    "abort-agent-key",
  );
  assert.equal(cancelled.status, "cancelled");
  assert.equal(cancelled.error, "operator-cancelled");
  assert.equal(cancelled.revision, created.revision + 1);
  assert.equal(harness.durable.require("abort-agent").status, "cancelled");
});

test("e03.mutation.task-identity", () => {
  const runtime = new TaskIdentityRuntime();
  const identity = runtime.allocate({
    runId: "identity-run",
    sessionId: "identity-session",
    taskId: "identity-task",
    parentTaskId: "identity-parent",
    parentSessionId: "identity-parent-session",
    parentLineage: ["root"],
    idempotencyKey: "identity-key",
  });
  assert.equal(identity.taskId, "identity-task");
  assert.equal(identity.attempt, 1);
  assert.equal(identity.attemptId, "identity-task:attempt:1");
  assert.deepEqual(identity.lineage, ["root", "identity-parent"]);
});

test("e03.mutation.task-attempt", () => {
  const runtime = new TaskIdentityRuntime();
  const first = runtime.allocate({
    runId: "attempt-run",
    sessionId: "attempt-session",
    taskId: "attempt-task",
    parentTaskId: "attempt-parent",
    parentSessionId: "attempt-parent-session",
    idempotencyKey: "attempt-key",
  });
  const second = runtime.nextAttempt(first);
  assert.equal(second.attempt, 2);
  assert.equal(second.attemptId, "attempt-task:attempt:2");
  assert.notEqual(second.leaseId, first.leaseId);
  assert.deepEqual(second.lineage, first.lineage);
});

test("e03.mutation.task-create", () => {
  const machine = new TaskStateMachine(new TestClock());
  const created = machine.create(creationInput("machine-create"));
  assert.equal(created.identity.taskId, "machine-create");
  assert.equal(created.status, "created");
  assert.equal(created.revision, 1);
  assert.equal(created.sequence, 1);
  assert.equal(created.promptDigest, digest("Execute machine-create"));
});

test("e03.mutation.task-transition", () => {
  const machine = new TaskStateMachine(new TestClock());
  const created = machine.create(creationInput("machine-transition"));
  const queued = machine.transition(created, "queued", {
    requestId: "transition-request",
    idempotencyKey: "transition-key",
    writerId: "transition-writer",
    expectedRevision: created.revision,
    eventType: "agent_task_queued",
  });
  assert.equal(queued.state.status, "queued");
  assert.equal(queued.state.revision, 2);
  assert.equal(queued.transition.fromRevision, 1);
  assert.equal(queued.transition.toRevision, 2);
  assert.equal(queued.transition.toStatus, "queued");
});

test("e03.mutation.task-cancel", () => {
  const machine = new TaskStateMachine(new TestClock());
  const created = machine.create(creationInput("machine-cancel"));
  const cancelled = machine.cancel(created, {
    requestId: "cancel-request",
    idempotencyKey: "cancel-key",
    writerId: "cancel-writer",
    expectedRevision: created.revision,
    reason: "cancelled-by-test",
  });
  assert.equal(cancelled.state.status, "cancelled");
  assert.equal(cancelled.state.error, "cancelled-by-test");
  assert.equal(cancelled.transition.eventType, "agent_task_cancelled");
  assert.ok(cancelled.state.terminalAt);
});

test("e03.mutation.task-kill", () => {
  const machine = new TaskStateMachine(new TestClock());
  const created = machine.create(creationInput("machine-kill"));
  const killed = machine.kill(created, {
    requestId: "kill-request",
    idempotencyKey: "kill-key",
    writerId: "kill-writer",
    expectedRevision: created.revision,
    reason: "killed-by-test",
  });
  assert.equal(killed.state.status, "killed");
  assert.equal(killed.state.error, "killed-by-test");
  assert.equal(killed.transition.eventType, "agent_task_killed");
  assert.ok(killed.state.terminalAt);
});

test("e03.mutation.late-result-fence", () => {
  const machine = new TaskStateMachine(new TestClock());
  const running = task("late-result-running", { status: "running" });
  machine.rejectLateResult(running, running.revision, running.identity.leaseId);
  assert.equal(running.status, "running");
  assert.equal(running.revision, 3);
  assert.ok(running.identity.leaseId.length > 0);
  assert.equal(running.transitions.at(-1)?.toStatus, "running");
});

test("e03.mutation.task-prepare", async () => {
  const { durable, value, key } = await preparedRegistry("registry-prepare");
  const prepared = durable.prepare({
    requestId: key,
    idempotencyKey: key,
    writerId: "typescript.E03AgentControlCoordinator",
    taskId: value.identity.taskId,
    expectedRevision: 0,
    proposed: value,
  });
  assert.equal(prepared.proposed.checksum, value.checksum);
  assert.equal(prepared.transition.phase, "prepare");
  assert.equal(prepared.transition.toRevision, 1);
  assert.equal(prepared.effect, null);
});

test("e03.mutation.task-receipt", async () => {
  const { durable, port, key } = await preparedRegistry("registry-receipt", {
    effect: true,
  });
  const receipt = await durable.recordReceipt(key);
  assert.ok(receipt);
  assert.equal(receipt?.accepted, true);
  assert.equal(receipt?.requestId, key);
  assert.equal(port.effects.length, 1);
  assert.equal(port.receipts[0]?.digest, receipt?.digest);
});

test("e03.mutation.task-commit", async () => {
  const { durable, value, key } = await preparedRegistry("registry-commit");
  const committed = await durable.commit(key, null);
  assert.notEqual(committed.state.checksum, value.checksum);
  assert.equal(committed.transition.phase, "commit");
  assert.equal(committed.state.identity.taskId, "registry-commit");
  assert.equal(durable.snapshot().revision, 1);
  assert.equal(durable.require("registry-commit").revision, 1);
});

test("e03.mutation.task-ack", async () => {
  const { durable, key } = await preparedRegistry("registry-ack");
  const committed = await durable.commit(key, null);
  const acknowledged = durable.acknowledge(
    key,
    response({
      ok: true,
      request_id: key,
      command: "agent.create",
      phase: "commit",
      revision: committed.state.revision,
    }),
  );
  assert.equal(acknowledged.phase, "ack");
  assert.equal(acknowledged.ok, true);
  assert.equal(acknowledged.replayed, false);
  assert.equal(durable.snapshot().requests[key]?.phase, "ack");
});

test("e03.mutation.task-restore", async () => {
  const restoredTask = task("registry-restore");
  const clock = new TestClock();
  const port = new TestPhysicalPort(clock);
  port.snapshot = snapshotWithTask(restoredTask);
  const durable = new DurableTaskRegistry(
    requireSnapshotFactory()(clock),
    port,
    clock,
  );
  const restored = await durable.restore(
    restoredTask.identity.runId,
    restoredTask.identity.parentSessionId,
  );
  assert.equal(restored.revision, 1);
  assert.equal(
    restored.tasks[restoredTask.identity.taskId]?.checksum,
    restoredTask.checksum,
  );
  assert.equal(durable.require(restoredTask.identity.taskId).status, "created");
  assert.equal(durable.snapshot().checksum, restored.checksum);
});

test("e03.mutation.lost-ack", async () => {
  const { durable, key } = await preparedRegistry("registry-lost-ack");
  const committed = await durable.commit(key, null);
  durable.acknowledge(
    key,
    response({
      ok: true,
      request_id: key,
      command: "agent.create",
      phase: "commit",
      revision: committed.state.revision,
    }),
  );
  const replay = durable.recoverLostAck(key);
  assert.ok(replay);
  assert.equal(replay?.ok, true);
  assert.equal(replay?.replayed, true);
  assert.equal(replay?.phase, "ack");
  assert.equal(replay?.revision, committed.state.revision);
});

test("e03.mutation.task-dispatch", async () => {
  const { registry: durable } = registry();
  const created = await commitTask(durable, task("executor-dispatch"));
  const executor = new TaskExecutor(durable, new TaskStateMachine(), {
    runChild: async () => successfulRunResult({ dispatched: true }),
  });
  const completed = await executor.dispatch({
    requestId: "executor-dispatch-request",
    idempotencyKey: "executor-dispatch-key",
    writerId: "typescript.E03AgentControlCoordinator",
    task: created,
    parentInput: runInput(),
    argumentsValue: { prompt: "dispatch" },
  });
  assert.equal(completed.status, "completed");
  assert.ok(completed.revision > created.revision);
  assert.equal(durable.require(created.identity.taskId).status, "completed");
});

test("e03.mutation.task-wait", async () => {
  const { registry: durable } = registry();
  const created = await commitTask(durable, task("executor-wait"));
  const executor = new TaskExecutor(durable, new TaskStateMachine(), {
    runChild: async () => successfulRunResult(),
  });
  const waited = await executor.wait(created.identity.taskId);
  assert.equal(waited.identity.taskId, created.identity.taskId);
  assert.equal(waited.status, "created");
  assert.equal(waited.revision, created.revision);
  assert.equal(
    waited.checksum,
    durable.require(created.identity.taskId).checksum,
  );
});

test("e03.mutation.task-result", async () => {
  const completed = task("executor-result", { status: "completed" });
  const clock = new TestClock();
  const port = new TestPhysicalPort(clock);
  port.snapshot = snapshotWithTask(completed);
  const durable = new DurableTaskRegistry(port.snapshot, port, clock);
  const executor = new TaskExecutor(durable, new TaskStateMachine(), {
    runChild: async () => successfulRunResult(),
  });
  const result = executor.result(completed.identity.taskId);
  assert.equal(result.ok, true);
  assert.equal(result.phase, "ack");
  assert.equal(result.revision, completed.revision);
  assert.equal(result.state?.task_id, completed.identity.taskId);
});

test("e03.mutation.task-timeout", async () => {
  const { registry: durable } = registry();
  const created = await commitTask(durable, task("executor-timeout"));
  let aborted = "";
  const executor = new TaskExecutor(durable, new TaskStateMachine(), {
    runChild: async () => successfulRunResult(),
    abortChild: async (_taskId, reason) => {
      aborted = reason;
    },
  });
  const failed = await executor.timeout(
    created.identity.taskId,
    "deadline-exceeded",
  );
  assert.equal(failed.status, "failed");
  assert.equal(failed.error, "deadline-exceeded");
  assert.equal(aborted, "deadline-exceeded");
  assert.equal(durable.require(created.identity.taskId).status, "failed");
});

function mailboxPair() {
  const parent = task("mailbox-parent", {
    parentTaskId: "root-task",
    parentSessionId: "root-session",
  });
  const child = task("mailbox-child", {
    parentTaskId: parent.identity.taskId,
    parentSessionId: parent.identity.sessionId,
  });
  return { parent, child, mailbox: new TeamMailbox(new TestClock()) };
}

test("e03.mutation.message-send", () => {
  const { parent, child, mailbox } = mailboxPair();
  const sent = mailbox.send(parent, child, {
    body: "Inspect the artifact.",
    idempotencyKey: "message-send-key",
  });
  assert.equal(sent.message.senderTaskId, parent.identity.taskId);
  assert.equal(sent.message.recipientTaskId, child.identity.taskId);
  assert.equal(sent.message.sequence, 1);
  assert.equal(sent.recipient.messages.length, 1);
  assert.equal(sent.recipient.sequence, child.sequence + 1);
});

test("e03.mutation.message-steer", () => {
  const { parent, child, mailbox } = mailboxPair();
  const steered = mailbox.steer(parent, child, {
    body: "Change course and verify the failure path.",
    idempotencyKey: "message-steer-key",
  });
  assert.equal(steered.message.kind, "steer");
  assert.match(steered.message.body, /Change course/);
  assert.equal(steered.recipient.messages.length, 1);
  assert.equal(mailbox.pending(steered.recipient).length, 1);
});

test("e03.mutation.message-receive", () => {
  const { parent, child, mailbox } = mailboxPair();
  const sent = mailbox.send(parent, child, {
    body: "Receive this exactly once.",
    idempotencyKey: "message-receive-key",
  });
  const received = mailbox.receive(sent.recipient, 1);
  assert.equal(received.messages.length, 1);
  assert.equal(received.messages[0]?.messageId, sent.message.messageId);
  assert.ok(received.messages[0]?.deliveredAt);
  assert.equal(mailbox.pending(received.task).length, 1);
  assert.equal(received.task.sequence, sent.recipient.sequence + 1);
});

test("e03.mutation.message-ack", () => {
  const { parent, child, mailbox } = mailboxPair();
  const sent = mailbox.send(parent, child, {
    body: "Acknowledge this delivery.",
    idempotencyKey: "message-ack-key",
  });
  const received = mailbox.receive(sent.recipient, 1);
  const acknowledged = mailbox.acknowledge(
    received.task,
    sent.message.messageId,
  );
  assert.ok(acknowledged.messages[0]?.acknowledgedAt);
  assert.equal(mailbox.pending(acknowledged).length, 0);
  assert.equal(acknowledged.sequence, received.task.sequence + 1);
  assert.equal(acknowledged.messages[0]?.digest.length, 64);
});

test("e03.mutation.message-dedupe", () => {
  const { parent, child, mailbox } = mailboxPair();
  const sent = mailbox.send(parent, child, {
    body: "Deduplicate this message.",
    idempotencyKey: "message-dedupe-key",
  });
  const selected = mailbox.rejectDuplicate(
    sent.recipient,
    "message-dedupe-key",
    digest("Deduplicate this message."),
  );
  assert.ok(selected);
  assert.equal(selected?.messageId, sent.message.messageId);
  assert.equal(selected?.body, sent.message.body);
  assert.equal(selected?.idempotencyKey, "message-dedupe-key");
});

test("e03.mutation.fanout-plan", () => {
  const parent = task("fanout-parent");
  const runtime = new TeamFanout();
  const plan = runtime.plan(
    parent,
    [
      { key: "alpha", agent: "reviewer", prompt: "Review alpha" },
      { key: "beta", agent: "reviewer", prompt: "Review beta" },
    ],
    {
      maximumConcurrency: 2,
      failureMode: "collect",
      idempotencyKey: "fanout-plan-key",
    },
  );
  assert.equal(plan.parentTaskId, parent.identity.taskId);
  assert.equal(plan.targets.length, 2);
  assert.equal(plan.maximumConcurrency, 2);
  assert.equal(plan.failureMode, "collect");
  assert.equal(plan.digest.length, 64);
});

test("e03.mutation.fanout-dispatch", async () => {
  const parent = task("fanout-dispatch-parent");
  const runtime = new TeamFanout();
  const plan = runtime.plan(
    parent,
    [
      { key: "alpha", agent: "reviewer", prompt: "Review alpha" },
      { key: "beta", agent: "reviewer", prompt: "Review beta" },
    ],
    {
      maximumConcurrency: 1,
      failureMode: "collect",
      idempotencyKey: "fanout-dispatch-key",
    },
  );
  const result = await runtime.dispatch(plan, async (target, index) =>
    task(`fanout-child-${index}`, {
      status: "completed",
      parentTaskId: parent.identity.taskId,
      parentSessionId: parent.identity.sessionId,
      fanoutKey: target.key,
    }),
  );
  assert.equal(result.ok, true);
  assert.equal(result.completed.length, 2);
  assert.equal(result.failed.length, 0);
  assert.match(result.summary, /2 completed/);
});

test("e03.mutation.fanin-collect", () => {
  const parent = task("fanin-parent");
  const runtime = new TeamFanout();
  const plan = runtime.plan(
    parent,
    [
      { key: "alpha", agent: "reviewer", prompt: "Review alpha" },
      { key: "beta", agent: "reviewer", prompt: "Review beta" },
    ],
    {
      maximumConcurrency: 2,
      idempotencyKey: "fanin-collect-key",
    },
  );
  const first = task("fanin-alpha", {
    status: "completed",
    parentTaskId: parent.identity.taskId,
    parentSessionId: parent.identity.sessionId,
    fanoutKey: "alpha",
  });
  const second = task("fanin-beta", {
    status: "completed",
    parentTaskId: parent.identity.taskId,
    parentSessionId: parent.identity.sessionId,
    fanoutKey: "beta",
  });
  const result = runtime.collect(plan, [second, first]);
  assert.equal(result.ok, true);
  assert.deepEqual(
    result.completed.map((value) => value.definition.metadata.fanout_key),
    ["alpha", "beta"],
  );
  assert.equal(result.pending.length, 0);
});

test("e03.mutation.fanout-fail-fast", () => {
  const parent = task("fail-fast-parent");
  const runtime = new TeamFanout();
  const plan = runtime.plan(
    parent,
    [{ key: "alpha", agent: "reviewer", prompt: "Review alpha" }],
    {
      maximumConcurrency: 1,
      failureMode: "fail-fast",
      idempotencyKey: "fanout-fail-fast-key",
    },
  );
  const failed = task("fail-fast-child", {
    status: "failed",
    parentTaskId: parent.identity.taskId,
    parentSessionId: parent.identity.sessionId,
    fanoutKey: "alpha",
  });
  const decision = runtime.failFast(plan, [failed]);
  assert.equal(decision.stop, true);
  assert.deepEqual(decision.failingTaskIds, [failed.identity.taskId]);
  assert.match(decision.reason, /failed/);
});
