import { expect, test } from "bun:test";
import { applyToolResultBudget } from "../../src/budget.ts";
import {
  adjustIndexToPreserveApiInvariants,
  ContextCompactionRuntime,
  type CompactMessage,
  type CompactOptions,
} from "../../src/compact/context-runtime.ts";
import { E01RuntimeCoordinator } from "../../src/e01/coordinator.ts";
import { command, identity, Journal } from "../../src/e01/kernel.ts";
import { consumeProviderStream } from "../../src/provider/model-runtime.ts";
import { ProviderRecoveryRuntime } from "../../src/provider/recovery-runtime.ts";
import { ProviderTelemetryRuntime } from "../../src/provider/telemetry-runtime.ts";
import { QueryLifecycleRuntime } from "../../src/query/lifecycle-runtime.ts";
import { RuntimeToolRegistry, scheduleToolBatches } from "../../src/tools.ts";

const timestamp = "2026-07-15T00:00:00.000Z";

function journalCommand(journal: Journal, options: { effect?: boolean } = {}) {
  return command(journal, "mutation", "contract", { value: 1 }, {
    identity: identity(journal),
    readSet: ["mutation.value"],
    writeSet: ["mutation.value"],
    requiresEffect: options.effect === true,
  });
}

function query(maximumTurns: number | null = null): QueryLifecycleRuntime {
  return new QueryLifecycleRuntime({
    identity: {
      queryId: "mutation-query",
      sessionId: "mutation-session",
      runId: "mutation-run",
      taskId: "mutation-task",
      workerRequestId: "mutation-worker",
      parentQueryId: null,
      branchId: "main",
    },
    budget: { maximumTurns },
  });
}

function admittedTurn(runtime: QueryLifecycleRuntime) {
  runtime.admit({
    inputId: "mutation-input",
    kind: "prompt",
    content: "perform the mutation contract task",
    priority: "interactive",
    idempotencyKey: "mutation-input-key",
    correlationId: "mutation-correlation",
    metadata: {},
    createdAt: timestamp,
  });
  return runtime.startTurn({ model: "mutation-model", messageDigest: "digest-before", inputIds: [] });
}

function message(id: string, apiRound: number, text: string, role: "user" | "assistant" = "user"): CompactMessage {
  return {
    id,
    role,
    content: [{ type: "text", text }],
    createdAt: new Date(Date.parse(timestamp) + apiRound * 1_000).toISOString(),
    turnIndex: apiRound,
    apiRound,
    synthetic: false,
    metadata: {},
  };
}

function compactMessages(): CompactMessage[] {
  return Array.from({ length: 6 }, (_, index) => message(
    `compact-${index}`,
    index,
    `message ${index} ${"content ".repeat(30)}`,
    index % 2 === 0 ? "user" : "assistant",
  ));
}

function compactOptions(): CompactOptions {
  return {
    trigger: "manual",
    model: "mutation-model",
    contextWindow: 20_000,
    maxOutputTokens: 2_000,
    targetTokens: 8_000,
    preserveRecentMessages: 1,
    preserveApiRounds: 1,
    systemPrompt: "mutation contract",
    customInstructions: "preserve durable facts",
    attachments: [],
    querySource: "mutation-test",
    now: timestamp,
  };
}

function retryContext(maxRetries = 3): ProviderRecoveryRuntime {
  const runtime = new ProviderRecoveryRuntime();
  runtime.createContext("mutation-request", "anthropic", "mutation-model", 16_000, maxRetries, 1_000);
  return runtime;
}

const model = {
  id: "mutation-model",
  provider: "anthropic",
  inputPricePerMillion: 1,
  outputPricePerMillion: 2,
  cacheReadPricePerMillion: 0.1,
  cacheWritePricePerMillion: 1.25,
} as any;

const usage = {
  inputTokens: 100,
  outputTokens: 20,
  cacheReadInputTokens: 10,
  cacheCreationInputTokens: 5,
  serverToolUseTokens: 0,
};

test("e01.mutation.restore-before-bootstrap", () => {
  const runtime = new Journal("run-before", "session-atomic");
  const corrupted = runtime.snapshot();
  corrupted.checksum = "corrupted";
  expect(() => runtime.restore(corrupted, "run-after")).toThrow("snapshot_checksum");
  expect(runtime.runId).toBe("run-before");
});

test("e01.mutation.revision-monotonic", () => {
  const runtime = new Journal("run-revision", "session-revision");
  const value = journalCommand(runtime);
  runtime.prepare(value);
  const before = runtime.revision;
  const receipt = runtime.commit(value, { mutation: { value: 1 } }, []);
  expect(receipt.revisionAfter).toBeGreaterThan(before);
  expect(runtime.revision).toBe(receipt.revisionAfter);
});

test("e01.mutation.transition-unique", () => {
  const runtime = new Journal("run-identity", "session-identity");
  expect(identity(runtime).transitionId).not.toBe(identity(runtime).transitionId);
});

test("e01.mutation.payload-digest", () => {
  const runtime = new Journal("run-digest", "session-digest");
  const value = journalCommand(runtime);
  value.payload.value = 2;
  expect(() => runtime.prepare(value)).toThrow("payload_digest");
});

test("e01.mutation.pending-committed", () => {
  const runtime = new Journal("run-pending", "session-pending");
  const value = journalCommand(runtime);
  runtime.prepare(value);
  runtime.commit(value, { mutation: { value: 1 } }, []);
  expect(runtime.pending()).toHaveLength(0);
  expect(runtime.committed()).toHaveLength(1);
});

test("e01.mutation.effect-fence", () => {
  const runtime = new Journal("run-effect", "session-effect");
  const value = journalCommand(runtime, { effect: true });
  runtime.prepare(value);
  const first = runtime.effect(value, { result: "first" });
  const repeated = runtime.effect(value, { result: "second" });
  expect(repeated.resultDigest).toBe(first.resultDigest);
  expect(runtime.snapshot().effects).toHaveLength(1);
});

test("e01.mutation.lost-ack", () => {
  const runtime = new Journal("run-ack", "session-ack");
  const value = journalCommand(runtime);
  runtime.prepare(value);
  const committed = runtime.commit(value, { mutation: { value: 1 } }, []);
  const count = runtime.committed().length;
  const revision = runtime.revision;
  expect(runtime.prepare(value).duplicate).toBeTrue();
  expect(runtime.revision).toBe(revision);
  runtime.ack(committed.identity.transitionId);
  runtime.ack(committed.identity.transitionId);
  expect(runtime.committed()).toHaveLength(count);
});

test("E01 effect snapshot restores multiple committed revisions for one aggregate", async () => {
  const first = new E01RuntimeCoordinator("effect-run-a", "effect-session", "effect-task", "effect-worker");
  await first.bootstrap();
  first.recordRuntimeEvent("second_commit", { value: 2 });
  const snapshot = first.snapshot();
  const second = new E01RuntimeCoordinator("effect-run-b", "effect-session", "effect-task", "effect-worker");
  expect(() => second.restore(snapshot)).not.toThrow();
  expect(second.protocol.snapshot().transactions.filter((item) => item.state === "committed")).toHaveLength(2);
});

test("E01 provider events enter prompt usage and recovery state owners", async () => {
  const runtime = new E01RuntimeCoordinator("provider-run", "provider-session", "provider-task", "provider-worker");
  await runtime.bootstrap();
  runtime.recordRuntimeEvent("model_request_prepared", {
    provider_request: {
      request_id: "provider-request-1",
      provider: "compatible",
      model: "provider-model",
      system: [{ text: "system" }],
      tools: [{ name: "read" }],
      messages: [{ role: "user", content: "work" }],
    },
  });
  runtime.recordRuntimeEvent("model_stream_report", {
    model_stream: {
      request_id: "provider-request-1",
      provider: "compatible",
      model: "provider-model",
      ok: false,
      status: 429,
      decision: "retry",
      fallback_model: "provider-fallback",
      error: "rate limit",
      usage: { input_tokens: 20, output_tokens: 3, cache_read_input_tokens: 5 },
    },
  });
  const snapshot = runtime.snapshot();
  expect(snapshot.telemetry.prompts).toHaveLength(1);
  expect(snapshot.telemetry.samples).toHaveLength(1);
  expect(snapshot.telemetry.samples[0].usage.inputTokens).toBe(20);
  expect(snapshot.recovery.contexts).toHaveLength(1);
  expect(snapshot.recovery.contexts[0].attempt).toBe(1);
});

test("e01.mutation.corrupt-snapshot", () => {
  const runtime = new Journal("run-checksum", "session-checksum");
  const corrupted = runtime.snapshot();
  corrupted.checksum = "invalid";
  expect(() => runtime.restore(corrupted, "run-restored")).toThrow("snapshot_checksum");
});

test("e01.mutation.session-scope", () => {
  const source = new Journal("run-source", "session-source");
  const target = new Journal("run-target", "session-target");
  expect(() => target.restore(source.snapshot(), "run-restored")).toThrow("session_mismatch");
});

test("e01.mutation.outbox-once", () => {
  const runtime = new Journal("run-outbox", "session-outbox");
  const value = journalCommand(runtime);
  runtime.prepare(value);
  const committed = runtime.commit(value, { mutation: { value: 1 } }, [{ kind: "mutation" }]);
  const first = runtime.project(committed.outboxIds[0], timestamp);
  const repeated = runtime.project(committed.outboxIds[0], "2026-07-15T00:01:00.000Z");
  expect(repeated.deliveredAt).toBe(first.deliveredAt);
});

test("e01.mutation.turn-limit", () => {
  const runtime = query(1);
  const turn = admittedTurn(runtime);
  runtime.completeTurn(turn.turnId, { messageDigest: "digest-after", inputTokens: 1, outputTokens: 1, stopReason: "continue" });
  expect(runtime.shouldStop().reason).toBe("max_turns");
});

test("e01.mutation.cancel", () => {
  const runtime = query();
  runtime.cancel("cancel-control", "user_cancelled");
  expect(runtime.snapshot().control.abortRequested).toBeTrue();
});

test("e01.mutation.empty-turn", () => {
  const runtime = query();
  const turn = admittedTurn(runtime);
  expect(() => runtime.completeTurn(turn.turnId, { messageDigest: "", inputTokens: 0, outputTokens: 0, stopReason: "end_turn" })).toThrow("message digest");
});

test("e01.mutation.query-revision", () => {
  const runtime = query();
  runtime.admit({
    inputId: "revision-input",
    kind: "prompt",
    content: "revision",
    priority: "normal",
    idempotencyKey: "revision-key",
    correlationId: "revision-correlation",
    metadata: {},
    createdAt: timestamp,
  });
  const snapshot = runtime.snapshot();
  expect(snapshot.transitions.length).toBeGreaterThan(0);
  expect(snapshot.transitions.every((item) => item.revision > 0)).toBeTrue();
});

test("e01.mutation.failure-route", () => {
  const runtime = query();
  const turn = admittedTurn(runtime);
  runtime.failTurn(turn.turnId, "fatal provider failure", true);
  expect(runtime.snapshot().status).toBe("failed");
  expect(runtime.shouldStop().stop).toBeTrue();
});

test("e01.mutation.compact-threshold", () => {
  const runtime = new ContextCompactionRuntime();
  const messages = Array.from({ length: 4 }, (_, index) => message(`threshold-${index}`, index, "x".repeat(10_000)));
  expect(runtime.calculateTokenWarningState(messages, 4_096, 1_024).shouldAutoCompact).toBeTrue();
});

test("e01.mutation.compact-tool-pair", () => {
  const messages: CompactMessage[] = [
    {
      ...message("tool-use", 0, "", "assistant"),
      content: [{ type: "tool_use", id: "tool-1", name: "read", input: {} }],
    },
    {
      ...message("tool-result", 1, "", "user"),
      content: [{ type: "tool_result", toolUseId: "tool-1", content: "ok", isError: false, createdAt: timestamp, compacted: false }],
    },
  ];
  expect(adjustIndexToPreserveApiInvariants(messages, 1)).toBe(0);
});

test("e01.mutation.compact-suffix", async () => {
  const runtime = new ContextCompactionRuntime();
  const messages = compactMessages();
  const result = await runtime.compactConversation(messages, compactOptions(), async () => "durable summary");
  expect(result.messages.some((item) => item.id === messages.at(-1)?.id)).toBeTrue();
});

test("e01.mutation.compact-restore", () => {
  const source = new ContextCompactionRuntime(123_456);
  const snapshot = source.snapshot();
  const target = new ContextCompactionRuntime(65_432);
  target.restore(snapshot);
  expect(target.snapshot().tokenRuntime).toEqual(snapshot.tokenRuntime);
});

test("e01.mutation.compact-cleanup", async () => {
  const runtime = new ContextCompactionRuntime();
  await runtime.compactConversation(compactMessages(), compactOptions(), async () => "durable summary");
  expect(runtime.snapshot().cleanup.generation).toBeGreaterThan(0);
});

test("e01.mutation.retry-delay", () => {
  const runtime = retryContext();
  const plan = runtime.plan("mutation-request", { status: 429, message: "rate limit" }, [], 2_000);
  expect(plan.action).toBe("retry");
  expect(plan.delayMs).toBeGreaterThan(0);
});

test("e01.mutation.retry-class", () => {
  const runtime = retryContext();
  const plan = runtime.plan("mutation-request", { status: 401, message: "unauthorized api key" }, [], 2_000);
  expect(plan.action).toBe("stop");
});

test("e01.mutation.max-retries", () => {
  const runtime = retryContext(0);
  const plan = runtime.plan("mutation-request", { status: 429, message: "rate limit" }, [], 2_000);
  expect(plan.action).toBe("stop");
  expect(plan.reason).toBe("retry_limit_exhausted");
});

test("e01.mutation.fallback", () => {
  const runtime = retryContext();
  const plan = runtime.plan("mutation-request", { status: 529, message: "overloaded" }, ["fallback-model"], 2_000);
  expect(plan.action).toBe("retry");
  expect(plan.nextModel).toBe("mutation-model");
});

test("e01.mutation.stream-final", async () => {
  async function* incomplete() {
    yield `event: message_start\ndata: ${JSON.stringify({
      type: "message_start",
      message: { id: "provider-message", model: "mutation-model", usage: { input_tokens: 1 } },
    })}\n\n`;
  }
  let message = "";
  try {
    await consumeProviderStream("mutation-stream", incomplete());
  } catch (error) {
    message = error instanceof Error ? error.message : String(error);
  }
  expect(message).toContain("message_stop");
});

test("e01.mutation.usage", () => {
  const runtime = new ProviderTelemetryRuntime();
  runtime.recordUsage({
    requestId: "usage-request",
    sessionId: "usage-session",
    runId: "usage-run",
    taskId: "usage-task",
    model,
    usage,
    durationMs: 10,
    firstTokenMs: 2,
    success: true,
    stopReason: "end_turn",
    errorCode: null,
    createdAt: timestamp,
  });
  const cost = runtime.sessionCost("usage-session");
  expect(cost.requestCount).toBe(1);
  expect(cost.inputTokens).toBe(usage.inputTokens);
});

test("e01.mutation.cache-lineage", () => {
  const runtime = new ProviderTelemetryRuntime();
  const base = {
    sessionId: "cache-session",
    agentId: null,
    model: "mutation-model",
    system: { text: "system" },
    tools: [],
    cacheTtlMs: 60_000,
  };
  runtime.recordPromptState({ ...base, requestId: "cache-1", messages: [{ text: "a" }], recordedAt: timestamp });
  const second = runtime.recordPromptState({ ...base, requestId: "cache-2", messages: [{ text: "b" }], recordedAt: "2026-07-15T00:00:01.000Z" });
  const third = runtime.recordPromptState({ ...base, requestId: "cache-3", messages: [{ text: "b" }], recordedAt: "2026-07-15T00:00:02.000Z" });
  expect(second.cacheBreak?.kind).toBe("messages_rewritten");
  expect(third.cacheBreak).toBeNull();
});

test("e01.mutation.tool-schema", () => {
  const registry = new RuntimeToolRegistry([{
    name: "write",
    description: "write a value",
    input_schema: { type: "object", required: ["value"], properties: { value: { type: "string" } } },
    metadata: {},
  } as any]);
  expect(registry.validate({ tool_name: "write", arguments: {} } as any)).not.toHaveLength(0);
});

test("e01.mutation.write-serialization", () => {
  const registry = new RuntimeToolRegistry([
    { name: "read", description: "read", input_schema: { type: "object" }, metadata: { read_only: "true", concurrency_safe: "true" } },
    { name: "write", description: "write", input_schema: { type: "object" }, metadata: {} },
  ] as any);
  const batches = scheduleToolBatches(registry, [
    { tool_name: "read", arguments: {} },
    { tool_name: "write", arguments: {} },
  ] as any, 0, 4);
  expect(batches).toHaveLength(2);
  expect(batches[0].executionMode).toBe("concurrent_read_only");
  expect(batches[1].executionMode).toBe("serial_non_read_only");
  expect(batches[1].steps).toHaveLength(1);
});

test("e01.mutation.result-budget", async () => {
  let externalized = 0;
  const host = {
    async externalize() {
      externalized += 1;
      return {
        artifact_id: "artifact-budget",
        title: "budget",
        kind: "structured_data",
        extension: ".json",
        path: "artifacts/budget.json",
        digest: "sha256:budget",
        size_bytes: 1_000,
        metadata: {},
      };
    },
  } as any;
  const result = await applyToolResultBudget(host, {
    tool_call_id: "budget-call",
    output: { content: "x".repeat(1_000) },
    artifacts: [],
    metadata: {},
  } as any, 64);
  expect(result.applied).toBeTrue();
  expect(result.artifact?.artifact_id).toBe("artifact-budget");
  expect(externalized).toBe(1);
});
