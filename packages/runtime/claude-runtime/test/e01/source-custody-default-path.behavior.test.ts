import { describe, expect, test } from "bun:test";

import {
  MAX_NON_STREAMING_TOKENS,
  ProviderModelRuntime,
  type ProviderRequestOptions,
  type ProviderTransport,
} from "../../src/provider/model-runtime.ts";
import { ProviderCacheCustodyRuntime } from "../../src/provider/cache-custody-runtime.ts";
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
    const runtime = new ProviderModelRuntime();
    runtime.configureEndpoint({ provider: "bedrock", region: "eu-west-1" });
    runtime.configureCredential({ kind: "aws", fingerprint: "aws-fingerprint" });
    const prepared = runtime.prepare(providerOptions());
    const body = prepared.body as Record<string, unknown>;
    expect(prepared.sourceCustody.breakpointCount).toBeGreaterThan(0);
    expect(prepared.sourceCustody.client?.transport).toBe("bedrock");
    expect(prepared.sourceCustody.client?.auth).toBe("aws");
    expect(prepared.sourceCustody.client?.credentialRefresh).toBe("aws");
    expect(prepared.sourceCustody.client?.credentialFingerprint).toBe("aws-fingerprint");
    expect(prepared.endpoint.baseUrl).toBe(prepared.sourceCustody.client!.endpoint);
    expect(prepared.endpoint.baseUrl).toBe("https://bedrock-runtime.amazonaws.com");
    expect(prepared.endpoint.region).toBe("eu-west-1");
    expect(prepared.headers["x-app"]).toBe("cli");
    expect(prepared.headers["x-claude-code-session-id"]).toBe("provider-v9");
    expect(JSON.stringify(body.system)).toContain("cache_control");
    expect(body.max_tokens).toBeLessThanOrEqual(MAX_NON_STREAMING_TOKENS);
    expect(runtime.resolveModel("haiku").maxOutputTokens).toBe(8_192);
    const snapshot = runtime.snapshot();
    expect(snapshot.sourceCustody?.oneHourSessions).toEqual(["provider-v9"]);
    const restored = new ProviderModelRuntime();
    restored.restore(snapshot);
    expect(restored.snapshot().sourceCustody).toEqual(snapshot.sourceCustody!);

    const custody = new ProviderCacheCustodyRuntime();
    let azureRefreshes = 0;
    const retryDelays: number[] = [];
    const foundry = custody.getAnthropicClient({
      providerId: "anthropic",
      model: "claude-sonnet-4-5",
      credential: "",
      sessionId: "factory-session",
      maxRetries: 1,
      credentialProviders: {
        azure: async () => {
          azureRefreshes += 1;
          return "azure-live-token";
        },
      },
      sleep: async (milliseconds) => {
        retryDelays.push(milliseconds);
      },
      environment: {
        CLAUDE_CODE_USE_FOUNDRY: "1",
        ANTHROPIC_FOUNDRY_RESOURCE: "zyra-resource",
      },
    });
    expect(foundry?.transport).toBe("foundry");
    expect(foundry?.endpoint).toBe("https://zyra-resource.services.ai.azure.com");
    expect(foundry?.skipAuth).toBe(false);
    const foundryRequests: Array<{ headers: Readonly<Record<string, string>> }> = [];
    let foundryAttempts = 0;
    const foundryResponse = await foundry!.execute({
      url: `${foundry!.endpoint}/v1/messages`,
      method: "POST",
      headers: foundry!.headers,
      body: "{}",
      timeoutMs: foundry!.timeoutMs,
    }, {
      async execute(request): Promise<import("../../src/provider/model-runtime.js").ProviderTransportResponse> {
        foundryRequests.push(request);
        foundryAttempts += 1;
        return { status: foundryAttempts === 1 ? 429 : 200, headers: {} };
      },
    });
    expect(foundryResponse.status).toBe(200);
    expect(foundryRequests).toHaveLength(2);
    expect(foundryRequests[0]?.headers.authorization).toBe("Bearer azure-live-token");
    expect(azureRefreshes).toBe(2);
    expect(retryDelays).toEqual([250]);
    expect(foundry!.executionSnapshot()).toMatchObject({ attempts: 2, credentialRefreshes: 2 });

    let bedrockRequest: { headers: Readonly<Record<string, string>> } | undefined;
    const bedrock = custody.getAnthropicClient({
      providerId: "bedrock",
      model: "claude-sonnet-4-5",
      credential: "",
      region: "us-west-2",
      maxRetries: 0,
      now: () => Date.parse("2026-07-16T00:00:00.000Z"),
      credentialProviders: {
        aws: async () => ({ accessKeyId: "AKID", secretAccessKey: "secret", sessionToken: "session" }),
      },
      environment: {},
    });
    await bedrock!.execute({
      url: `${bedrock!.endpoint}/model/claude/invoke-with-response-stream`,
      method: "POST",
      headers: bedrock!.headers,
      body: "{}",
      timeoutMs: bedrock!.timeoutMs,
    }, {
      async execute(request) {
        bedrockRequest = request;
        return { status: 200, headers: {} };
      },
    });
    expect(bedrockRequest?.headers.authorization).toStartWith("AWS4-HMAC-SHA256 Credential=AKID/");
    expect(bedrockRequest?.headers["x-amz-security-token"]).toBe("session");

    let vertexAuthorization = "";
    const vertex = custody.getAnthropicClient({
      providerId: "vertex",
      model: "claude-haiku-4-5",
      credential: "",
      maxRetries: 0,
      credentialProviders: { gcp: async () => "gcp-live-token" },
      environment: {
        ANTHROPIC_VERTEX_PROJECT_ID: "project-v10",
        VERTEX_REGION_CLAUDE_HAIKU_4_5: "asia-east1",
      },
    });
    expect(vertex?.projectId).toBe("project-v10");
    expect(vertex?.region).toBe("asia-east1");
    await vertex!.execute({
      url: `${vertex!.endpoint}/v1/projects/project-v10/locations/asia-east1/publishers/anthropic/models/claude:rawPredict`,
      method: "POST",
      headers: vertex!.headers,
      body: "{}",
      timeoutMs: vertex!.timeoutMs,
    }, {
      async execute(request) {
        vertexAuthorization = request.headers.authorization;
        return { status: 200, headers: {} };
      },
    });
    expect(vertexAuthorization).toBe("Bearer gcp-live-token");
  });

  test("e01.v10.provider-query-wrapper-settles-real-request-state", async () => {
    const runtime = new ProviderModelRuntime();
    runtime.configureCredential({ kind: "api_key", apiKey: "anthropic-live-key", fingerprint: "anthropic-key" });
    const prepared = runtime.prepare(providerOptions());
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
    const response = await runtime.queryHaiku(prepared, transport);
    expect(response.content[0]).toEqual({ type: "text", text: "ok" });
    expect(runtime.requestState(prepared.requestId).state).toBe("completed");
    expect(runtime.requestState(prepared.requestId).client_attempts).toBe(2);
    expect(observedApiKey).toBe("anthropic-live-key");
    expect(runtime.queryWithModel).toBeDefined();
  });
});
