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
    { kind: "file", path: "/workspace/unread.txt", name: "unread.txt", content: "unread" },
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

const providerSuccess = (id: string): import("../../src/provider/model-runtime.js").ProviderTransportResponse => ({
  status: 200,
  headers: { "request-id": id },
  body: {
    id,
    type: "message",
    role: "assistant",
    content: [{ type: "text", text: "ok" }],
    model: "claude-haiku-3-5",
    stop_reason: "end_turn",
    stop_sequence: null,
    usage: { input_tokens: 1, output_tokens: 1 },
  },
});

const withEnvironment = async <T>(
  updates: Readonly<Record<string, string | undefined>>,
  run: () => Promise<T>,
): Promise<T> => {
  const before = Object.fromEntries(Object.keys(updates).map((name) => [name, process.env[name]]));
  try {
    for (const [name, value] of Object.entries(updates)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
    return await run();
  } finally {
    for (const [name, value] of Object.entries(before)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
};

describe("E01 V10 default-path source custody", () => {
  test("e01.v10.compaction-default-path-custody-is-canonical", async () => {
    const runtime = new ContextCompactionRuntime();
    runtime.configureSessionMemory({ enabled: true, minimumMessages: 2, minimumTokens: 1_000 });
    let attempts = 0;
    const result = await runtime.compactConversation(compactMessages(), compactOptions(), async () => {
      attempts += 1;
      return attempts === 1 ? "" : "canonical compact summary";
    });
    const snapshot = runtime.snapshot();
    expect(runtime.getEffectiveContextWindowSize("claude-sonnet-4-5", 200_000)).toBe(180_000);
    expect(attempts).toBe(2);
    expect(result.boundary.boundaryId).toContain("memory-");
    expect(result.boundary.sourceCustody?.source).toBe("session-memory");
    expect(snapshot.sourceCustody?.lastMicrocompactAt["v9-session"]).toBeDefined();
    expect(snapshot.sourceCustody?.pendingEdits["v9-session"]).toBeUndefined();
    expect(snapshot.sourceCustody?.consumedEdits["v9-session"]).toEqual(["read-1"]);
    expect(snapshot.sourceCustody?.lastAutoCompaction["v9-session"]?.required).toBe(false);
    expect(snapshot.sourceCustody?.lastPartialBoundary["v9-session"]?.preservedSegment).toHaveLength(1);
    const sourceBoundaryId = snapshot.sourceCustody?.lastSessionMemory["v9-session"]?.boundary.boundaryId;
    expect(sourceBoundaryId).toBeDefined();
    expect(result.boundary.boundaryId.startsWith(`${sourceBoundaryId}-`)).toBe(true);
    expect(snapshot.sourceCustody?.lastSummaryStream["v9-session"]?.attempts).toBe(2);
    expect(snapshot.sessionMemory.sourceCustodyBoundaryId).toBe(result.boundary.boundaryId);
    expect(snapshot.sessionMemory.consumedCacheEditIds).toEqual(["read-1"]);
    const restored = new ContextCompactionRuntime();
    restored.restore(snapshot);
    expect(restored.snapshot().sourceCustody).toEqual(snapshot.sourceCustody!);
  });

  test("e01.v10.compaction-auto-plan-controls-default-path", async () => {
    const runtime = new ContextCompactionRuntime();
    const result = await runtime.autoCompactIfNeeded(
      compactMessages(),
      { ...compactOptions(), trigger: "auto_threshold", contextWindow: 20_000 },
      async () => "automatic summary",
    );
    expect(result?.boundary.trigger).toBe("auto_threshold");
    expect(runtime.snapshot().sourceCustody?.lastAutoCompaction["v9-session"]?.required).toBe(true);
  });

  test("e01.v10.compaction-over-wide-preservation-keeps-a-valid-source-boundary", async () => {
    const runtime = new ContextCompactionRuntime();
    const result = await runtime.compactConversation(
      compactMessages(),
      {
        ...compactOptions(),
        preserveRecentMessages: compactMessages().length + 4,
      },
      async () => "bounded compact summary",
    );

    expect(result.changed).toBe(true);
    expect(result.boundary.sourceMessageIds.length).toBeGreaterThan(0);
    expect(result.boundary.preservedMessageIds.length).toBeGreaterThan(0);
  });

  test("e01.v10.post-compact-restore-follows-read-lineage-and-budgets", () => {
    const runtime = new ContextCompactionRuntime();
    const messages = compactMessages();
    expect(runtime.collectReadToolFilePaths(messages)).toEqual(["/workspace/read.txt"]);
    expect(runtime.shouldExcludeFromPostCompactRestore("/workspace/node_modules/secret.txt", messages)).toBe(true);
    expect(runtime.shouldExcludeFromPostCompactRestore("/workspace/read.txt", messages)).toBe(true);
    expect(runtime.shouldExcludeFromPostCompactRestore("/workspace/unread.txt", messages)).toBe(false);
    const truncated = runtime.truncateToTokens("z".repeat(50_000), 100);
    expect(truncated.truncated).toBe(true);
    expect(runtime.truncateToTokens(truncated.content, 100).content).toBe(truncated.content);
    const attachments = runtime.createPostCompactAttachments(compactOptions().attachments, messages);
    expect(attachments).toHaveLength(1);
    expect(attachments[0]?.path).toBe("/workspace/unread.txt");
  });

  test("e01.v11.provider-client-factory-executes-auth-and-retry", async () => {
    await withEnvironment({
      CLAUDE_CODE_USE_FOUNDRY: "1",
      CLAUDE_CODE_USE_BEDROCK: undefined,
      CLAUDE_CODE_USE_VERTEX: undefined,
      ANTHROPIC_FOUNDRY_RESOURCE: "zyra-resource",
      AZURE_ACCESS_TOKEN: "azure-live-token",
    }, async () => {
      const runtime = new ProviderModelRuntime();
      runtime.configureCredential({ kind: "oauth", accessToken: "azure-live-token", fingerprint: "azure-fingerprint" });
      const prepared = runtime.prepare({ ...providerOptions(), metadata: { providerMaxRetries: 1 } });
      expect(prepared.sourceCustody.client?.transport).toBe("foundry");
      expect(prepared.endpoint.baseUrl).toBe("https://zyra-resource.services.ai.azure.com");
      const snapshot = runtime.snapshot();
      expect(JSON.stringify(snapshot)).not.toContain("azure-live-token");
      expect(snapshot.requests[0]?.request.headers.authorization).toBe("[redacted]");
      const restored = new ProviderModelRuntime();
      restored.restore(snapshot, {
        kind: "oauth",
        accessToken: "azure-live-token",
        fingerprint: "azure-fingerprint",
      });
      const requests: Array<{ url: string; headers: Readonly<Record<string, string>> }> = [];
      let attempts = 0;
      const response = await restored.queryHaiku(snapshot.requests[0]!.request, {
        async execute(request) {
          requests.push(request);
          attempts += 1;
          return attempts === 1
            ? { status: 429, headers: { "retry-after": "0" }, body: { error: { message: "retry" } } }
            : providerSuccess("foundry-default");
        },
      });
      expect(response.content[0]).toEqual({ type: "text", text: "ok" });
      expect(requests).toHaveLength(2);
      expect(requests[0]?.url).toBe("https://zyra-resource.services.ai.azure.com/v1/messages");
      expect(requests[0]?.headers.authorization).toBe("Bearer azure-live-token");
      expect(restored.requestState(prepared.requestId).client_attempts).toBe(2);
    });

    await withEnvironment({
      CLAUDE_CODE_USE_FOUNDRY: undefined,
      CLAUDE_CODE_USE_BEDROCK: "1",
      CLAUDE_CODE_USE_VERTEX: undefined,
      AWS_ACCESS_KEY_ID: "AKID",
      AWS_SECRET_ACCESS_KEY: "aws-secret",
      AWS_SESSION_TOKEN: "aws-session",
    }, async () => {
      const runtime = new ProviderModelRuntime({ provider: "bedrock" });
      runtime.configureEndpoint({ provider: "bedrock", region: "us-west-2" });
      const prepared = runtime.prepare(providerOptions());
      let authorization = "";
      let sessionToken = "";
      await runtime.queryHaiku(prepared, {
        async execute(request) {
          authorization = request.headers.authorization ?? "";
          sessionToken = request.headers["x-amz-security-token"] ?? "";
          return providerSuccess("bedrock-default");
        },
      });
      expect(authorization).toStartWith("AWS4-HMAC-SHA256 Credential=AKID/");
      expect(sessionToken).toBe("aws-session");
      expect(runtime.requestState(prepared.requestId).client_credential_refreshes).toBe(0);
    });

    await withEnvironment({
      CLAUDE_CODE_USE_FOUNDRY: undefined,
      CLAUDE_CODE_USE_BEDROCK: undefined,
      CLAUDE_CODE_USE_VERTEX: "1",
      GOOGLE_OAUTH_ACCESS_TOKEN: "gcp-live-token",
      ANTHROPIC_VERTEX_PROJECT_ID: "project-v12",
      VERTEX_REGION_CLAUDE_HAIKU_4_5: "asia-east1",
    }, async () => {
      const runtime = new ProviderModelRuntime({ provider: "vertex" });
      const prepared = runtime.prepare(providerOptions());
      let authorization = "";
      await runtime.queryHaiku(prepared, {
        async execute(request) {
          authorization = request.headers.authorization ?? "";
          return providerSuccess("vertex-default");
        },
      });
      expect(prepared.sourceCustody.client?.transport).toBe("vertex");
      expect(prepared.endpoint.projectId).toBe("project-v12");
      expect(prepared.endpoint.region).toBe("asia-east1");
      expect(authorization).toBe("Bearer gcp-live-token");
    });
  });

  test("e01.v10.provider-query-wrapper-settles-real-request-state", async () => {
    const runtime = new ProviderModelRuntime();
    runtime.configureCredential({ kind: "api_key", apiKey: "anthropic-live-key", fingerprint: "anthropic-key" });
    const prepared = runtime.prepare(providerOptions());
    const snapshot = runtime.snapshot();
    expect(JSON.stringify(snapshot)).not.toContain("anthropic-live-key");
    const restored = new ProviderModelRuntime();
    restored.restore(snapshot, {
      kind: "api_key",
      apiKey: "anthropic-live-key",
      fingerprint: "anthropic-key",
    });
    let attempts = 0;
    let observedApiKey = "";
    const transport: ProviderTransport = {
      async execute(request): Promise<import("../../src/provider/model-runtime.js").ProviderTransportResponse> {
        attempts += 1;
        observedApiKey = request.headers["x-api-key"] ?? "";
        if (attempts === 1) return { status: 429, headers: { "retry-after": "0" }, body: { error: { message: "retry" } } };
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
    const response = await restored.queryHaiku(snapshot.requests[0]!.request, transport);
    expect(response.content[0]).toEqual({ type: "text", text: "ok" });
    expect(restored.requestState(prepared.requestId).state).toBe("completed");
    expect(restored.requestState(prepared.requestId).client_attempts).toBe(2);
    expect(observedApiKey).toBe("anthropic-live-key");
    expect(runtime.queryWithModel).toBeDefined();
  });
});
