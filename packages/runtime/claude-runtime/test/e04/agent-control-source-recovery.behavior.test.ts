import assert from "node:assert/strict";
import { resolve } from "node:path";
import { test } from "bun:test";

import {
  AgentControlHandler,
  AgentExecutionRuntime,
  DurableTaskRegistry,
  IsolationRequestRuntime,
  TypeScriptAgentRuntime,
  TypeScriptControlRuntime,
  type AgentExecutionContext,
  type RuntimeRunInput,
  type RuntimeRunResult,
} from "../../src/index.ts";
import {
  digest,
  emptySnapshot,
  type E03ControlEnvelope,
  type E03IsolationReceipt,
  type E03TaskState,
} from "../../src/e03/contracts.ts";
import {
  TestClock,
  TestPhysicalPort,
  commitTask,
  context,
  definition,
  registry,
  runInput,
  scope,
  successfulRunResult,
  task,
} from "../e03/fixtures.ts";

function parentInput(): RuntimeRunInput {
  return {
    ...runInput(),
    tools: [
      {
        name: "read",
        purpose: "read",
        source: "builtin",
        input_schema: { type: "object" },
        output_schema: { type: "object" },
        metadata: {},
      },
      {
        name: "write",
        purpose: "write",
        source: "builtin",
        input_schema: { type: "object" },
        output_schema: { type: "object" },
        metadata: {},
      },
    ],
    metadata: {
      workspace_roots: [process.cwd()],
      skills: ["analysis", "review"],
      mcp_servers: ["local", "remote"],
    },
  };
}

function physicalHost(port: TestPhysicalPort): AgentExecutionContext["host"] {
  return {
    emitEvent: async () => {},
    executeBatch: async () => [],
    externalize: async () => {
      throw new Error("artifact effect is not used by this test");
    },
    isAborted: () => false,
    mutateAgent: async (request) => {
      if (request.action === "e03.restore") {
        const snapshot = await port.restore(
          String(request.run_id),
          String(request.parent_session_id),
        );
        return {
          accepted: Boolean(snapshot),
          task_id: request.task_id,
          status: snapshot ? "restored" : "rejected",
          snapshot,
          error: snapshot ? "" : "e03_snapshot_not_found",
          error_code: snapshot ? "" : "e03_snapshot_not_found",
        } as any;
      }
      if (request.action === "e03.effect") {
        const receipt = await port.effect(request.effect_request as any);
        return {
          accepted: receipt.accepted,
          task_id: request.task_id,
          status: receipt.accepted ? "effect-recorded" : "rejected",
          effect_receipt: receipt,
          error: receipt.error,
        } as any;
      }
      if (request.action === "e03.cas") {
        const receipt = await port.compareAndSwap(
          Number(request.expected_registry_revision),
          request.registry_snapshot as any,
        );
        return {
          ...receipt,
          task_id: request.task_id,
          status: receipt.accepted ? "committed" : "rejected",
        } as any;
      }
      throw new Error(`unexpected physical action ${request.action}`);
    },
  };
}

async function createExecutionTask(
  runtime: AgentExecutionRuntime,
  taskId: string,
): Promise<E03TaskState> {
  const childScope = scope({
    tools: ["read"],
    deniedTools: ["write"],
    skills: ["analysis"],
    mcpServers: ["local"],
  });
  return runtime.create({
    requestId: `create:${taskId}`,
    runId: "run-e03",
    sessionId: `session-${taskId}`,
    parentTaskId: "parent-task",
    parentSessionId: "parent-session",
    taskId,
    idempotencyKey: `create:${taskId}`,
    definition: definition(`definition-${taskId}`, {
      tools: ["read"],
      deniedTools: ["write"],
      skills: ["analysis"],
      mcpServers: ["local"],
    }),
    scope: childScope,
    context: context(taskId, `session-${taskId}`, childScope),
    prompt: `perform ${taskId}`,
    executionMode: "foreground",
  });
}

function childResult(
  taskState: E03TaskState,
  changes: Partial<RuntimeRunResult> = {},
): RuntimeRunResult {
  return {
    ...successfulRunResult(),
    metadata: {
      ...successfulRunResult().metadata,
      e03_agent_task_id: taskState.identity.taskId,
      e03_agent_lease_id: taskState.identity.leaseId,
    },
    ...changes,
  };
}

function controlEnvelope(
  requestId: string,
  command: "agent.cancel" | "agent.kill",
  taskState: E03TaskState,
): E03ControlEnvelope {
  return {
    schema_version: "3.0",
    request_id: requestId,
    idempotency_key: requestId,
    run_id: taskState.identity.runId,
    session_id: taskState.identity.parentSessionId,
    parent_task_id: taskState.identity.parentTaskId,
    expected_revision: taskState.revision,
    command,
    body: { task_id: taskState.identity.taskId, reason: requestId },
  };
}

function isolationReceipt(
  request: ReturnType<IsolationRequestRuntime["prepare"]>,
  disposition: "created" | "reused",
  changes: Partial<E03IsolationReceipt> = {},
): E03IsolationReceipt {
  const payload = {
    receiptId: `receipt:${request.requestId}:${disposition}`,
    requestId: request.requestId,
    taskId: request.taskId,
    leaseId: request.leaseId,
    accepted: true,
    workspacePath: resolve(process.cwd(), ".tmp", request.requestId),
    observedBaseRevision: "e04-head",
    resultingRevision: "e04-head",
    dirtyBaseline: false,
    nestedRepository: false,
    mergeConflict: false,
    cleanupFailed: false,
    workspaceDisposition: disposition,
    physicalBackend: "git-worktree",
    worktreeHead: "e04-head",
    worktreeBranch: request.branchName,
    artifacts: [],
    error: "",
    completedAt: "2026-07-18T00:00:10.000Z",
    ...changes,
  };
  return { ...payload, digest: digest(payload) };
}

test("e04-agent-run-resume", async () => {
  const clock = new TestClock();
  const harness = registry(clock);
  let created: E03TaskState | null = null;
  let captured: RuntimeRunInput | null = null;
  const runtime = new AgentExecutionRuntime(harness.registry, {
    runChild: async (input) => {
      captured = input;
      return childResult(created!);
    },
  }, clock);
  created = await createExecutionTask(runtime, "e04-agent-success");
  const completed = await runtime.run(
    created.identity.taskId,
    parentInput(),
    { prompt: "scoped child" },
    "run:e04-agent-success",
    "run:e04-agent-success",
  );
  assert.equal(completed.status, "completed");
  assert.deepEqual(captured!.tools.map((tool) => tool.name), ["read"]);
  assert.deepEqual(captured!.metadata?.skills, ["analysis"]);
  assert.deepEqual(captured!.metadata?.mcp_servers, ["local"]);
  assert.equal(
    captured!.config.runtimeConstraints?.e03_lease_id,
    created.identity.leaseId,
  );
  assert.equal(captured!.restoredState?.agent_task_id, created.identity.taskId);

  const controlClock = new TestClock();
  const controlHarness = registry(controlClock);
  const controlExecution = new AgentExecutionRuntime(controlHarness.registry, {
    runChild: async () => successfulRunResult(),
  }, controlClock);
  const handler = new AgentControlHandler(
    controlHarness.registry,
    controlExecution,
  );
  const cancellable = await commitTask(
    controlHarness.registry,
    task("e04-cancel"),
  );
  const cancel = controlEnvelope("cancel:e04", "agent.cancel", cancellable);
  const cancelled = await handler.execute(cancel);
  assert.equal(cancelled.state?.status, "cancelled");
  const effectsAfterCancel = controlHarness.port.effects.length;
  const cancelReplay = await handler.execute(cancel);
  assert.equal(cancelReplay.replayed, true);
  assert.equal(controlHarness.port.effects.length, effectsAfterCancel);

  const killable = await commitTask(
    controlHarness.registry,
    task("e04-kill"),
    "commit:e04-kill",
  );
  const kill = controlEnvelope("kill:e04", "agent.kill", killable);
  const killed = await handler.execute(kill);
  assert.equal(killed.state?.status, "killed");
  const effectsAfterKill = controlHarness.port.effects.length;
  const killReplay = await handler.execute(kill);
  assert.equal(killReplay.replayed, true);
  assert.equal(controlHarness.port.effects.length, effectsAfterKill);

  const backgroundClock = new TestClock();
  const backgroundPort = new TestPhysicalPort(backgroundClock);
  const host = physicalHost(backgroundPort);
  const input = parentInput();
  const firstAgentTool = new TypeScriptAgentRuntime(input);
  const firstContext: AgentExecutionContext = {
    parentInput: input,
    host,
    runChild: async () => {
      throw new Error("background work must not run during spawn");
    },
  };
  const queued = await firstAgentTool.execute(
    "Agent",
    {
      prompt: "durable background child",
      agent_type: "explore",
      task_id: "e04-durable-background",
      idempotency_key: "e04-durable-background",
      background: true,
      isolation: "workspace",
      workspace_root: process.cwd(),
    },
    firstContext,
  );
  assert.equal(queued.output.status, "created");
  assert.equal(firstAgentTool.snapshot().background_queue_owner, "DurableTaskRegistry");
  assert.deepEqual(firstAgentTool.snapshot().queued_background_task_ids, [
    "e04-durable-background",
  ]);

  let backgroundRuns = 0;
  const restoredAgentTool = new TypeScriptAgentRuntime(input);
  const restoredContext: AgentExecutionContext = {
    parentInput: input,
    host,
    runChild: async (child) => {
      backgroundRuns += 1;
      return {
        ...successfulRunResult(),
        metadata: {
          ...successfulRunResult().metadata,
          e03_agent_task_id: child.taskId,
          e03_agent_lease_id: String(
            child.config.runtimeConstraints?.e03_lease_id ?? "",
          ),
        },
      };
    },
  };
  const restoredList = await restoredAgentTool.execute(
    "agent_list",
    {},
    restoredContext,
  );
  assert.deepEqual(
    (restoredList.output.tasks as Array<{ task_id: string }>).map(
      (value) => value.task_id,
    ),
    ["e04-durable-background"],
  );
  await restoredAgentTool.drainBackground(restoredContext);
  assert.equal(
    backgroundRuns,
    1,
    JSON.stringify(restoredAgentTool.snapshot().background_claims),
  );
  assert.deepEqual(restoredAgentTool.snapshot().queued_background_task_ids, []);
});

test("e04-agent-failure", async () => {
  const clock = new TestClock();
  const harness = registry(clock);
  let created: E03TaskState | null = null;
  const runtime = new AgentExecutionRuntime(harness.registry, {
    runChild: async () => ({
      ...childResult(created!),
      metadata: {
        e03_agent_task_id: created!.identity.taskId,
        e03_agent_lease_id: "stale-lease",
      },
    }),
  }, clock);
  created = await createExecutionTask(runtime, "e04-agent-ownership-failure");
  const failed = await runtime.run(
    created.identity.taskId,
    parentInput(),
    {},
    "run:e04-agent-ownership-failure",
    "run:e04-agent-ownership-failure",
  );
  assert.equal(failed.status, "failed");
  assert.match(failed.error, /lease_ownership/);
  assert.equal(failed.result, null);
});

test("e04-agent-resume", async () => {
  const clock = new TestClock();
  const port = new TestPhysicalPort(clock);
  const firstRegistry = new DurableTaskRegistry(emptySnapshot(clock), port, clock);
  let created: E03TaskState | null = null;
  const first = new AgentExecutionRuntime(firstRegistry, {
    runChild: async () => ({
      ...childResult(created!),
      ok: false,
      stoppedReason: "first-attempt-failed",
    }),
  }, clock);
  created = await createExecutionTask(first, "e04-agent-resume-task");
  const failed = await first.run(
    created.identity.taskId,
    parentInput(),
    {},
    "run:e04-agent-resume-first",
    "run:e04-agent-resume-first",
  );
  assert.equal(failed.status, "failed");

  const secondRegistry = new DurableTaskRegistry(emptySnapshot(clock), port, clock);
  await secondRegistry.restore(failed.identity.runId, failed.identity.parentSessionId);
  let resumedInput: RuntimeRunInput | null = null;
  const second = new AgentExecutionRuntime(secondRegistry, {
    runChild: async (input) => {
      resumedInput = input;
      return {
        ...successfulRunResult(),
        metadata: {
          ...successfulRunResult().metadata,
          e03_agent_task_id: input.taskId,
          e03_agent_lease_id: String(
            input.config.runtimeConstraints?.e03_lease_id ?? "",
          ),
        },
      };
    },
  }, clock);
  const resumePromise = second.resume(
    failed.identity.taskId,
    failed.revision,
    parentInput(),
    { prompt: "resume from durable context" },
    "resume:e04-agent",
    "resume:e04-agent",
  );
  const resumed = await resumePromise;
  assert.equal(resumed.status, "completed");
  assert.equal(resumed.identity.attempt, 2);
  assert.notEqual(resumed.identity.leaseId, failed.identity.leaseId);
  assert.equal(
    resumedInput!.restoredState?.agent_lease_id,
    resumed.identity.leaseId,
  );
  assert.equal(resumed.deliveries.filter((item) => item.kind === "final").length, 2);
});

test("e04-agent-disable", async () => {
  const clock = new TestClock();
  const harness = registry(clock);
  const runtime = new AgentExecutionRuntime(harness.registry, {
    runChild: async () => successfulRunResult(),
  }, clock);
  const created = await createExecutionTask(runtime, "e04-agent-disable-task");
  const previous = process.env.ZYRA_DISABLE_E04_AGENT_SOURCE_RUNTIME;
  try {
    process.env.ZYRA_DISABLE_E04_AGENT_SOURCE_RUNTIME = "1";
    const failed = await runtime.run(
      created.identity.taskId,
      parentInput(),
      {},
      "run:e04-agent-disable",
      "run:e04-agent-disable",
    );
    assert.equal(failed.status, "failed");
    assert.match(failed.error, /source runtime is disabled|no legacy or Python/i);
  } finally {
    if (previous === undefined)
      delete process.env.ZYRA_DISABLE_E04_AGENT_SOURCE_RUNTIME;
    else process.env.ZYRA_DISABLE_E04_AGENT_SOURCE_RUNTIME = previous;
  }
});

test("e04-isolation-control", () => {
  const runtime = new IsolationRequestRuntime(new TestClock());
  const state = task("e04-isolation-control", {
    scope: scope({
      workspaceRoots: [process.cwd()],
      isolationModes: ["worktree"],
    }),
  });
  const request = runtime.prepare(state, {
    mode: "worktree",
    workspaceRoot: process.cwd(),
    baseRevision: "HEAD",
    branchName: "review/e04",
    idempotencyKey: "e04-isolation-control",
  });
  assert.equal(request.reuseExisting, true);
  assert.equal(request.createIfMissing, true);
  const receipt = runtime.recordReceipt(
    request,
    isolationReceipt(request, "created"),
  );
  assert.equal(receipt.workspaceDisposition, "created");
  assert.equal(receipt.physicalBackend, "git-worktree");
  const committed = runtime.commit(state, request, receipt);
  assert.equal(committed.isolationReceipt?.worktreeHead, "e04-head");

  const terminal = task("e04-control-terminal", { status: "completed" });
  const decision = TypeScriptControlRuntime.decideAgentTerminalMutation(
    terminal,
    { action: "kill", expectedRevision: 0 },
  );
  assert.equal(decision.replay, true);
  assert.equal(decision.physicalEffectRequired, false);
  assert.equal(decision.canonicalOwner, "typescript.E03AgentControlCoordinator");
});

test("e04-isolation-failure", () => {
  const runtime = new IsolationRequestRuntime(new TestClock());
  const state = task("e04-isolation-failure", {
    scope: scope({
      workspaceRoots: [process.cwd()],
      isolationModes: ["worktree"],
    }),
  });
  const request = runtime.prepare(state, {
    mode: "worktree",
    workspaceRoot: process.cwd(),
    baseRevision: "HEAD",
    idempotencyKey: "e04-isolation-failure",
  });
  assert.throws(
    () =>
      runtime.recordReceipt(
        request,
        isolationReceipt(request, "reused", {
          worktreeHead: "wrong-head",
        }),
      ),
    /does not prove the requested revision|worktree_receipt_revision/,
  );
  assert.throws(
    () =>
      TypeScriptControlRuntime.decideAgentTerminalMutation(state, {
        action: "cancel",
        expectedRevision: state.revision + 1,
      }),
    /stale_revision|expected.*current/,
  );
});

test("e04-isolation-resume", () => {
  const runtime = new IsolationRequestRuntime(new TestClock());
  const state = task("e04-isolation-resume", {
    scope: scope({
      workspaceRoots: [process.cwd()],
      isolationModes: ["worktree"],
    }),
  });
  const input = {
    mode: "worktree" as const,
    workspaceRoot: process.cwd(),
    baseRevision: "HEAD",
    branchName: "review/e04-resume",
    idempotencyKey: "e04-isolation-resume",
  };
  const original = runtime.prepare(state, input);
  const replay = runtime.prepare(state, input);
  assert.equal(replay.requestId, original.requestId);
  assert.equal(replay.branchName, original.branchName);
  const reused = runtime.recordReceipt(
    replay,
    isolationReceipt(replay, "reused"),
  );
  assert.equal(reused.workspaceDisposition, "reused");
  assert.equal(reused.worktreeHead, reused.resultingRevision);
});

test("e04-isolation-disable", () => {
  const runtime = new IsolationRequestRuntime(new TestClock());
  const state = task("e04-isolation-disable", {
    scope: scope({ workspaceRoots: [process.cwd()] }),
  });
  const previous = process.env.ZYRA_DISABLE_E04_ISOLATION_SOURCE_RUNTIME;
  try {
    process.env.ZYRA_DISABLE_E04_ISOLATION_SOURCE_RUNTIME = "1";
    assert.throws(
      () =>
        runtime.prepare(state, {
          mode: "workspace",
          workspaceRoot: process.cwd(),
          baseRevision: "HEAD",
          idempotencyKey: "e04-isolation-disable",
        }),
      /source runtime is disabled|no legacy or Python/i,
    );
  } finally {
    if (previous === undefined)
      delete process.env.ZYRA_DISABLE_E04_ISOLATION_SOURCE_RUNTIME;
    else process.env.ZYRA_DISABLE_E04_ISOLATION_SOURCE_RUNTIME = previous;
  }
});
