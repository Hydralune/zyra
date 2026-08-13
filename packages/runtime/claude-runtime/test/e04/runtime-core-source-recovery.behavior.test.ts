import { afterEach, expect, test } from "bun:test";

import type {
  ArtifactReceipt,
  ArtifactRequest,
  JsonObject,
  RuntimeEvent,
  RuntimeHost,
  RuntimeRunInput,
  ToolBatch,
  ToolExecutionRequest,
  ToolExecutionResponse,
} from "../../src/contracts.ts";
import { CompactionSourceCustodyRuntime } from "../../src/compact/compaction-custody-runtime.ts";
import {
  ContextCompactionRuntime,
  type CompactMessage,
  type CompactOptions,
} from "../../src/compact/context-runtime.ts";
import { CompactRestoreRuntime } from "../../src/compact/restore-runtime.ts";
import { QueryLifecycleRuntime } from "../../src/query/lifecycle-runtime.ts";
import { ClaudeRuntimeCore } from "../../src/query-engine.ts";
import { ToolExecutionRuntime, type ToolRuntimeSpec } from "../../src/tools/execution-runtime.ts";
import { ToolResultRuntime } from "../../src/tools/result-runtime.ts";

afterEach(() => {
  delete process.env.ZYRA_DISABLE_E04_QUERY_SOURCE_RUNTIME;
  delete process.env.ZYRA_DISABLE_E04_TOOL_SOURCE_RUNTIME;
  delete process.env.ZYRA_DISABLE_E04_COMPACT_SOURCE_RUNTIME;
});

const compactMessages = (): CompactMessage[] => Array.from(
  { length: 6 },
  (_, index) => ({
    id: `e04-message-${index}`,
    role: index % 2 === 0 ? "user" as const : "assistant" as const,
    content: [{ type: "text" as const, text: `message ${index} ${"context ".repeat(700)}` }],
    createdAt: new Date(Date.parse("2026-07-18T00:00:00.000Z") + index * 1_000).toISOString(),
    turnIndex: index,
    apiRound: index,
    synthetic: false,
    metadata: {},
  }),
);

const compactOptions = (): CompactOptions => ({
  trigger: "auto_threshold",
  model: "e04-model",
  contextWindow: 20_000,
  maxOutputTokens: 2_000,
  targetTokens: 6_000,
  preserveRecentMessages: 1,
  preserveApiRounds: 1,
  systemPrompt: "e04 compact",
  customInstructions: "preserve effects",
  attachments: [],
  querySource: "e04-default-query",
  sessionId: "e04-compact-session",
  summaryMaxAttempts: 1,
});

class E04RuntimeHost implements RuntimeHost {
  readonly events: RuntimeEvent[] = [];
  readonly artifacts: ArtifactReceipt[] = [];
  readonly batches: ToolBatch[] = [];

  async emitEvent(event: RuntimeEvent): Promise<void> {
    this.events.push(event);
  }

  async executeBatch(
    _batch: ToolBatch,
    requests: ToolExecutionRequest[],
  ): Promise<ToolExecutionResponse[]> {
    this.batches.push(structuredClone(_batch));
    return requests.map((request) => ({
      tool_call_id: request.toolCallId,
      ok: true,
      summary: `executed ${request.toolName}`,
      output: { arguments: request.arguments },
      artifacts: [],
      metadata: { host: "e04" },
    }));
  }

  async externalize(request: ArtifactRequest): Promise<ArtifactReceipt> {
    const artifact: ArtifactReceipt = {
      artifact_id: `e04-artifact-${this.artifacts.length + 1}`,
      kind: request.kind,
      uri: `memory://${request.requestId}`,
      title: request.title,
      metadata: request.metadata,
    };
    this.artifacts.push(artifact);
    return artifact;
  }

  isAborted(): boolean {
    return false;
  }
}

const runtimeInput = (overrides: Partial<RuntimeRunInput> = {}): RuntimeRunInput => ({
  runId: "e04-default-run",
  taskId: "e04-default-task",
  workerRequestId: "e04-default-worker",
  sessionId: "e04-default-session",
  messages: [{ role: "user", content: "inspect" }],
  turns: [[{ tool_name: "read_source", arguments: { path: "README.md" } }]],
  tools: [{
    name: "read_source",
    purpose: "read source",
    source: "e04-test",
    input_schema: {
      type: "object",
      required: ["path"],
      properties: { path: { type: "string" } },
    },
    output_schema: {},
    metadata: { read_only: "true", concurrency_safe: "true" },
  }],
  config: {},
  ...overrides,
});

function registerTool(
  runtime: ToolExecutionRuntime,
  name: string,
  readOnly: boolean,
): void {
  runtime.register({
    name,
    namespace: "e04",
    version: "1.0.0",
    description: `${name} source-recovery fixture`,
    inputSchema: { type: "object", additionalProperties: true },
    effects: readOnly ? ["read"] : ["write"],
    risk: readOnly ? "low" : "high",
    readOnly,
    supportsStreaming: false,
    supportsCancellation: true,
    idempotent: readOnly,
    maximumResultChars: 8_000,
    timeoutMs: 10_000,
    concurrencyKey: `e04:${name}`,
    metadata: { source: "claude-code-best/toolOrchestration" },
  } satisfies Omit<ToolRuntimeSpec, "schemaDigest">);
}

function queueCall(
  runtime: ToolExecutionRuntime,
  callId: string,
  toolName: string,
  readOnly: boolean,
): void {
  runtime.createInvocation({
    callId,
    sessionId: "e04-session",
    runId: "e04-run",
    taskId: "e04-task",
    turnId: "e04-turn",
    toolName,
    arguments: {},
    idempotencyKey: `e04:${callId}`,
  });
  if (readOnly) runtime.queueWithoutPermission(callId);
  else runtime.queueWithDelegatedPermission(callId, `permission:${callId}`);
}

test("e04-tool-batch", () => {
  const runtime = new ToolExecutionRuntime();
  registerTool(runtime, "read_source", true);
  registerTool(runtime, "write_source", false);
  queueCall(runtime, "read-before", "read_source", true);
  queueCall(runtime, "write-middle", "write_source", false);
  queueCall(runtime, "read-after", "read_source", true);

  const batches = runtime.runTools(
    "e04-turn",
    ["read-before", "write-middle", "read-after"],
    8,
  );

  expect(batches.map((batch) => batch.callIds)).toEqual([
    ["read-before"],
    ["write-middle"],
    ["read-after"],
  ]);
  expect(batches.map((batch) => batch.readOnly)).toEqual([true, false, true]);
  expect(batches.map((batch) => batch.maximumConcurrency)).toEqual([1, 1, 1]);
});

test("e04-query-reason-observe", () => {
  const runtime = new QueryLifecycleRuntime({
    identity: {
      queryId: "e04-query",
      sessionId: "e04-session",
      runId: "e04-run",
      taskId: "e04-task",
      workerRequestId: "e04-worker",
      parentQueryId: null,
      branchId: "main",
    },
    budget: { maximumTurns: 4 },
  });
  const admission = runtime.ask({
    prompt: "inspect then revise",
    inputId: "e04-input",
    idempotencyKey: "e04-input-key",
    correlationId: "e04-correlation",
    tools: ["read_source"],
    maxTurns: 4,
  });
  expect(admission.accepted).toBeTrue();
  const turn = runtime.startTurn({
    turnId: "e04-turn",
    model: "e04-model",
    messageDigest: "before",
    inputIds: [],
  });
  runtime.planTools(turn.turnId, [{
    toolCallId: "e04-call",
    name: "read_source",
    arguments: { path: "README.md" },
    readOnly: true,
  }]);
  runtime.startTool("e04-call");
  runtime.recordToolResult({
    toolCallId: "e04-call",
    ok: true,
    summary: "read",
    result: { text: "observed" },
  });
  runtime.completeTurn(turn.turnId, {
    messageDigest: "after",
    inputTokens: 10,
    outputTokens: 5,
    stopReason: "tool_use",
  });
  const continuation = runtime.advanceAfterObservation({
    messagesForQuery: [{ role: "user", content: "inspect" }],
    assistantMessages: [{ role: "assistant", content: "calling" }],
    toolResults: [{ role: "tool", content: "observed" }],
    turnCount: 0,
    maxTurns: 4,
  });

  expect(continuation.reason).toBe("next_turn");
  expect(continuation.turn_count).toBe(1);
  expect(continuation.messages).toHaveLength(3);
  expect(runtime.snapshot().transitions.at(-1)?.cause).toBe("query_recursive_call");
});

test("e04-query-model-failure", () => {
  const runtime = new QueryLifecycleRuntime({
    identity: {
      queryId: "e04-query-failure",
      sessionId: "e04-session",
      runId: "e04-run",
      taskId: "e04-task",
      workerRequestId: "e04-worker",
      parentQueryId: null,
      branchId: "main",
    },
  });
  runtime.ask({
    prompt: "fail during sampling",
    inputId: "e04-failure-input",
    idempotencyKey: "e04-failure-input-key",
    correlationId: "e04-failure-correlation",
  });
  const turn = runtime.startTurn({
    turnId: "e04-failure-turn",
    model: "e04-failing-model",
    messageDigest: "before-failure",
    inputIds: [],
  });
  const failed = runtime.failTurn(turn.turnId, "provider unavailable", true);

  expect(failed.status).toBe("failed");
  expect(runtime.shouldStop().reason).toBe("model_error");
  expect(runtime.snapshot().status).toBe("failed");
  expect(runtime.snapshot().transitions.some((item) => item.cause === "query_failed")).toBeTrue();
});

test("e04-query-resume", () => {
  const identity = {
    queryId: "e04-query-resume",
    sessionId: "e04-resume-session",
    runId: "e04-run",
    taskId: "e04-task",
    workerRequestId: "e04-worker",
    parentQueryId: null,
    branchId: "main",
  };
  const runtime = new QueryLifecycleRuntime({ identity });
  runtime.ask({
    prompt: "pause and restore",
    inputId: "e04-resume-input",
    idempotencyKey: "e04-resume-input-key",
    correlationId: "e04-resume-correlation",
  });
  runtime.pause("e04-pause-control");
  const restored = new QueryLifecycleRuntime({ identity });
  restored.restore(runtime.snapshot());
  restored.resume("e04-resume-control", "e04-resume-correlation-id");
  const once = restored.snapshot();
  restored.resume("e04-resume-control-duplicate", "e04-resume-correlation-id");

  expect(once.status).not.toBe("paused");
  expect(restored.snapshot().resumeCorrelationIds).toContain("e04-resume-correlation-id");
  expect(restored.snapshot().transitions.some((item) => item.cause === "query_resumed")).toBeTrue();
  expect(restored.snapshot().revision).toBe(once.revision);
});

test("e04-tool-resume", async () => {
  let externalizeCount = 0;
  const host = {
    async externalize(request: ArtifactRequest) {
      externalizeCount += 1;
      return {
        artifact_id: `artifact-${externalizeCount}`,
        kind: request.kind,
        uri: `memory://artifact-${externalizeCount}`,
        title: request.title,
        metadata: request.metadata,
      };
    },
  };
  const result: ToolExecutionResponse = {
    tool_call_id: "e04-budget-call",
    ok: true,
    summary: "large result",
    output: {
      text: "x".repeat(2_000),
      status: "completed",
      return_code: 0,
    },
    artifacts: [],
    metadata: { termination: "exited" },
  };
  const runtime = new ToolResultRuntime();
  const [first, concurrentDuplicate] = await Promise.all([
    runtime.enforceToolResultBudget(host, result, 128),
    runtime.enforceToolResultBudget(host, result, 128),
  ]);
  expect(externalizeCount).toBe(1);
  expect(first.applied).toBeTrue();
  expect(first.result.output.terminal_facts).toEqual({
    ok: true,
    summary: "large result",
    error: null,
    status: "completed",
    return_code: 0,
    termination: "exited",
  });
  expect(concurrentDuplicate.result).toEqual(first.result);

  const restored = new ToolResultRuntime();
  restored.restore(runtime.snapshot());
  const reapplied = await restored.enforceToolResultBudget(host, result, 128);
  expect(externalizeCount).toBe(1);
  expect(reapplied.reapplied).toBeTrue();
  expect(reapplied.result).toEqual(first.result);
});

test("e04-tool-failure", async () => {
  let releaseEffect!: () => void;
  const effectBlocked = new Promise<void>((resolve) => {
    releaseEffect = () => resolve();
  });
  const host = {
    async externalize(request: ArtifactRequest) {
      await effectBlocked;
      return {
        artifact_id: "conflict-artifact",
        kind: request.kind,
        uri: "memory://conflict-artifact",
        title: request.title,
        metadata: request.metadata,
      };
    },
  };
  const runtime = new ToolResultRuntime();
  const first = runtime.enforceToolResultBudget(host, {
    tool_call_id: "conflict-call",
    ok: true,
    summary: "first",
    output: { text: "a".repeat(1_000) },
    artifacts: [],
    metadata: {},
  }, 64);
  let conflict = "";
  try {
    await runtime.enforceToolResultBudget(host, {
      tool_call_id: "conflict-call",
      ok: true,
      summary: "second",
      output: { text: "b".repeat(1_000) },
      artifacts: [],
      metadata: {},
    }, 64);
  } catch (error) {
    conflict = error instanceof Error ? error.message : String(error);
  }
  expect(conflict).toContain("tool_result_budget_identity_conflict");
  releaseEffect();
  await first;
});

test("e04-compact-failure", async () => {
  const runtime = new ContextCompactionRuntime();
  let summaryAttempts = 0;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const result = await runtime.autoCompactIfNeeded(
      compactMessages(),
      compactOptions(),
      async () => {
        summaryAttempts += 1;
        throw new Error("summary unavailable");
      },
    );
    expect(result).toBeNull();
  }
  expect(summaryAttempts).toBe(3);
  const fourth = await runtime.autoCompactIfNeeded(
    compactMessages(),
    compactOptions(),
    async () => {
      summaryAttempts += 1;
      return "must not execute";
    },
  );
  expect(fourth).toBeNull();
  expect(summaryAttempts).toBe(3);
  const custody = runtime.snapshot().sourceCustody!;
  expect(custody.consecutiveAutoCompactionFailures?.["e04-compact-session"]).toBe(3);
  expect(custody.lastAutoCompaction["e04-compact-session"]?.reason).toBe(
    "circuit_breaker",
  );
});

test("e04-compact-restore", async () => {
  const runtime = new CompactRestoreRuntime({ workspaceRoot: process.cwd() });
  const input = {
    sessionId: "source-session",
    messages: [{ role: "user", content: "resume" }],
    contentReplacements: [{ tool_use_id: "tool-1", content: "bounded" }],
    agentSetting: "code-worker",
    agentColor: "default",
    contextCollapseCommits: [{ commit_id: "collapse-1" }],
    contextCollapseSnapshot: { staged: true },
  };
  const adopted = await runtime.processResumedConversation(input, {
    forkSession: false,
  }, {
    currentSessionId: "fresh-session",
    currentWorkspace: process.cwd(),
    availableAgentSettings: ["code-worker"],
  });
  expect(adopted.sessionId).toBe("source-session");
  expect(adopted.seededContentReplacements).toBeFalse();
  expect(adopted.restoredAgentSetting).toBe("code-worker");
  const forked = await runtime.processResumedConversation(input, {
    forkSession: true,
  }, {
    currentSessionId: "fresh-session",
    currentWorkspace: process.cwd(),
    availableAgentSettings: ["code-worker"],
  });
  expect(forked.sessionId).toBe("fresh-session");
  expect(forked.seededContentReplacements).toBeTrue();
  expect(forked.contextCollapseCommits).toHaveLength(1);

  const restored = new CompactRestoreRuntime({ workspaceRoot: process.cwd() });
  restored.restore(runtime.snapshot());
  expect(await restored.processResumedConversation(input, {
    forkSession: true,
  }, {
    currentSessionId: "fresh-session",
    currentWorkspace: process.cwd(),
    availableAgentSettings: ["code-worker"],
  })).toEqual(forked);
});

test("e04-compact-disable", async () => {
  process.env.ZYRA_DISABLE_E04_COMPACT_SOURCE_RUNTIME = "1";
  const custody = new CompactionSourceCustodyRuntime();
  const plan = custody.autoCompactIfNeeded({
    sessionId: "disabled-compact",
    currentTokens: 10_000,
    contextWindow: 10_000,
    reservedTokens: 0,
    minimumFreeTokens: 1_000,
    messages: compactMessages(),
    keepLastMessages: 1,
  });
  expect(plan.reason).toBe("disabled");
  const compact = new ContextCompactionRuntime();
  await expect(compact.autoCompactIfNeeded(
    compactMessages(),
    compactOptions(),
    async () => "summary",
  )).rejects.toThrow("e04_compact_source_runtime_disabled");
  let compactError = "";
  try {
    await compact.compactConversation(compactMessages(), compactOptions(), async () => "summary");
  } catch (error) {
    compactError = error instanceof Error ? error.message : String(error);
  }
  expect(compactError).toContain("e04_compact_source_runtime_disabled");
  const restore = new CompactRestoreRuntime({ workspaceRoot: process.cwd() });
  expect(restore.processResumedConversation({
    sessionId: "disabled",
    messages: [],
  }, {
    forkSession: false,
  }, {
    currentSessionId: "disabled",
    currentWorkspace: process.cwd(),
  })).rejects.toThrow("e04_compact_source_runtime_disabled");
});

test("E04-B source-runtime disable switches fail the default semantic owners", async () => {
  const tools = new ToolExecutionRuntime();
  registerTool(tools, "read_source", true);
  queueCall(tools, "disabled-read", "read_source", true);
  process.env.ZYRA_DISABLE_E04_TOOL_SOURCE_RUNTIME = "1";
  expect(() => tools.runTools("e04-turn", ["disabled-read"])).toThrow(
    "e04_tool_source_runtime_disabled",
  );

  const query = new QueryLifecycleRuntime({
    identity: {
      queryId: "disabled-query",
      sessionId: "e04-session",
      runId: "e04-run",
      taskId: "e04-task",
      workerRequestId: "e04-worker",
      parentQueryId: null,
      branchId: "main",
    },
  });
  process.env.ZYRA_DISABLE_E04_QUERY_SOURCE_RUNTIME = "1";
  expect(() => query.ask({
    prompt: "blocked",
    inputId: "disabled-input",
    idempotencyKey: "disabled-input-key",
    correlationId: "disabled-correlation",
  })).toThrow("e04_query_source_runtime_disabled");
});

test("E04-B exact disconnect switches kill ClaudeRuntimeCore default paths", async () => {
  const cases: Array<{
    variable: string;
    expected: string;
    input: RuntimeRunInput;
  }> = [
    {
      variable: "ZYRA_DISABLE_E04_QUERY_SOURCE_RUNTIME",
      expected: "e04_query_source_runtime_disabled",
      input: runtimeInput(),
    },
    {
      variable: "ZYRA_DISABLE_E04_TOOL_SOURCE_RUNTIME",
      expected: "e04_tool_source_runtime_disabled",
      input: runtimeInput(),
    },
    {
      variable: "ZYRA_DISABLE_E04_COMPACT_SOURCE_RUNTIME",
      expected: "e04_compact_source_runtime_disabled",
      input: runtimeInput({
        config: { runtimeConstraints: { force_compact_restore: true } },
      }),
    },
  ];
  for (const item of cases) {
    process.env[item.variable] = "1";
    let message = "";
    try {
      await new ClaudeRuntimeCore().run(item.input, new E04RuntimeHost());
    } catch (error) {
      message = error instanceof Error ? error.message : String(error);
    } finally {
      delete process.env[item.variable];
    }
    expect(message).toContain(item.expected);
  }
});

test("e04-query-disable", async () => {
  const host = new E04RuntimeHost();
  const result = await new ClaudeRuntimeCore().run(runtimeInput(), host);
  expect(result.ok).toBeTrue();
  expect(host.events.some((event) => event.phase === "upstream_query_continuation")).toBeTrue();
  const e01 = result.sessionSnapshot.e01Runtime as JsonObject;
  const query = e01.query as JsonObject;
  expect(Number(query.revision)).toBeGreaterThan(0);
  expect(query.status).toBe("completed");
});

test("e04-tool-disable", async () => {
  const host = new E04RuntimeHost();
  const result = await new ClaudeRuntimeCore().run(runtimeInput(), host);
  expect(result.ok).toBeTrue();
  expect(host.batches).toHaveLength(1);
  expect(host.batches[0]?.steps).toHaveLength(1);
});

test("e04-compact-boundary e04-compact-disable", async () => {
  const host = new E04RuntimeHost();
  const result = await new ClaudeRuntimeCore().run(runtimeInput({
    config: { runtimeConstraints: { force_compact_restore: true } },
  }), host);
  expect(result.ok).toBeTrue();
  expect(result.contextCompactionCount).toBeGreaterThan(0);
  expect(host.artifacts.some((artifact) => artifact.title === "CodeWorker context compaction")).toBeTrue();
});
