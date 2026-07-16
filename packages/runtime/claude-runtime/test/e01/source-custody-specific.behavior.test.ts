import { describe, expect, test } from "bun:test";

import {
  ProviderCacheCustodyRuntime,
} from "../../src/provider/cache-custody-runtime.js";
import {
  CompactionSourceCustodyRuntime,
  type CustodyCompactionMessage,
  type SummaryStreamEvent,
} from "../../src/compact/compaction-custody-runtime.js";

const toolConversation = (): CustodyCompactionMessage[] => [
  { role: "assistant", content: [{ type: "tool_use", id: "tool-1", name: "read" }] },
  { role: "user", content: [{ type: "tool_result", tool_use_id: "tool-1", content: "x".repeat(240) }] },
  { role: "user", content: [{ type: "text", text: "continue" }] },
];

describe("E01 source-specific provider custody", () => {
  test("e01.custody.provider-creates-anthropic-client-descriptor", () => {
    const runtime = new ProviderCacheCustodyRuntime();
    const client = runtime.getAnthropicClient({
      providerId: "anthropic",
      endpoint: "https://provider.invalid",
      model: "claude-sonnet",
      credential: "secret-value",
    });
    expect(client?.transport).toBe("anthropic");
    expect(client?.headers["x-api-key"]).toBe("[redacted]");
    expect(client?.credentialFingerprint).not.toContain("secret-value");
  });

  test("e01.custody.provider-one-hour-ttl-is-session-latched", () => {
    const runtime = new ProviderCacheCustodyRuntime();
    expect(runtime.should1hCacheTTL({ sessionId: "s1", providerId: "bedrock", model: "claude-sonnet", querySource: "cli", oneHourEnabled: true })).toBe(true);
    expect(runtime.should1hCacheTTL({ sessionId: "s1", providerId: "bedrock", model: "other", querySource: "blocked", oneHourEnabled: false })).toBe(true);
    expect(runtime.snapshot().oneHourSessions).toEqual(["s1"]);
  });

  test("e01.custody.provider-cache-breakpoint-is-unique", () => {
    const runtime = new ProviderCacheCustodyRuntime();
    const messages = runtime.addCacheBreakpoints(toolConversation(), { ttl: "1h", pinnedToolResultIds: ["tool-1"] });
    const blocks = messages.flatMap((message) => Array.isArray(message.content) ? message.content as Record<string, unknown>[] : []);
    expect(blocks.filter((block) => block.cacheControl !== undefined)).toHaveLength(1);
    expect(blocks.find((block) => block.tool_use_id === "tool-1")?.cacheReference).toBe(true);
  });

  test("e01.custody.provider-cache-breakpoint-honors-skip-write", () => {
    const runtime = new ProviderCacheCustodyRuntime();
    const messages = runtime.addCacheBreakpoints(toolConversation(), { ttl: "5m", skipCacheWrite: true });
    const blocks = messages.flatMap((message) => Array.isArray(message.content) ? message.content as Record<string, unknown>[] : []);
    expect(blocks.some((block) => block.cacheControl !== undefined)).toBe(false);
  });

  test("e01.custody.provider-cache-diff-path-is-session-scoped", () => {
    const runtime = new ProviderCacheCustodyRuntime();
    expect(runtime.getCacheBreakDiffPath("G:\\cache\\", "session/unsafe", 42)).toBe("G:/cache/session_unsafe/42-cache-break.json");
  });

  test("e01.custody.provider-excluded-model-suppresses-break", () => {
    const runtime = new ProviderCacheCustodyRuntime();
    const result = runtime.checkResponseForCacheBreak({ sessionId: "s", model: "local-test", promptHash: "a", cacheReadInputTokens: 0, observedAtMs: 1 }, { excludedModels: ["local-*"] });
    expect(result.reason).toBe("excluded-model");
    expect(result.broken).toBe(false);
  });

  test("e01.custody.provider-cache-read-collapse-is-detected", () => {
    const runtime = new ProviderCacheCustodyRuntime();
    runtime.checkResponseForCacheBreak({ sessionId: "s", model: "claude", promptHash: "a", cacheReadInputTokens: 1000, observedAtMs: 1 });
    const result = runtime.checkResponseForCacheBreak({ sessionId: "s", model: "claude", promptHash: "a", cacheReadInputTokens: 10, observedAtMs: 2 });
    expect(result.reason).toBe("cache-read-collapse");
    expect(result.readRatio).toBe(0.01);
  });

  test("e01.custody.provider-compaction-suppresses-cache-collapse", () => {
    const runtime = new ProviderCacheCustodyRuntime();
    runtime.checkResponseForCacheBreak({ sessionId: "s", model: "claude", promptHash: "a", cacheReadInputTokens: 1000, observedAtMs: 1 });
    const result = runtime.checkResponseForCacheBreak({ sessionId: "s", model: "claude", promptHash: "b", cacheReadInputTokens: 0, observedAtMs: 2, compactionObserved: true });
    expect(result.broken).toBe(false);
    expect(result.suppressed).toBe(true);
  });

  test("e01.custody.provider-tool-mismatch-retains-index-evidence", () => {
    const runtime = new ProviderCacheCustodyRuntime();
    const evidence = runtime.logToolUseToolResultMismatch([], [
      { role: "assistant", content: [{ type: "tool_use", id: "a" }, { type: "tool_use", id: "b" }] },
      { role: "user", content: [{ type: "tool_result", tool_use_id: "a" }, { type: "tool_result", tool_use_id: "orphan" }] },
    ]);
    expect(evidence.missingResultIds).toEqual(["b"]);
    expect(evidence.orphanResultIds).toEqual(["orphan"]);
    expect(evidence.toolUseIndexes.a).toEqual([0]);
  });

  test("e01.custody.provider-error-is-rendered-for-users", () => {
    const runtime = new ProviderCacheCustodyRuntime();
    expect(runtime.getAssistantMessageFromError({ status: 429, message: "limit" })).toContain("bounded backoff");
    expect(runtime.getAssistantMessageFromError({ code: "ETIMEDOUT" })).toContain("timed out");
    expect(runtime.getAssistantMessageFromError({ status: 401 })).toContain("authentication failed");
  });

  test("e01.custody.provider-request-hook-mutates-real-message-input", () => {
    const runtime = new ProviderCacheCustodyRuntime();
    const request: Record<string, unknown> = { providerId: "bedrock", model: "claude-sonnet", sessionId: "s", querySource: "cli", oneHourCacheEnabled: true, messages: toolConversation() };
    const effect = runtime.applyProviderRequestCustody(request);
    expect(effect.ttl).toBe("1h");
    expect(effect.breakpointCount).toBe(1);
    expect((request.sourceCustody as { applied: boolean }).applied).toBe(true);
  });
});

describe("E01 source-specific compaction custody", () => {
  test("e01.custody.compact-pending-edits-are-consumed-once", () => {
    const runtime = new CompactionSourceCustodyRuntime();
    runtime.maybeTimeBasedMicrocompact({ sessionId: "s", source: "main", nowMs: 100, intervalMs: 1, cacheRoot: ".cache", messages: toolConversation(), keepLastMessages: 1 });
    expect(runtime.pendingCacheEdits("s")).toEqual(["tool-1"]);
    expect(runtime.consumePendingCacheEdits("s")).toEqual(["tool-1"]);
    expect(runtime.consumePendingCacheEdits("s")).toEqual([]);
  });

  test("e01.custody.compact-main-thread-source-is-explicit", () => {
    const runtime = new CompactionSourceCustodyRuntime();
    expect(runtime.isMainThreadSource("interactive-main")).toBe(true);
    expect(runtime.isMainThreadSource("background-subagent")).toBe(false);
  });

  test("e01.custody.compact-old-tool-results-are-replaced", () => {
    const runtime = new CompactionSourceCustodyRuntime();
    const result = runtime.microcompactMessages(toolConversation(), { keepLastMessages: 1, replacementLimit: 2 });
    expect(result.editedToolResultIds).toEqual(["tool-1"]);
    expect(result.removedCharacters).toBeGreaterThan(100);
  });

  test("e01.custody.compact-cache-path-is-deterministic", () => {
    const runtime = new CompactionSourceCustodyRuntime();
    expect(runtime.cachedMicrocompactPath("G:\\cache\\", "session/a")).toMatch(/^G:\/cache\/session_a\/microcompact-[a-f0-9]{8}\.json$/);
  });

  test("e01.custody.compact-time-gate-blocks-early-repeat", () => {
    const runtime = new CompactionSourceCustodyRuntime();
    const first = runtime.maybeTimeBasedMicrocompact({ sessionId: "s", source: "main", nowMs: 100, intervalMs: 10, cacheRoot: ".cache", messages: toolConversation(), keepLastMessages: 1 });
    const second = runtime.maybeTimeBasedMicrocompact({ sessionId: "s", source: "main", nowMs: 105, intervalMs: 10, cacheRoot: ".cache", messages: toolConversation(), keepLastMessages: 1 });
    expect(first.performed).toBe(true);
    expect(second.reason).toBe("not-due");
  });

  test("e01.custody.compact-auto-threshold-reserves-output-space", () => {
    const runtime = new CompactionSourceCustodyRuntime();
    expect(runtime.shouldAutoCompact({ currentTokens: 850, contextWindow: 1000, reservedTokens: 100, minimumFreeTokens: 75 })).toBe(true);
    expect(runtime.shouldAutoCompact({ currentTokens: 500, contextWindow: 1000, reservedTokens: 100, minimumFreeTokens: 75 })).toBe(false);
  });

  test("e01.custody.compact-auto-plan-preserves-tail", () => {
    const runtime = new CompactionSourceCustodyRuntime();
    const result = runtime.autoCompactIfNeeded({ currentTokens: 950, contextWindow: 1000, reservedTokens: 100, minimumFreeTokens: 50, messages: toolConversation(), keepLastMessages: 2 });
    expect(result.required).toBe(true);
    expect(result.firstKeptIndex).toBe(1);
    expect(result.preserved).toHaveLength(2);
  });

  test("e01.custody.compact-boundary-carries-preserved-segment", () => {
    const runtime = new CompactionSourceCustodyRuntime();
    const boundary = runtime.annotateBoundaryWithPreservedSegment({ boundaryId: "b", firstKeptIndex: 1, summary: "summary", source: "partial" }, toolConversation());
    expect(boundary.preservedSegment).toHaveLength(2);
    expect(boundary.summary).toBe("summary");
  });

  test("e01.custody.compact-partial-result-has-stable-boundary", () => {
    const runtime = new CompactionSourceCustodyRuntime();
    const first = runtime.partialCompactConversation({ sessionId: "s", messages: toolConversation(), summary: "summary", keepLastMessages: 1 });
    const second = runtime.partialCompactConversation({ sessionId: "s", messages: toolConversation(), summary: "summary", keepLastMessages: 1 });
    expect(first.boundaryId).toBe(second.boundaryId);
    expect(first.firstKeptIndex).toBe(2);
  });

  test("e01.custody.compact-summary-stream-retries-incomplete-attempt", async () => {
    const runtime = new CompactionSourceCustodyRuntime();
    const result = await runtime.streamCompactSummary({
      maxAttempts: 2,
      stream: async function* (attempt): AsyncIterable<SummaryStreamEvent> {
        yield { type: "delta", text: attempt === 1 ? "partial" : "complete" };
        if (attempt === 2) yield { type: "complete", text: " summary" };
      },
    });
    expect(result.summary).toBe("complete summary");
    expect(result.attempts).toBe(2);
  });

  test("e01.custody.compact-summary-stream-emits-keepalive", async () => {
    const runtime = new CompactionSourceCustodyRuntime();
    let keepaliveCount = 0;
    const result = await runtime.streamCompactSummary({
      maxAttempts: 1,
      onKeepalive: () => { keepaliveCount += 1; },
      stream: async function* (): AsyncIterable<SummaryStreamEvent> {
        yield { type: "keepalive" };
        yield { type: "delta", text: "summary" };
        yield { type: "complete" };
      },
    });
    expect(result.keepalives).toBe(1);
    expect(keepaliveCount).toBe(1);
  });

  test("e01.custody.compact-session-memory-must-save-tokens", () => {
    const runtime = new CompactionSourceCustodyRuntime();
    expect(runtime.shouldUseSessionMemoryCompaction({ enabled: true, summary: "summary", summaryTokenCount: 20, currentTokenCount: 200 })).toBe(true);
    expect(runtime.shouldUseSessionMemoryCompaction({ enabled: true, summary: "summary", summaryTokenCount: 200, currentTokenCount: 100 })).toBe(false);
  });

  test("e01.custody.compact-session-memory-builds-real-result", () => {
    const runtime = new CompactionSourceCustodyRuntime();
    const result = runtime.createCompactionResultFromSessionMemory({ sessionId: "s", summary: "stored summary", messages: toolConversation(), attachments: [{ id: "a" }], keepLastMessages: 1, originalTokenCount: 300, summaryTokenCount: 40 });
    expect(result.messages[0]?.content).toBe("stored summary");
    expect(result.boundary.source).toBe("session-memory");
    expect(result.savedTokenCount).toBe(260);
  });

  test("e01.custody.compact-session-memory-fallback-is-null", () => {
    const runtime = new CompactionSourceCustodyRuntime();
    const result = runtime.trySessionMemoryCompaction({ enabled: false, sessionId: "s", summary: "stored", messages: toolConversation(), keepLastMessages: 1, originalTokenCount: 300, summaryTokenCount: 40 });
    expect(result).toBeNull();
  });

  test("e01.custody.compact-hook-mutates-default-input", () => {
    const runtime = new CompactionSourceCustodyRuntime();
    const input: Record<string, unknown> = { sessionId: "s", source: "main", nowMs: 100, microcompactIntervalMs: 1, cacheRoot: ".cache", keepLastMessages: 1, messages: toolConversation() };
    const effect = runtime.applyCompactionCustody(input);
    expect(effect?.performed).toBe(true);
    expect(input.sourceCustodyCompaction).toBeDefined();
    expect(input.messages).toEqual(effect?.messages);
  });
});
