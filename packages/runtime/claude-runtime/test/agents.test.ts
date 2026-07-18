import assert from "node:assert/strict";
import test from "node:test";

import {
  ClaudeRuntimeCore,
  PermissionedCapabilityHost,
  TypeScriptCapabilityRuntime,
  type AgentMutationReceipt,
  type AgentMutationRequest,
  type ArtifactReceipt,
  type ArtifactRequest,
  type JsonObject,
  type RuntimeEvent,
  type RuntimeHost,
  type RuntimeRunInput,
  type ToolBatch,
  type ToolExecutionRequest,
  type ToolExecutionResponse,
} from "../src/index.ts";
import {
  digest,
  effectRequestDigest,
  type E03EffectRequest,
} from "../src/e03/contracts.ts";

class AgentHost implements RuntimeHost {
  readonly events: RuntimeEvent[] = [];
  readonly mutations: AgentMutationRequest[] = [];
  readonly revisions = new Map<string, number>();
  readonly statuses = new Map<string, string>();
  readonly records = new Map<string, JsonObject>();
  private readonly effectReceipts = new Map<string, JsonObject>();
  private e03Snapshot: JsonObject | null = null;
  private e03Revision = 0;

  async emitEvent(event: RuntimeEvent): Promise<void> {
    this.events.push(event);
  }

  async executeBatch(
    _batch: ToolBatch,
    requests: ToolExecutionRequest[],
  ): Promise<ToolExecutionResponse[]> {
    return requests.map((request) => ({
      tool_call_id: request.toolCallId,
      ok: true,
      summary: "executed " + request.toolName,
      output: {},
      artifacts: [],
      error: null,
      metadata: {
        permission_effect: "allow",
        permission_commit_only: request.permissionOnly ? "true" : "false",
      },
    }));
  }

  async mutateAgent(request: AgentMutationRequest): Promise<AgentMutationReceipt> {
    if (request.action === "e03.restore") {
      if (!this.e03Snapshot) {
        return {
          accepted: false,
          task_id: request.task_id,
          status: "",
          revision: this.e03Revision,
          error: "e03 snapshot not found",
          error_code: "e03_snapshot_not_found",
        };
      }
      return {
        accepted: true,
        task_id: request.task_id,
        status: "restored",
        revision: this.e03Revision,
        error: "",
        snapshot: structuredClone(this.e03Snapshot),
      };
    }
    if (request.action === "e03.cas") {
      const expected = Number(request.expected_registry_revision);
      if (expected !== this.e03Revision) {
        const candidate = request.registry_snapshot as JsonObject;
        if (this.e03Snapshot?.checksum === candidate.checksum) {
          return {
            accepted: true,
            task_id: request.task_id,
            status: "committed",
            revision: this.e03Revision,
            replayed: true,
            error: "",
          };
        }
        return {
          accepted: false,
          task_id: request.task_id,
          status: "conflict",
          revision: this.e03Revision,
          error: "revision conflict",
        };
      }
      this.e03Snapshot = structuredClone(request.registry_snapshot as JsonObject);
      this.e03Revision = Number(this.e03Snapshot.revision ?? expected + 1);
      return {
        accepted: true,
        task_id: request.task_id,
        status: "committed",
        revision: this.e03Revision,
        replayed: false,
        error: "",
      };
    }
    if (request.action === "e03.effect") {
      const effect = request.effect_request as unknown as E03EffectRequest;
      const prior = this.effectReceipts.get(effect.idempotencyKey);
      if (prior) {
        const replayPayload = {
          ...prior,
          effectId: effect.effectId,
          requestId: effect.requestId,
          taskId: effect.taskId,
          leaseId: effect.leaseId,
          expectedRevision: effect.expectedRevision,
          replayed: true,
          digest: "",
        };
        const { digest: _priorDigest, ...unsignedReplay } = replayPayload;
        const replay = { ...unsignedReplay, digest: digest(unsignedReplay) };
        this.effectReceipts.set(effect.idempotencyKey, replay);
        return {
          accepted: true,
          task_id: request.task_id,
          status: "effect_committed",
          revision: this.e03Revision,
          error: "",
          effect_receipt: replay,
        };
      }
      const payload = {
        receiptId: `receipt-${digest(effect.effectId).slice(0, 32)}`,
        effectId: effect.effectId,
        idempotencyKey: effect.idempotencyKey,
        requestDigest: effectRequestDigest(effect),
        requestId: effect.requestId,
        taskId: effect.taskId,
        leaseId: effect.leaseId,
        expectedRevision: effect.expectedRevision,
        accepted: true,
        replayed: false,
        result: {
          operation: effect.operation,
          effect_kind: effect.effectKind,
          physical_port: "agents-test-host",
        },
        artifacts: [],
        error: "",
        completedAt: new Date().toISOString(),
      };
      const receipt = { ...payload, digest: digest(payload) };
      this.effectReceipts.set(effect.idempotencyKey, receipt);
      return {
        accepted: true,
        task_id: request.task_id,
        status: "effect_committed",
        revision: this.e03Revision,
        error: "",
        effect_receipt: receipt,
      };
    }
    this.mutations.push(request);
    if (request.action === "list") {
      return {
        accepted: true,
        task_id: request.task_id,
        status: "listed",
        revision: 0,
        error: "",
        tasks: [...this.records.values()],
      };
    }
    if (request.action === "load") {
      const record = this.records.get(request.task_id);
      if (!record) {
        return {
          accepted: false,
          task_id: request.task_id,
          status: "",
          revision: -1,
          error: "not found",
          error_code: "agent_task_not_found",
        };
      }
      return {
        accepted: true,
        task_id: request.task_id,
        status: String(record.status),
        revision: Number(record.revision),
        error: "",
        record,
      };
    }
    const current = this.revisions.get(request.task_id) ?? 0;
    let next = current;
    let status = this.statuses.get(request.task_id) ?? "created";
    if (request.action === "create") {
      next = 2;
      status = "ready";
      this.records.set(request.task_id, {
        ...(request.record as JsonObject),
        status,
        revision: next,
        attempt: 1,
        created_at: new Date(0).toISOString(),
        updated_at: new Date(0).toISOString(),
      });
    } else {
      const expected = Number(request.expected_revision);
      if (expected !== current) {
        return {
          accepted: false,
          task_id: request.task_id,
          status,
          revision: current,
          error: "revision conflict",
        };
      }
      next += 1;
      status = {
        dispatch: "dispatched",
        running: "running",
        complete: "completed",
        fail: "failed",
        cancel: "cancelled",
        resume: "resuming",
        message: status,
      }[request.action] ?? status;
    }
    this.revisions.set(request.task_id, next);
    this.statuses.set(request.task_id, status);
    const prior = this.records.get(request.task_id) ?? {};
    const metadata: JsonObject = { ...((prior.metadata as JsonObject) ?? {}) };
    if (request.action === "complete" || request.action === "fail") {
      metadata.typescript_result = request.result;
      metadata.result_digest = request.result_digest;
    }
    this.records.set(request.task_id, {
      ...prior,
      status,
      revision: next,
      attempt: Number(prior.attempt ?? 1),
      metadata,
      updated_at: new Date().toISOString(),
    });
    return {
      accepted: true,
      task_id: request.task_id,
      status,
      revision: next,
      error: "",
      record: this.records.get(request.task_id) ?? {},
    };
  }

  async externalize(request: ArtifactRequest): Promise<ArtifactReceipt> {
    return {
      artifact_id: "artifact-" + request.requestId,
      kind: request.kind,
      uri: "memory://" + request.requestId,
      title: request.title,
    };
  }

  isAborted(): boolean {
    return false;
  }

  e03Task(taskId: string): JsonObject | null {
    const tasks = (this.e03Snapshot?.tasks as JsonObject | undefined) ?? {};
    return (tasks[taskId] as JsonObject | undefined) ?? null;
  }
}

function input(): RuntimeRunInput {
  return {
    runId: "run-agent",
    taskId: "parent-task",
    nodeId: "node",
    workerRequestId: "worker-parent",
    sessionId: "parent-session",
    messages: [{ role: "user", content: "delegate" }],
    turns: [],
    tools: [{
      name: "read",
      purpose: "read",
      source: "test",
      input_schema: {
        type: "object",
        required: ["path"],
        properties: { path: { type: "string" } },
      },
      output_schema: {},
      metadata: { read_only: "true", concurrency_safe: "true" },
    }],
    config: {
      permissionPolicy: { mode: "default", default_effect: "allow" },
      runtimeConstraints: {
        workspaceRoot: process.cwd(),
        agentBudget: {
          maxTurns: 12,
          maxToolCalls: 48,
          maxInputTokens: 64000,
          maxOutputTokens: 16000,
          maxResultChars: 120000,
          maxWallTimeMs: 900000,
          maxChildren: 4,
          maxDepth: 3,
        },
      },
    },
  };
}

test("TypeScript AgentTool runs a child QueryEngine and commits one durable lifecycle", async () => {
  const selected = input();
  const capabilities = await TypeScriptCapabilityRuntime.open(selected);
  const runtimeInput = { ...selected, tools: capabilities.mergeToolSpecs(selected.tools) };
  const delegate = new AgentHost();
  const host = new PermissionedCapabilityHost(delegate, runtimeInput, capabilities);
  const result = await capabilities.execute("Agent", {
    prompt: "Read a file.",
    task_id: "child-1",
    turns: [[{ tool_name: "read", arguments: { path: "README.md" } }]],
  }, {
    parentInput: runtimeInput,
    host,
    runChild: async (child) => new ClaudeRuntimeCore().run(
      child,
      new PermissionedCapabilityHost(delegate, child, capabilities),
    ),
  });
  assert.equal(result.output.status, "completed");
  assert.equal((result.output.result as JsonObject).ok, true, JSON.stringify(result.output));
  assert.equal(delegate.e03Task("child-1")?.status, "completed");
  assert.equal(delegate.mutations.length, 0);
  assert.equal((capabilities.snapshot().agents as Record<string, unknown>).python_agent_fallback, false);
  await capabilities.close();
});

test("AgentTool rejects inherited depth overflow", async () => {
  const selected = input();
  selected.metadata = { e03_depth: 4 };
  const capabilities = await TypeScriptCapabilityRuntime.open(selected);
  const runtimeInput = { ...selected, tools: capabilities.mergeToolSpecs(selected.tools) };
  const delegate = new AgentHost();
  const host = new PermissionedCapabilityHost(delegate, runtimeInput, capabilities);
  await assert.rejects(
    capabilities.execute("Agent", { prompt: "too deep" }, {
      parentInput: runtimeInput,
      host,
      runChild: async () => { throw new Error("unreachable"); },
    }),
    /depth/,
  );
  await capabilities.close();
});

test("background AgentTool is drained without a Python child loop", async () => {
  const selected = input();
  const capabilities = await TypeScriptCapabilityRuntime.open(selected);
  const runtimeInput = { ...selected, tools: capabilities.mergeToolSpecs(selected.tools) };
  const delegate = new AgentHost();
  const host = new PermissionedCapabilityHost(delegate, runtimeInput, capabilities);
  const context = {
    parentInput: runtimeInput,
    host,
    runChild: async (child: RuntimeRunInput) => new ClaudeRuntimeCore().run(
      child,
      new PermissionedCapabilityHost(delegate, child, capabilities),
    ),
  };
  const accepted = await capabilities.execute("Agent", {
    prompt: "background read",
    task_id: "background-1",
    agent_type: "explore",
    background: true,
  }, context);
  assert.equal(accepted.output.status, "created");
  await capabilities.drainBackground(context);
  const completed = await capabilities.execute("agent_result", {
    task_id: "background-1",
  }, context);
  assert.equal(completed.output.status, "completed");
  assert.equal(delegate.e03Task("background-1")?.status, "completed");
  await capabilities.close();
});

test("a new TypeScript runtime hydrates and cancels an exact durable background task", async () => {
  const selected = input();
  const delegate = new AgentHost();
  const first = await TypeScriptCapabilityRuntime.open(selected);
  const firstInput = { ...selected, tools: first.mergeToolSpecs(selected.tools) };
  const firstHost = new PermissionedCapabilityHost(delegate, firstInput, first);
  const created = await first.execute("Agent", {
    prompt: "durable background read",
    task_id: "durable-background-1",
    idempotency_key: "durable-background-key",
    agent_type: "explore",
    background: true,
  }, {
    parentInput: firstInput,
    host: firstHost,
    runChild: async () => { throw new Error("background must not run before cancellation"); },
  });
  await first.close();

  const second = await TypeScriptCapabilityRuntime.open(selected);
  const secondInput = { ...selected, tools: second.mergeToolSpecs(selected.tools) };
  const secondHost = new PermissionedCapabilityHost(delegate, secondInput, second);
  const cancelled = await second.execute("agent_cancel", {
    task_id: "durable-background-1",
    expected_revision: created.output.revision,
  }, {
    parentInput: secondInput,
    host: secondHost,
    runChild: async () => { throw new Error("cancel must not run a child"); },
  });
  assert.equal(cancelled.output.status, "cancelled");
  assert.equal(delegate.e03Task("durable-background-1")?.status, "cancelled");
  await second.close();
});

test("TypeScript fanout preserves request order and owns fanin", async () => {
  const selected = input();
  const capabilities = await TypeScriptCapabilityRuntime.open(selected);
  const runtimeInput = { ...selected, tools: capabilities.mergeToolSpecs(selected.tools) };
  const delegate = new AgentHost();
  const host = new PermissionedCapabilityHost(delegate, runtimeInput, capabilities);
  const result = await capabilities.execute("Agent", {
    prompt: "fanout",
    idempotency_key: "fanout-key",
    budget: { max_children: 2 },
    requests: [
      { prompt: "first", task_id: "fanout-first", turns: [], background: false },
      { prompt: "second", task_id: "fanout-second", turns: [], background: false },
    ],
  }, {
    parentInput: runtimeInput,
    host,
    runChild: async (child) => new ClaudeRuntimeCore().run(
      child,
      new PermissionedCapabilityHost(delegate, child, capabilities),
    ),
  });
  const values = result.output.tasks as Array<{ task_id: string }>;
  assert.deepEqual(values.map((item) => item.task_id), ["fanout-first", "fanout-second"]);
  assert.equal(result.output.count, 2);
  assert.equal(result.output.maximum_concurrency, 2);
  await capabilities.close();
});

test("cross-process Agent replay returns the durable result without a second dispatch", async () => {
  const selected = input();
  const delegate = new AgentHost();
  const first = await TypeScriptCapabilityRuntime.open(selected);
  const firstInput = { ...selected, tools: first.mergeToolSpecs(selected.tools) };
  const firstHost = new PermissionedCapabilityHost(delegate, firstInput, first);
  const argumentsValue = {
    prompt: "idempotent child",
    task_id: "replay-child",
    idempotency_key: "replay-child-key",
    turns: [],
  };
  await first.execute("Agent", argumentsValue, {
    parentInput: firstInput,
    host: firstHost,
    runChild: async (child) => new ClaudeRuntimeCore().run(
      child,
      new PermissionedCapabilityHost(delegate, child, first),
    ),
  });
  await first.close();
  const mutationCount = delegate.mutations.length;

  const second = await TypeScriptCapabilityRuntime.open(selected);
  const secondInput = { ...selected, tools: second.mergeToolSpecs(selected.tools) };
  const replayed = await second.execute("Agent", argumentsValue, {
    parentInput: secondInput,
    host: new PermissionedCapabilityHost(delegate, secondInput, second),
    runChild: async () => { throw new Error("idempotent replay must not dispatch"); },
  });
  assert.equal(replayed.output.status, "completed");
  assert.deepEqual(
    delegate.mutations.slice(mutationCount).map((item) => item.action),
    [],
  );
  await second.close();
});

test("cross-process Agent resume uses exact revision and correlation", async () => {
  const selected = input();
  const delegate = new AgentHost();
  const first = await TypeScriptCapabilityRuntime.open(selected);
  const firstInput = { ...selected, tools: first.mergeToolSpecs(selected.tools) };
  const failed = await first.execute("Agent", {
    prompt: "fail then resume",
    task_id: "resume-child",
    idempotency_key: "resume-child-key",
    turns: [],
  }, {
    parentInput: firstInput,
    host: new PermissionedCapabilityHost(delegate, firstInput, first),
    runChild: async () => { throw new Error("intentional first attempt failure"); },
  });
  assert.equal(failed.output.status, "failed");
  await first.close();

  const second = await TypeScriptCapabilityRuntime.open(selected);
  const secondInput = { ...selected, tools: second.mergeToolSpecs(selected.tools) };
  const resumed = await second.execute("agent_resume", {
    task_id: "resume-child",
    expected_revision: failed.output.revision,
    resume_correlation_id: "resume-correlation-1",
    restored_state: {},
    turns: [],
  }, {
    parentInput: secondInput,
    host: new PermissionedCapabilityHost(delegate, secondInput, second),
    runChild: async (child) => new ClaudeRuntimeCore().run(
      child,
      new PermissionedCapabilityHost(delegate, child, second),
    ),
  });
  assert.equal(resumed.output.status, "completed");
  assert.equal(delegate.e03Task("resume-child")?.status, "completed");
  await second.close();
});
