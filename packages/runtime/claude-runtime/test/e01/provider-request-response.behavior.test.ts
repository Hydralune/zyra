import { describe, expect, test } from "bun:test";

import {
  DeterministicIdFactory,
  ManualClock,
} from "../../src/core/runtime-primitives.ts";
import { ProviderRequestRuntime } from "../../src/provider/request-runtime.ts";
import { ProviderResponseRuntime } from "../../src/provider/response-runtime.ts";

function ids(seed: string, sequence = 0): DeterministicIdFactory {
  return new DeterministicIdFactory(seed, sequence);
}

function requestBudget(overrides: Partial<{
  maximumAttempts: number;
  maximumInputTokens: number;
  maximumOutputTokens: number;
  maximumCost: number | null;
  deadlineAt: number;
}> = {}) {
  return {
    maximumAttempts: overrides.maximumAttempts ?? 3,
    maximumInputTokens: overrides.maximumInputTokens ?? 10_000,
    maximumOutputTokens: overrides.maximumOutputTokens ?? 2_000,
    maximumCost: overrides.maximumCost ?? 10,
    deadlineAt: overrides.deadlineAt ?? Number.MAX_SAFE_INTEGER,
  };
}

function createRequest(
  runtime: ProviderRequestRuntime,
  overrides: Partial<{
    requestId: string;
    sessionId: string;
    runId: string;
    queryId: string;
    turnId: string;
    idempotencyKey: string;
    modelPreference: string;
    payload: Record<string, string | number | boolean>;
    contextDigest: string;
    toolSetDigest: string;
    budget: ReturnType<typeof requestBudget>;
    correlationId: string;
  }> = {},
) {
  return runtime.create({
    requestId: overrides.requestId ?? "request-1",
    sessionId: overrides.sessionId ?? "session-1",
    runId: overrides.runId ?? "run-1",
    queryId: overrides.queryId ?? "query-1",
    turnId: overrides.turnId ?? "turn-1",
    idempotencyKey: overrides.idempotencyKey ?? "provider-call-1",
    modelPreference: overrides.modelPreference ?? "claude-test",
    payload: overrides.payload ?? { prompt: "inspect runtime", stream: true },
    contextDigest: overrides.contextDigest ?? "context-digest-1",
    toolSetDigest: overrides.toolSetDigest ?? "tool-digest-1",
    budget: overrides.budget ?? requestBudget(),
    correlationId: overrides.correlationId ?? "correlation-create",
    metadata: { fixture: "provider-request" },
  });
}

function prepareAttempt(
  runtime: ProviderRequestRuntime,
  overrides: Partial<{
    requestId: string;
    routeId: string;
    providerId: string;
    modelId: string;
    endpointId: string;
    credentialId: string;
    correlationId: string;
  }> = {},
) {
  return runtime.prepareAttempt({
    requestId: overrides.requestId ?? "request-1",
    routeId: overrides.routeId ?? "route-1",
    providerId: overrides.providerId ?? "anthropic",
    modelId: overrides.modelId ?? "claude-test",
    endpointId: overrides.endpointId ?? "endpoint-1",
    credentialId: overrides.credentialId ?? "credential-1",
    requestBody: { model: overrides.modelId ?? "claude-test", messages: [] },
    requestHeaders: {
      "content-type": "application/json",
      "x-api-key": "secret-that-must-be-digested",
    },
    correlationId: overrides.correlationId ?? "correlation-prepare",
  });
}

function responseRuntime(seed: string): ProviderResponseRuntime {
  return new ProviderResponseRuntime({ ids: ids(seed) });
}

function beginResponse(runtime: ProviderResponseRuntime) {
  return runtime.begin({
    responseId: "response-1",
    requestId: "request-1",
    attemptId: "attempt-1",
    providerId: "anthropic",
    modelId: "claude-test",
  });
}

function applyTextResponse(runtime: ProviderResponseRuntime) {
  runtime.apply({
    responseId: "response-1",
    eventId: "event-1",
    sequence: 1,
    kind: "message_start",
    payload: { messageId: "provider-message-1" },
  });
  runtime.apply({
    responseId: "response-1",
    eventId: "event-2",
    sequence: 2,
    kind: "block_start",
    blockIndex: 0,
    payload: { kind: "text", text: "Hello" },
  });
  runtime.apply({
    responseId: "response-1",
    eventId: "event-3",
    sequence: 3,
    kind: "block_delta",
    blockIndex: 0,
    payload: { text: ", world" },
  });
  runtime.apply({
    responseId: "response-1",
    eventId: "event-4",
    sequence: 4,
    kind: "block_stop",
    blockIndex: 0,
    payload: {},
  });
  runtime.apply({
    responseId: "response-1",
    eventId: "event-5",
    sequence: 5,
    kind: "usage",
    payload: {
      inputTokens: 10,
      outputTokens: 2,
      cacheReadTokens: 4,
      cacheWriteTokens: 0,
      serviceTier: "standard",
    },
  });
  runtime.apply({
    responseId: "response-1",
    eventId: "event-6",
    sequence: 6,
    kind: "message_stop",
    payload: { stopReason: "end_turn" },
  });
}

describe("provider request lifecycle", () => {
  test("creates an idempotent durable request without duplicating state", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderRequestRuntime({
      clock,
      ids: ids("request-create"),
    });
    const first = createRequest(runtime);
    const repeated = runtime.create({
      requestId: "different-request-id",
      sessionId: "session-1",
      runId: "run-1",
      queryId: "query-1",
      turnId: "turn-1",
      idempotencyKey: "provider-call-1",
      modelPreference: "claude-test",
      payload: { prompt: "inspect runtime", stream: true },
      contextDigest: "context-digest-1",
      toolSetDigest: "tool-digest-1",
      budget: requestBudget(),
      correlationId: "correlation-repeat",
    });
    expect(repeated.requestId).toBe(first.requestId);
    expect(runtime.listTransitions(first.requestId)).toHaveLength(1);
    expect(runtime.snapshot().requests).toHaveLength(1);
  });

  test("rejects reused idempotency keys with different payloads", () => {
    const runtime = new ProviderRequestRuntime({ ids: ids("request-conflict") });
    createRequest(runtime);
    expect(() =>
      runtime.create({
        sessionId: "session-1",
        runId: "run-1",
        queryId: "query-1",
        turnId: "turn-1",
        idempotencyKey: "provider-call-1",
        modelPreference: "claude-test",
        payload: { prompt: "different prompt" },
        contextDigest: "context-digest-1",
        toolSetDigest: "tool-digest-1",
        budget: requestBudget(),
        correlationId: "correlation-conflict",
      }),
    ).toThrow("provider_request_idempotency_conflict");
  });

  test("runs prepare dispatch stream and completion with usage custody", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderRequestRuntime({
      clock,
      ids: ids("request-complete"),
    });
    createRequest(runtime);
    const attempt = prepareAttempt(runtime);
    expect(attempt.ordinal).toBe(1);
    expect(attempt.requestHeadersDigest).not.toContain("secret-that-must-be-digested");
    runtime.dispatch({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      correlationId: "correlation-dispatch",
    });
    clock.advance(25);
    runtime.responseStarted({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      responseStatus: 200,
      providerRequestId: "provider-request-1",
      correlationId: "correlation-response",
    });
    runtime.appendChunk({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      sequence: 1,
      channel: "text",
      payload: { delta: "complete" },
    });
    const completed = runtime.complete({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      output: { role: "assistant", text: "complete" },
      usage: {
        inputTokens: 100,
        outputTokens: 20,
        cacheReadTokens: 40,
        cacheWriteTokens: 10,
        cost: 0.0125,
      },
      stopReason: "end_turn",
      correlationId: "correlation-complete",
    });
    expect(completed.status).toBe("completed");
    expect(completed.activeAttemptId).toBeNull();
    expect(completed.usage?.cost).toBe(0.0125);
    expect(runtime.getAttempt(attempt.attemptId)).toMatchObject({
      status: "succeeded",
      responseStatus: 200,
      providerRequestId: "provider-request-1",
    });
    expect(runtime.pending()).toEqual([]);
    expect(runtime.listTransitions("request-1").map((item) => item.kind)).toEqual([
      "request.created",
      "attempt.prepared",
      "attempt.dispatched",
      "response.started",
      "request.completed",
    ]);
  });

  test("rejects response chunks with a sequence gap without advancing state", () => {
    const runtime = new ProviderRequestRuntime({ ids: ids("request-gap") });
    createRequest(runtime);
    const attempt = prepareAttempt(runtime);
    runtime.dispatch({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      correlationId: "correlation-dispatch",
    });
    runtime.responseStarted({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      responseStatus: 200,
      correlationId: "correlation-response",
    });
    expect(() =>
      runtime.appendChunk({
        requestId: "request-1",
        attemptId: attempt.attemptId,
        sequence: 2,
        channel: "text",
        payload: { delta: "late" },
      }),
    ).toThrow("provider_chunk_sequence_gap");
    expect(runtime.getAttempt(attempt.attemptId).expectedChunkSequence).toBe(1);
    expect(runtime.getChunks(attempt.attemptId)).toEqual([]);
  });

  test("deduplicates a repeated response chunk by chunk id and digest", () => {
    const runtime = new ProviderRequestRuntime({ ids: ids("request-chunk-idem") });
    createRequest(runtime);
    const attempt = prepareAttempt(runtime);
    runtime.dispatch({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      correlationId: "correlation-dispatch",
    });
    runtime.responseStarted({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      responseStatus: 200,
      correlationId: "correlation-response",
    });
    const input = {
      requestId: "request-1",
      attemptId: attempt.attemptId,
      chunkId: "chunk-1",
      sequence: 1,
      channel: "text" as const,
      payload: { delta: "one" },
    };
    const first = runtime.appendChunk(input);
    const second = runtime.appendChunk(input);
    expect(second.chunkId).toBe(first.chunkId);
    expect(runtime.getChunks(attempt.attemptId)).toHaveLength(1);
    expect(() =>
      runtime.appendChunk({ ...input, payload: { delta: "conflict" } }),
    ).toThrow("provider_chunk_conflict");
  });

  test("schedules a retry and permits a different fallback route when due", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderRequestRuntime({
      clock,
      ids: ids("request-retry"),
    });
    createRequest(runtime);
    const first = prepareAttempt(runtime, { routeId: "route-primary" });
    runtime.dispatch({
      requestId: "request-1",
      attemptId: first.attemptId,
      correlationId: "correlation-dispatch-1",
    });
    const waiting = runtime.failAttempt({
      requestId: "request-1",
      attemptId: first.attemptId,
      errorClass: "overloaded",
      errorMessage: "provider overloaded",
      retryable: true,
      retryAfterMilliseconds: 100,
      correlationId: "correlation-failure-1",
    });
    expect(waiting.status).toBe("retry_wait");
    expect(waiting.retryAt).toBe(1_100);
    expect(() =>
      prepareAttempt(runtime, { routeId: "route-fallback" }),
    ).toThrow("provider_retry_not_due");
    clock.advance(100);
    const fallback = prepareAttempt(runtime, {
      routeId: "route-fallback",
      providerId: "bedrock",
      endpointId: "endpoint-fallback",
      correlationId: "correlation-prepare-2",
    });
    expect(fallback.ordinal).toBe(2);
    expect(fallback.routeId).toBe("route-fallback");
    expect(runtime.get("request-1").attemptIds).toEqual([
      first.attemptId,
      fallback.attemptId,
    ]);
  });

  test("terminates immediately for a non-retryable request error", () => {
    const runtime = new ProviderRequestRuntime({ ids: ids("request-terminal") });
    createRequest(runtime);
    const attempt = prepareAttempt(runtime);
    runtime.dispatch({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      correlationId: "correlation-dispatch",
    });
    const failed = runtime.failAttempt({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      errorClass: "invalid_request",
      errorMessage: "unsupported parameter",
      retryable: false,
      correlationId: "correlation-failure",
    });
    expect(failed.status).toBe("failed");
    expect(failed.stopReason).toBe("non_retryable_error");
    expect(runtime.pending()).toEqual([]);
  });

  test("stops retrying after the attempt budget is exhausted", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderRequestRuntime({
      clock,
      ids: ids("request-attempt-budget"),
    });
    createRequest(runtime, {
      budget: requestBudget({ maximumAttempts: 1 }),
    });
    const attempt = prepareAttempt(runtime);
    runtime.dispatch({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      correlationId: "correlation-dispatch",
    });
    const failed = runtime.failAttempt({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      errorClass: "timeout",
      errorMessage: "deadline",
      retryable: true,
      correlationId: "correlation-timeout",
    });
    expect(failed.status).toBe("failed");
    expect(failed.stopReason).toBe("attempt_budget_exhausted");
    expect(() => prepareAttempt(runtime)).toThrow(
      "provider_request_status_conflict",
    );
  });

  test("rejects attempt preparation after the request deadline", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderRequestRuntime({
      clock,
      ids: ids("request-deadline"),
    });
    createRequest(runtime, {
      budget: requestBudget({ deadlineAt: 1_010 }),
    });
    clock.advance(10);
    expect(() => prepareAttempt(runtime)).toThrow(
      "provider_request_deadline_exceeded",
    );
  });

  test("rejects output usage that exceeds token budget", () => {
    const runtime = new ProviderRequestRuntime({ ids: ids("request-usage") });
    createRequest(runtime, {
      budget: requestBudget({ maximumOutputTokens: 10 }),
    });
    const attempt = prepareAttempt(runtime);
    runtime.dispatch({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      correlationId: "correlation-dispatch",
    });
    expect(() =>
      runtime.complete({
        requestId: "request-1",
        attemptId: attempt.attemptId,
        output: "too long",
        usage: {
          inputTokens: 10,
          outputTokens: 11,
          cacheReadTokens: 0,
          cacheWriteTokens: 0,
          cost: 0.01,
        },
        stopReason: "maximum_tokens",
        correlationId: "correlation-complete",
      }),
    ).toThrow("provider_output_token_budget_exceeded");
    expect(runtime.get("request-1").status).toBe("dispatched");
  });

  test("accepts fractional cost but rejects cost above the request ceiling", () => {
    const runtime = new ProviderRequestRuntime({ ids: ids("request-cost") });
    createRequest(runtime, {
      budget: requestBudget({ maximumCost: 0.1 }),
    });
    const attempt = prepareAttempt(runtime);
    runtime.dispatch({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      correlationId: "correlation-dispatch",
    });
    expect(() =>
      runtime.complete({
        requestId: "request-1",
        attemptId: attempt.attemptId,
        output: "complete",
        usage: {
          inputTokens: 10,
          outputTokens: 5,
          cacheReadTokens: 0,
          cacheWriteTokens: 0,
          cost: 0.1001,
        },
        stopReason: "end_turn",
        correlationId: "correlation-complete",
      }),
    ).toThrow("provider_cost_budget_exceeded");
  });

  test("cancels an active attempt and prevents later completion", () => {
    const runtime = new ProviderRequestRuntime({ ids: ids("request-cancel") });
    createRequest(runtime);
    const attempt = prepareAttempt(runtime);
    runtime.dispatch({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      correlationId: "correlation-dispatch",
    });
    const cancelled = runtime.cancel(
      "request-1",
      "user interrupted",
      "correlation-cancel",
    );
    expect(cancelled.status).toBe("cancelled");
    expect(cancelled.cancellationReason).toBe("user interrupted");
    expect(runtime.getAttempt(attempt.attemptId).status).toBe("abandoned");
    expect(() =>
      runtime.complete({
        requestId: "request-1",
        attemptId: attempt.attemptId,
        output: "late",
        usage: {
          inputTokens: 1,
          outputTokens: 1,
          cacheReadTokens: 0,
          cacheWriteTokens: 0,
          cost: 0,
        },
        stopReason: "end_turn",
        correlationId: "correlation-late",
      }),
    ).toThrow("provider_request_status_conflict");
  });

  test("converts an in-flight restored request to retry-wait", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderRequestRuntime({
      clock,
      ids: ids("request-restore"),
    });
    createRequest(runtime);
    const attempt = prepareAttempt(runtime);
    runtime.dispatch({
      requestId: "request-1",
      attemptId: attempt.attemptId,
      correlationId: "correlation-dispatch",
    });
    const oldTransitionIds = runtime
      .listTransitions("request-1")
      .map((item) => item.transitionId);
    const snapshot = runtime.snapshot();
    const restored = new ProviderRequestRuntime({
      clock,
      ids: ids("request-restore-after", 100),
    });
    restored.restore(snapshot);
    const project = restored.get("request-1");
    expect(project.status).toBe("retry_wait");
    expect(project.retryAt).toBe(1_000);
    expect(project.restartEpoch).toBe(1);
    expect(project.activeAttemptId).toBeNull();
    const restoredTransitionIds = restored
      .listTransitions("request-1")
      .map((item) => item.transitionId);
    expect(restoredTransitionIds).toEqual(oldTransitionIds);
  });

  test("rejects a snapshot whose request payload changed", () => {
    const runtime = new ProviderRequestRuntime({ ids: ids("request-snapshot") });
    createRequest(runtime);
    const snapshot = runtime.snapshot();
    snapshot.requests[0]!.payload.prompt = "tampered";
    const restored = new ProviderRequestRuntime({ ids: ids("request-after") });
    expect(() => restored.restore(snapshot)).toThrow(
      "provider_request_snapshot_checksum_mismatch",
    );
  });
});

describe("provider response normalization", () => {
  test("normalizes a streamed text response and usage metadata", () => {
    const runtime = responseRuntime("response-text");
    beginResponse(runtime);
    applyTextResponse(runtime);
    const response = runtime.finalize("response-1");
    expect(response.text).toBe("Hello, world");
    expect(response.reasoning).toBe("");
    expect(response.providerMessageId).toBe("provider-message-1");
    expect(response.stopReason).toBe("end_turn");
    expect(response.usage).toEqual({
      inputTokens: 10,
      outputTokens: 2,
      cacheReadTokens: 4,
      cacheWriteTokens: 0,
      serviceTier: "standard",
      providerFields: {},
    });
    expect(response.blocks[0]?.status).toBe("complete");
    expect(runtime.getResponse("response-1").responseDigest).toBe(
      response.responseDigest,
    );
  });

  test("normalizes reasoning and a structured tool use", () => {
    const runtime = responseRuntime("response-tool");
    beginResponse(runtime);
    runtime.apply({
      responseId: "response-1",
      sequence: 1,
      kind: "block_start",
      blockIndex: 0,
      payload: { kind: "reasoning", reasoning: "Inspect" },
    });
    runtime.apply({
      responseId: "response-1",
      sequence: 2,
      kind: "block_delta",
      blockIndex: 0,
      payload: { reasoning: " first" },
    });
    runtime.apply({
      responseId: "response-1",
      sequence: 3,
      kind: "block_stop",
      blockIndex: 0,
      payload: {},
    });
    runtime.apply({
      responseId: "response-1",
      sequence: 4,
      kind: "block_start",
      blockIndex: 1,
      payload: {
        kind: "tool_use",
        toolUseId: "tool-use-1",
        toolName: "read_file",
        input: { path: "src/main.ts" },
      },
    });
    runtime.apply({
      responseId: "response-1",
      sequence: 5,
      kind: "block_delta",
      blockIndex: 1,
      payload: { input: { encoding: "utf8" } },
    });
    runtime.apply({
      responseId: "response-1",
      sequence: 6,
      kind: "block_stop",
      blockIndex: 1,
      payload: {},
    });
    runtime.apply({
      responseId: "response-1",
      sequence: 7,
      kind: "message_stop",
      payload: { stopReason: "tool_calls" },
    });
    const response = runtime.finalize("response-1");
    expect(response.reasoning).toBe("Inspect first");
    expect(response.stopReason).toBe("tool_use");
    expect(response.toolUses).toEqual([
      {
        toolUseId: "tool-use-1",
        toolName: "read_file",
        input: { path: "src/main.ts", encoding: "utf8" },
        blockIndex: 1,
      },
    ]);
  });

  test("maps provider-specific stop reason aliases", () => {
    const aliases = [
      ["stop", "end_turn"],
      ["length", "maximum_tokens"],
      ["safety", "content_filter"],
      ["unrecognized", "unknown"],
    ] as const;
    for (const [source, expected] of aliases) {
      const runtime = responseRuntime(`response-stop-${source}`);
      beginResponse(runtime);
      runtime.apply({
        responseId: "response-1",
        sequence: 1,
        kind: "message_stop",
        payload: { stopReason: source },
      });
      expect(runtime.finalize("response-1").stopReason).toBe(expected);
    }
  });

  test("rejects event sequence gaps without recording the event", () => {
    const runtime = responseRuntime("response-gap");
    beginResponse(runtime);
    expect(() =>
      runtime.apply({
        responseId: "response-1",
        sequence: 2,
        kind: "message_start",
        payload: {},
      }),
    ).toThrow("response_event_sequence_gap");
    expect(runtime.replay("response-1")).toEqual([]);
  });

  test("deduplicates repeated event ids and rejects conflicting replays", () => {
    const runtime = responseRuntime("response-event-idem");
    beginResponse(runtime);
    const event = runtime.apply({
      responseId: "response-1",
      eventId: "stable-event",
      sequence: 1,
      kind: "message_start",
      payload: { messageId: "message-1" },
    });
    expect(
      runtime.apply({
        responseId: "response-1",
        eventId: "stable-event",
        sequence: 1,
        kind: "message_start",
        payload: { messageId: "message-1" },
      }).digest,
    ).toBe(event.digest);
    expect(runtime.replay("response-1")).toHaveLength(1);
    expect(() =>
      runtime.apply({
        responseId: "response-1",
        eventId: "stable-event",
        sequence: 1,
        kind: "message_start",
        payload: { messageId: "different" },
      }),
    ).toThrow("response_event_conflict");
  });

  test("rejects starting two blocks at the same index", () => {
    const runtime = responseRuntime("response-block-duplicate");
    beginResponse(runtime);
    runtime.apply({
      responseId: "response-1",
      sequence: 1,
      kind: "block_start",
      blockIndex: 0,
      payload: { kind: "text" },
    });
    expect(() =>
      runtime.apply({
        responseId: "response-1",
        sequence: 2,
        kind: "block_start",
        blockIndex: 0,
        payload: { kind: "reasoning" },
      }),
    ).toThrow("response_block_duplicate");
  });

  test("refuses finalization while a content block remains open", () => {
    const runtime = responseRuntime("response-open-block");
    beginResponse(runtime);
    runtime.apply({
      responseId: "response-1",
      sequence: 1,
      kind: "block_start",
      blockIndex: 0,
      payload: { kind: "text", text: "unfinished" },
    });
    runtime.apply({
      responseId: "response-1",
      sequence: 2,
      kind: "message_stop",
      payload: { stopReason: "end_turn" },
    });
    expect(() => runtime.finalize("response-1")).toThrow(
      "response_blocks_open",
    );
  });

  test("rejects duplicate tool use identifiers across blocks", () => {
    const runtime = responseRuntime("response-tool-duplicate");
    beginResponse(runtime);
    let sequence = 1;
    for (const index of [0, 1]) {
      runtime.apply({
        responseId: "response-1",
        sequence: sequence++,
        kind: "block_start",
        blockIndex: index,
        payload: {
          kind: "tool_use",
          toolUseId: "duplicate-tool-use",
          toolName: "read_file",
          input: { path: `${index}.txt` },
        },
      });
      runtime.apply({
        responseId: "response-1",
        sequence: sequence++,
        kind: "block_stop",
        blockIndex: index,
        payload: {},
      });
    }
    runtime.apply({
      responseId: "response-1",
      sequence,
      kind: "message_stop",
      payload: { stopReason: "tool_use" },
    });
    expect(() => runtime.finalize("response-1")).toThrow(
      "response_tool_use_duplicate",
    );
  });

  test("enforces the aggregate response character limit", () => {
    const runtime = new ProviderResponseRuntime({
      ids: ids("response-character-limit"),
      maximumCharacters: 5,
    });
    beginResponse(runtime);
    runtime.apply({
      responseId: "response-1",
      sequence: 1,
      kind: "block_start",
      blockIndex: 0,
      payload: { kind: "text", text: "12345" },
    });
    expect(() =>
      runtime.apply({
        responseId: "response-1",
        sequence: 2,
        kind: "block_delta",
        blockIndex: 0,
        payload: { text: "6" },
      }),
    ).toThrow("response_character_limit");
  });

  test("records provider error state and prevents further stream events", () => {
    const runtime = responseRuntime("response-error");
    beginResponse(runtime);
    runtime.apply({
      responseId: "response-1",
      sequence: 1,
      kind: "error",
      payload: { code: "overloaded", message: "try later" },
    });
    const builder = runtime.snapshot().builders[0]!;
    expect(builder.status).toBe("failed");
    expect(builder.errorCode).toBe("overloaded");
    expect(() =>
      runtime.apply({
        responseId: "response-1",
        sequence: 2,
        kind: "message_stop",
        payload: {},
      }),
    ).toThrow("response_not_open");
  });

  test("replays only events after the requested aggregate sequence", () => {
    const runtime = responseRuntime("response-replay");
    beginResponse(runtime);
    applyTextResponse(runtime);
    expect(runtime.replay("response-1", 3).map((event) => event.sequence)).toEqual([
      4,
      5,
      6,
    ]);
  });

  test("restores a complete response with identical normalized digest", () => {
    const runtime = responseRuntime("response-snapshot");
    beginResponse(runtime);
    applyTextResponse(runtime);
    const expected = runtime.finalize("response-1");
    const snapshot = runtime.snapshot();
    const restored = responseRuntime("response-after");
    restored.restore(snapshot);
    expect(restored.getResponse("response-1")).toEqual(expected);
    expect(restored.replay("response-1")).toHaveLength(6);
  });

  test("rejects snapshot event payload tampering", () => {
    const runtime = responseRuntime("response-tamper");
    beginResponse(runtime);
    applyTextResponse(runtime);
    runtime.finalize("response-1");
    const snapshot = runtime.snapshot();
    snapshot.events[0]!.payload.messageId = "tampered";
    const restored = responseRuntime("response-tamper-after");
    expect(() => restored.restore(snapshot)).toThrow(
      "response_snapshot_checksum_mismatch",
    );
  });
});
