import assert from "node:assert/strict";
import test from "node:test";

import {
  ClaudeRuntimeCore,
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

class MemoryHost implements RuntimeHost {
  readonly events: RuntimeEvent[] = [];
  readonly batches: ToolBatch[] = [];
  readonly artifacts: ArtifactReceipt[] = [];
  aborted = false;

  async emitEvent(event: RuntimeEvent): Promise<void> {
    this.events.push(event);
  }

  async executeBatch(
    batch: ToolBatch,
    requests: ToolExecutionRequest[],
  ): Promise<ToolExecutionResponse[]> {
    this.batches.push(batch);
    return requests.map((request) => ({
      tool_call_id: request.toolCallId,
      ok: request.arguments.fail !== true,
      summary: request.arguments.fail === true ? "failed" : "executed " + request.toolName,
      output: (request.arguments.large === true
        ? { content: "x".repeat(1000) }
        : { arguments: request.arguments }) as JsonObject,
      artifacts: [],
      error: request.arguments.fail === true ? "tool_failed" : null,
      metadata: { host: "memory" },
    }));
  }

  async externalize(request: ArtifactRequest): Promise<ArtifactReceipt> {
    const artifact = {
      artifact_id: "artifact-" + String(this.artifacts.length + 1),
      kind: request.kind,
      uri: "memory://" + request.requestId,
      title: request.title,
      metadata: request.metadata,
    };
    this.artifacts.push(artifact);
    return artifact;
  }

  isAborted(): boolean {
    return this.aborted;
  }
}

interface E01RuntimeView {
  journal: {
    revision: number;
    state: {
      provider?: { revision?: number };
      query?: { revision?: number };
      context?: { revision?: number };
    };
  };
  query: { revision: number };
  compact: { boundaries: unknown[] };
  telemetry: { prompts: unknown[]; samples: unknown[] };
  recovery: { contexts: unknown[] };
  providerPrompt: { lastPrompt: { fingerprint: string } | null };
  providerRequests: {
    requests: Array<{ requestId: string; status: string }>;
    attempts: Array<{ requestId: string; status: string }>;
    chunks: Array<{ attemptId: string; sequence: number }>;
  };
  providerResponses: {
    builders: Array<{ requestId: string; status: string }>;
    responses: Array<{ requestId: string; stopReason: string }>;
  };
  providerRouting: {
    states: Array<{ routeId: string; status: string; inFlight: number; successes: number; failures: number }>;
  };
  providerRateLimits: {
    buckets: Array<{ limitId: string; reserved: number; consumed: number }>;
    reservations: Array<{ requestId: string; status: string }>;
  };
  provider: {
    requests: Array<{ state: string; request: { requestId: string } }>;
  };
  providerTransport: {
    requests: Array<{ state: string; status: number | null }>;
  };
  providerCredentials: {
    records: Array<{ status: string; totalSuccesses: number; totalFailures: number }>;
  };
  tools: {
    calls: Array<{ state: string; toolName: string }>;
    leases: Array<{ releasedAt: string | null }>;
  };
  toolResults: {
    accumulators: Array<{ status: string; success: boolean | null }>;
    deliveries: Array<{ success: boolean; deliveryDigest: string }>;
  };
  session: {
    status: string;
    messages: unknown[];
    effects: Array<{ state: string }>;
  };
  custody: {
    providers: Array<{ state: string; routeId: string; attemptId: string }>;
    tools: Array<{ state: string; effectId: string | null; deliveryId: string | null }>;
    turns: Array<{ state: string; toolCallIds: string[] }>;
    messages: Array<{ source: string }>;
  };
}

function e01State(result: Awaited<ReturnType<ClaudeRuntimeCore["run"]>>): E01RuntimeView {
  return result.sessionSnapshot.e01Runtime as unknown as E01RuntimeView;
}

function input(overrides: Partial<RuntimeRunInput> = {}): RuntimeRunInput {
  return {
    runId: "run-1",
    taskId: "task-1",
    nodeId: "node-1",
    workerRequestId: "worker-request-1",
    sessionId: "session-1",
    messages: [{ role: "user", content: "execute" }],
    turns: [
      [
        { tool_name: "read", arguments: { path: "a" } },
        { tool_name: "read", arguments: { path: "b" } },
        { tool_name: "write", arguments: { path: "c", content: "done" } },
      ],
      [{ tool_name: "read", arguments: { path: "c" } }],
    ],
    tools: [
      {
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
      },
      {
        name: "write",
        purpose: "write",
        source: "test",
        input_schema: {
          type: "object",
          required: ["path", "content"],
          properties: {
            path: { type: "string" },
            content: { type: "string" },
          },
        },
        output_schema: {},
        metadata: { read_only: "false", concurrency_safe: "false" },
      },
    ],
    config: {},
    ...overrides,
  };
}

test("runtime owns multi-turn lifecycle and read-only batches", async () => {
  const host = new MemoryHost();
  const result = await new ClaudeRuntimeCore().run(input(), host);
  assert.equal(result.ok, true);
  assert.equal(result.turnCount, 2);
  assert.equal(result.toolCallCount, 4);
  assert.deepEqual(
    host.batches.map((batch) => batch.executionMode),
    ["concurrent_read_only", "serial_non_read_only", "concurrent_read_only"],
  );
  assert.equal(result.metadata.canonical_runtime_owner, "typescript");
  assert.equal(result.sessionSnapshot.canonical_owner, "typescript");
  assert.ok(e01State(result).query.revision > 0);
  assert.ok((e01State(result).journal.state.query?.revision ?? 0) > 0);
  assert.ok(host.events.some((event) => event.phase === "stream_request_start"));
  assert.ok(host.events.some((event) => event.phase === "session_completed"));
});

test("runtime rejects invalid tool arguments before the Python host", async () => {
  const host = new MemoryHost();
  const selected = input({
    turns: [[{ tool_name: "read", arguments: {} }]],
  });
  const result = await new ClaudeRuntimeCore().run(selected, host);
  assert.equal(result.ok, false);
  assert.equal(result.stoppedReason, "schema_error");
  assert.equal(host.batches.length, 0);
  assert.ok(host.events.some((event) => event.phase === "tool_call_completed"));
});

test("runtime externalizes large tool results and compacts context", async () => {
  const host = new MemoryHost();
  const selected = input({
    turns: [[
      { tool_name: "read", arguments: { path: "a", large: true } },
      { tool_name: "read", arguments: { path: "b", large: true } },
    ]],
    config: {
      maxToolResultChars: 80,
      maxQueryContextChars: 120,
    },
  });
  const result = await new ClaudeRuntimeCore().run(selected, host);
  assert.equal(result.ok, true);
  assert.ok(result.artifacts.length >= 2);
  assert.ok(result.contextCompactionCount >= 1);
  assert.ok(e01State(result).compact.boundaries.length >= 1);
  assert.ok((e01State(result).journal.state.context?.revision ?? 0) > 0);
  assert.ok(host.events.some((event) => event.phase === "tool_result_budget_exceeded"));
  assert.ok(host.events.some((event) => event.phase === "context_compacted"));
});

test("runtime commits provider prompt usage and recovery state through default loop", async () => {
  const originalFetch = globalThis.fetch;
  let providerFetchCount = 0;
  globalThis.fetch = (async () => {
    providerFetchCount += 1;
    return new Response(JSON.stringify({
      id: "provider-success-message",
      type: "message",
      role: "assistant",
      model: "zyra-local-code-model",
      content: [{
        type: "tool_use",
        id: "provider-success-tool",
        name: "read",
        input: { path: "a" },
      }],
      stop_reason: "tool_use",
      stop_sequence: null,
      usage: {
        input_tokens: 12,
        output_tokens: 7,
        cache_read_input_tokens: 0,
        cache_creation_input_tokens: 0,
      },
    }), {
      status: 200,
      headers: {
        "content-type": "application/json",
        "request-id": "provider-success-upstream-request",
      },
    });
  }) as unknown as typeof fetch;
  const successHost = new MemoryHost();
  let success: Awaited<ReturnType<ClaudeRuntimeCore["run"]>>;
  try {
    success = await new ClaudeRuntimeCore().run(input({
      runId: "provider-success-run",
      sessionId: "provider-success-session",
      workerRequestId: "provider-success-request",
      turns: [],
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          model_api_key: "test-only-provider-key",
        },
      },
    }), successHost);
  } finally {
    globalThis.fetch = originalFetch;
  }
  const successState = e01State(success);
  assert.equal(success.ok, true);
  assert.equal(providerFetchCount, 1);
  assert.equal(successState.telemetry.prompts.length, 1);
  assert.equal(successState.telemetry.samples.length, 1);
  assert.ok(successState.providerPrompt.lastPrompt?.fingerprint);
  assert.deepEqual(successState.providerRequests.requests.map((item) => item.status), ["completed"]);
  assert.deepEqual(successState.providerRequests.attempts.map((item) => item.status), ["succeeded"]);
  assert.equal(successState.providerRequests.chunks.length, 1);
  assert.deepEqual(successState.providerResponses.responses.map((item) => item.stopReason), ["tool_use"]);
  assert.equal(successState.providerRouting.states.find((item) => item.routeId === "compatible-default")?.successes, 1);
  assert.equal(successState.providerRouting.states.find((item) => item.routeId === "compatible-default")?.inFlight, 0);
  assert.deepEqual(successState.providerRateLimits.reservations.map((item) => item.status), ["committed"]);
  assert.equal(successState.providerRateLimits.buckets.find((item) => item.limitId === "compatible-default-requests")?.consumed, 1);
  assert.deepEqual(successState.provider.requests.map((item) => item.state), ["completed"]);
  assert.deepEqual(successState.providerTransport.requests.map((item) => item.state), ["completed"]);
  assert.equal(successState.providerCredentials.records.length, 1);
  assert.equal(successState.providerCredentials.records[0].totalSuccesses, 1);
  assert.deepEqual(successState.tools.calls.map((item) => item.state), ["succeeded"]);
  assert.ok(successState.tools.leases.every((item) => item.releasedAt !== null));
  assert.deepEqual(successState.toolResults.accumulators.map((item) => item.status), ["sealed"]);
  assert.deepEqual(successState.toolResults.deliveries.map((item) => item.success), [true]);
  assert.deepEqual(successState.session.effects.map((item) => item.state), ["committed"]);
  assert.ok(successState.session.messages.length >= 4);
  assert.deepEqual(successState.custody.providers.map((item) => item.state), ["succeeded"]);
  assert.deepEqual(successState.custody.tools.map((item) => item.state), ["succeeded"]);
  assert.ok(successState.custody.tools.every((item) => item.effectId && item.deliveryId));
  assert.deepEqual(successState.custody.turns.map((item) => item.state), ["completed"]);
  assert.ok(successState.custody.messages.some((item) => item.source === "tool_result"));
  assert.ok((successState.journal.state.provider?.revision ?? 0) > 0);
  assert.ok(successHost.events.some((event) => event.phase === "model_request_prepared"));
  assert.ok(successHost.events.some((event) => event.phase === "model_stream_report"));

  const failureHost = new MemoryHost();
  const failure = await new ClaudeRuntimeCore().run(input({
    runId: "provider-failure-run",
    sessionId: "provider-failure-session",
    workerRequestId: "provider-failure-request",
    config: { runtimeConstraints: { simulate_model_error: true } },
  }), failureHost);
  const failureState = e01State(failure);
  assert.equal(failure.ok, false);
  assert.equal(failure.stoppedReason, "model_error");
  assert.ok(failureState.recovery.contexts.length >= 1);
  assert.deepEqual(failureState.providerRequests.requests.map((item) => item.status), ["failed"]);
  assert.deepEqual(failureState.providerRequests.attempts.map((item) => item.status), ["failed"]);
  assert.deepEqual(failureState.providerResponses.builders.map((item) => item.status), ["failed"]);
  assert.equal(failureState.providerRouting.states.find((item) => item.routeId === "local-default")?.failures, 1);
  assert.deepEqual(failureState.providerRateLimits.reservations.map((item) => item.status), ["released"]);
  assert.ok((failureState.journal.state.provider?.revision ?? 0) > 0);
  assert.ok(failureHost.events.some((event) => event.phase === "api_retry_report"));
});

test("runtime lets canonical recovery policy stop a non-retryable provider request", async () => {
  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  globalThis.fetch = (async () => {
    requestCount += 1;
    return new Response("invalid api key", { status: 401 });
  }) as unknown as typeof fetch;
  try {
    const result = await new ClaudeRuntimeCore().run(input({
      runId: "provider-auth-run",
      sessionId: "provider-auth-session",
      workerRequestId: "provider-auth-request",
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          api_retry_max_attempts: 3,
        },
      },
    }), new MemoryHost());
    const state = e01State(result);
    assert.equal(result.ok, false);
    assert.equal(result.stoppedReason, "model_stream_failed");
    assert.equal(requestCount, 1);
    assert.equal(state.recovery.contexts.length, 1);
    assert.ok((state.journal.state.provider?.revision ?? 0) > 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("runtime clears canonical recovery state after a retry succeeds", async () => {
  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  globalThis.fetch = (async () => {
    requestCount += 1;
    if (requestCount === 1) {
      return new Response("temporary provider failure", {
        status: 503,
        headers: { "retry-after": "0" },
      });
    }
    return new Response(JSON.stringify({
      id: "retry-provider-message",
      type: "message",
      role: "assistant",
      model: "zyra-local-code-model",
      content: [{
        type: "tool_use",
        id: "retry-tool-call",
        name: "read",
        input: { path: "a" },
      }],
      stop_reason: "tool_use",
      stop_sequence: null,
      usage: { input_tokens: 4, output_tokens: 3 },
    }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as unknown as typeof fetch;
  try {
    const result = await new ClaudeRuntimeCore().run(input({
      runId: "provider-retry-run",
      sessionId: "provider-retry-session",
      workerRequestId: "provider-retry-request",
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          api_retry_max_attempts: 2,
        },
      },
    }), new MemoryHost());
    const state = e01State(result);
    assert.equal(result.ok, true);
    assert.equal(requestCount, 2);
    assert.equal(state.recovery.contexts.length, 0);
    assert.ok((state.journal.state.provider?.revision ?? 0) > 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("runtime restores its exact TypeScript snapshot", async () => {
  const firstHost = new MemoryHost();
  const first = await new ClaudeRuntimeCore().run(input({
    turns: [[{ tool_name: "read", arguments: { path: "a" } }]],
  }), firstHost);
  const resumedHost = new MemoryHost();
  const resumed = await new ClaudeRuntimeCore().run(input({
    turns: [[{ tool_name: "read", arguments: { path: "b" } }]],
    restoredState: first.sessionSnapshot,
  }), resumedHost);
  assert.equal(resumed.ok, true);
  assert.equal(resumed.metadata.restored, "true");
  assert.equal(
    (resumed.sessionSnapshot.lineage as { restored?: boolean } | null)?.restored,
    true,
  );
  assert.ok(resumedHost.events.some((event) => event.phase === "context_restored"));
});

test("runtime stops on max turns, abort and model error", async () => {
  const maxTurns = await new ClaudeRuntimeCore().run(input({
    config: { maxTurns: 1 },
  }), new MemoryHost());
  assert.equal(maxTurns.ok, false);
  assert.equal(maxTurns.stoppedReason, "max_turns_exceeded");
  assert.equal(maxTurns.turnCount, 1);

  const abortedHost = new MemoryHost();
  abortedHost.aborted = true;
  const aborted = await new ClaudeRuntimeCore().run(input(), abortedHost);
  assert.equal(aborted.ok, false);
  assert.equal(aborted.stoppedReason, "user_cancelled");

  const modelError = await new ClaudeRuntimeCore().run(input({
    config: { runtimeConstraints: { simulate_model_error: true } },
  }), new MemoryHost());
  assert.equal(modelError.ok, false);
  assert.equal(modelError.stoppedReason, "model_error");
});

test("runtime projects control commands and failure recovery signals", async () => {
  const controlHost = new MemoryHost();
  const controlled = await new ClaudeRuntimeCore().run(input({
    turns: [[{ tool_name: "read", arguments: { path: "a" } }]],
    config: {
      controlCommands: [
        { name: "context" },
        { name: "resume" },
        { name: "doctor", artifact_policy: "artifact" },
      ],
    },
  }), controlHost);
  assert.equal(controlled.ok, true);
  assert.equal(controlled.metadata.control_command_count, "3");
  assert.equal(controlled.metadata.runtime_state_control_mutations, "3");
  assert.equal(controlled.metadata.session_lifecycle_resume_plans, "1");
  assert.ok(controlHost.events.some((event) => event.phase === "control_command"));
  assert.ok(controlled.artifacts.some((artifact) => artifact.title.includes("Claude control command")));

  const failureHost = new MemoryHost();
  const failed = await new ClaudeRuntimeCore().run(input({
    turns: [[{ tool_name: "read", arguments: { path: "a", fail: true } }]],
  }), failureHost);
  assert.equal(failed.ok, false);
  assert.ok(failureHost.events.some((event) => event.phase === "tool_failure_signal"));
  assert.ok(failureHost.events.some((event) => event.phase === "watchdog_signal"));
});
