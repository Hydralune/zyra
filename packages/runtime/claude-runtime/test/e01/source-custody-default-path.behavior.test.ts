import { describe, expect, test } from "bun:test";

import {
  MAX_NON_STREAMING_TOKENS,
  ProviderModelRuntime,
  type ProviderRequestOptions,
  type ProviderTransport,
} from "../../src/provider/model-runtime.ts";
import {
  ContextCompactionRuntime,
  type CompactMessage,
  type CompactOptions,
} from "../../src/compact/context-runtime.ts";

const compactMessages = (): CompactMessage[] => [
  {
    id: "user-1",
    role: "user",
    content: [{ type: "text", text: "inspect the workspace" }],
    createdAt: "2026-07-16T00:00:00.000Z",
    turnIndex: 0,
    apiRound: 0,
    synthetic: false,
    metadata: {},
  },
  {
    id: "assistant-1",
    role: "assistant",
    content: [{ type: "tool_use", id: "read-1", name: "read_file", input: { path: "/workspace/read.txt" } }],
    createdAt: "2026-07-16T00:00:01.000Z",
    turnIndex: 0,
    apiRound: 0,
    synthetic: false,
    metadata: {},
  },
  {
    id: "tool-1",
    role: "tool",
    content: [{
      type: "tool_result",
      toolUseId: "read-1",
      content: "source:" + "x".repeat(8_000),
      isError: false,
      createdAt: "2026-07-16T00:00:02.000Z",
      compacted: false,
    }],
    createdAt: "2026-07-16T00:00:02.000Z",
    turnIndex: 0,
    apiRound: 0,
    synthetic: false,
    metadata: {},
  },
  {
    id: "user-2",
    role: "user",
    content: [{ type: "text", text: "continue with the result" }],
    createdAt: "2026-07-16T00:00:03.000Z",
    turnIndex: 1,
    apiRound: 1,
    synthetic: false,
    metadata: {},
  },
];

const compactOptions = (): CompactOptions => ({
  trigger: "manual",
  model: "claude-sonnet-4-5",
  contextWindow: 200_000,
  maxOutputTokens: 8_192,
  targetTokens: 12_000,
  preserveRecentMessages: 1,
  preserveApiRounds: 1,
  systemPrompt: "system",
  customInstructions: "preserve decisions",
  attachments: [
    { kind: "file", path: "/workspace/read.txt", name: "read.txt", content: "a".repeat(30_000) },
    { kind: "file", path: "/workspace/node_modules/secret.txt", name: "secret", content: "secret" },
  ],
  querySource: "interactive-main",
  sessionId: "v9-session",
  microcompactIntervalMs: 1,
  cacheRoot: ".cache/compact",
  summaryMaxAttempts: 2,
  now: "2026-07-16T00:10:00.000Z",
});

const providerOptions = (): ProviderRequestOptions => ({
  model: "claude-haiku-3-5",
  messages: [{ role: "user", content: [{ type: "text", text: "respond briefly" }] }],
  system: [{ type: "text", text: "system" }],
  tools: [],
  maxTokens: MAX_NON_STREAMING_TOKENS + 10_000,
  temperature: null,
  topP: null,
  stopSequences: [],
  stream: false,
  thinking: { enabled: false, budgetTokens: 0, effort: null },
  metadata: { oneHourCacheEnabled: true },
  betaHeaders: [],
  querySource: "interactive-main",
  sessionId: "provider-v9",
  runId: "run-v9",
  taskId: "task-v9",
});

describe("E01 V9 default-path source custody", () => {
  test("e01.v9.compaction-default-path-custody-is-canonical", async () => {
    const runtime = new ContextCompactionRuntime();
    runtime.configureSessionMemory({ enabled: true, minimumMessages: 2, minimumTokens: 1_000 });
    let attempts = 0;
    const result = await runtime.compactConversation(compactMessages(), compactOptions(), async () => {
      attempts += 1;
      return attempts === 1 ? "" : "canonical compact summary";
    });
    const snapshot = runtime.snapshot();
    expect(runtime.getEffectiveContextWindowSize("local-model", 200_000)).toBe(128_000);
    expect(attempts).toBe(2);
    expect(result.boundary.boundaryId).toContain("partial-");
    expect(snapshot.sourceCustody?.lastMicrocompactAt["v9-session"]).toBeDefined();
    expect(snapshot.sourceCustody?.pendingEdits["v9-session"]).toEqual(["read-1"]);
    const restored = new ContextCompactionRuntime();
    restored.restore(snapshot);
    expect(restored.snapshot().sourceCustody).toEqual(snapshot.sourceCustody!);
  });

  test("e01.v9.post-compact-restore-follows-read-lineage-and-budgets", () => {
    const runtime = new ContextCompactionRuntime();
    const messages = compactMessages();
    expect(runtime.collectReadToolFilePaths(messages)).toEqual(["/workspace/read.txt"]);
    expect(runtime.shouldExcludeFromPostCompactRestore("/workspace/node_modules/secret.txt", messages)).toBe(true);
    const truncated = runtime.truncateToTokens("z".repeat(50_000), 100);
    expect(truncated.truncated).toBe(true);
    const attachments = runtime.createPostCompactAttachments(compactOptions().attachments, messages);
    expect(attachments).toHaveLength(1);
    expect(attachments[0]?.path).toBe("/workspace/read.txt");
  });

  test("e01.v9.provider-default-path-owns-cache-custody-and-parameters", () => {
    const runtime = new ProviderModelRuntime();
    runtime.configureEndpoint({ provider: "bedrock" });
    const prepared = runtime.prepare(providerOptions());
    const body = prepared.body as Record<string, unknown>;
    expect(prepared.sourceCustody.breakpointCount).toBeGreaterThan(0);
    expect(JSON.stringify(body.system)).toContain("cache_control");
    expect(body.max_tokens).toBeLessThanOrEqual(MAX_NON_STREAMING_TOKENS);
    expect(runtime.resolveModel("haiku").maxOutputTokens).toBe(8_192);
    const snapshot = runtime.snapshot();
    expect(snapshot.sourceCustody?.oneHourSessions).toEqual(["provider-v9"]);
    const restored = new ProviderModelRuntime();
    restored.restore(snapshot);
    expect(restored.snapshot().sourceCustody).toEqual(snapshot.sourceCustody!);
  });

  test("e01.v9.provider-query-wrapper-settles-real-request-state", async () => {
    const runtime = new ProviderModelRuntime();
    const prepared = runtime.prepare(providerOptions());
    const transport: ProviderTransport = {
      async execute() {
        return {
          status: 200,
          headers: { "request-id": "provider-request-v9" },
          body: {
            id: "message-v9",
            type: "message",
            role: "assistant",
            content: [{ type: "text", text: "ok" }],
            model: "claude-haiku-3-5",
            stop_reason: "end_turn",
            stop_sequence: null,
            usage: { input_tokens: 1, output_tokens: 1 },
          },
        };
      },
    };
    const response = await runtime.queryHaiku(prepared, transport);
    expect(response.content[0]).toEqual({ type: "text", text: "ok" });
    expect(runtime.requestState(prepared.requestId).state).toBe("completed");
    expect(runtime.queryWithModel).toBeDefined();
  });
});
