import assert from "node:assert/strict";
import test from "node:test";

import {
  ClaudeRuntimeCore,
  RuntimeSession,
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
import { projectToolOutputForRuntime } from "../src/tools/model-result-projection.ts";
import {
  assertProviderRouteRenewalLineage,
  providerControlPlaneEvidenceFrames,
  providerControlPlaneToolSteps,
} from "../src/provider-control-plane-runtime.ts";
import type { ProviderRouteLease } from "../../provider-control-plane/src/contracts.ts";
import { ProgressiveExecutionRuntime } from "../src/loop/progressive-execution-runtime.ts";
import {
  durableCompactionSummary,
  e01RuntimeEventPayload,
  isClearlyPreDeliveryInspection,
  isClearlyRepairDrivingTool,
  isGeneratedDeliveryInspection,
  isGeneratedDeliveryMutation,
  isTargetedRepairInspection,
  isClearlyVerificationDrivingTool,
  isVerificationDrivingToolResult,
  modelCompactionPrompt,
  shouldCheckpointRuntimePhase,
  verificationScopeForTool,
  verificationScopeForToolResult,
} from "../src/query-engine.ts";

class MemoryHost implements RuntimeHost {
  readonly events: RuntimeEvent[] = [];
  readonly batches: ToolBatch[] = [];
  readonly artifacts: ArtifactReceipt[] = [];
  readonly checkpoints: JsonObject[] = [];
  aborted = false;

  async emitEvent(event: RuntimeEvent): Promise<void> {
    this.events.push(event);
  }

  async checkpointState(snapshot: JsonObject): Promise<void> {
    this.checkpoints.push(structuredClone(snapshot));
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
      metadata: {
        host: "memory",
        workspace_mutation_committed: String(
          request.toolName === "write" && request.arguments.fail !== true,
        ),
      },
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
  compact: {
    boundaries: unknown[];
    cleanup: {
      generation: number;
      classifierApprovalsCleared: number;
      speculativeChecksCleared: number;
      sessionMessageCacheCleared: number;
      lastQuerySource: string;
    };
  };
  telemetry: {
    compactGeneration: number;
    prompts: unknown[];
    samples: unknown[];
    events: Array<{ name: string; attributes: Record<string, unknown> }>;
  };
  recovery: {
    contexts: unknown[];
    cooldowns: Array<{ key: string; untilMs: number; reason: string }>;
  };
  providerPrompt: { lastPrompt: { fingerprint: string } | null };
  providerRequests: {
    requests: Array<{ requestId: string; status: string }>;
    attempts: Array<{
      requestId: string;
      status: string;
    }>;
    chunks: Array<{ attemptId: string; sequence: number }>;
    transitions: Array<{
      kind: string;
      occurredAt: number;
      payload: Record<string, unknown>;
    }>;
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
  assert.ok(host.events.some((event) => event.phase === "model_stream_frame"));
  assert.ok(host.events.some((event) => event.phase === "message_delta"));
  assert.ok(host.checkpoints.length > 0);
  assert.ok(host.checkpoints.every((checkpoint) => checkpoint.e01Runtime !== undefined));
  assert.ok(host.checkpoints.every((checkpoint) => checkpoint.checkpointPhase !== "model_stream_frame"));
  assert.ok(host.checkpoints.every((checkpoint) => checkpoint.checkpointPhase !== "message_delta"));
});

test("runtime checkpoints durable recovery boundaries instead of observations", () => {
  for (const phase of [
    "session_started",
    "model_request_prepared",
    "model_stream_report",
    "tool_batch_completed",
    "turn_end",
    "context_compacted",
    "session_suspended",
    "query_session_snapshot",
  ]) {
    assert.equal(shouldCheckpointRuntimePhase(phase), true, phase);
  }
  for (const phase of [
    "message_delta",
    "model_stream_frame",
    "turn_started",
    "stream_request_start",
    "tool_loop_plan",
    "tool_call_started",
    "tool_call_completed",
    "tool_use_summary",
    "upstream_query_continuation",
    "progressive_verification_requested",
  ]) {
    assert.equal(shouldCheckpointRuntimePhase(phase), false, phase);
  }
});

test("provider evidence keeps structural frames without replaying content tokens", () => {
  type Frame = Parameters<typeof providerControlPlaneEvidenceFrames>[0][number];
  const frame = (kind: Frame["kind"], sequence: number): Frame => ({
    frameId: `frame-${sequence}`,
    dispatchId: "dispatch-1",
    routeId: "route-1",
    sequence,
    kind,
    text: kind.endsWith("_delta") ? "token" : null,
    toolCallId: kind === "tool_call_delta" ? "call-1" : null,
    toolName: kind === "tool_call_delta" ? "read" : null,
    jsonDelta: kind === "tool_call_delta" ? "{}" : null,
    usage: {},
    providerEvent: kind === "response_end" ? "done" : null,
    createdAt: sequence,
    metadata: {},
  });
  const evidence = providerControlPlaneEvidenceFrames([
    frame("thinking_delta", 1),
    frame("text_delta", 2),
    frame("tool_call_delta", 3),
    frame("usage", 4),
    frame("response_end", 5),
  ]);
  assert.deepEqual(evidence.map((item) => item.kind), [
    "tool_call_delta",
    "usage",
    "response_end",
  ]);
  assert.equal(evidence[0]?.jsonDelta, null);
});

test("provider evidence bounds fragmented tool arguments to one structural frame per call", () => {
  type Frame = Parameters<typeof providerControlPlaneEvidenceFrames>[0][number];
  const frames: Frame[] = [{
    frameId: "tool-start",
    dispatchId: "dispatch-large-tool",
    routeId: "route-large-tool",
    sequence: 1,
    kind: "tool_call_delta",
    text: null,
    toolCallId: "call-large-tool",
    toolName: "shell",
    jsonDelta: "",
    usage: {},
    providerEvent: "chat.completion.chunk",
    createdAt: 1,
    metadata: { providerIndex: 0 },
  }];
  for (let sequence = 2; sequence <= 602; sequence += 1) {
    frames.push({
      ...frames[0]!,
      frameId: `tool-argument-${sequence}`,
      sequence,
      toolCallId: null,
      toolName: null,
      jsonDelta: "x",
      createdAt: sequence,
    });
  }
  frames.push({
    ...frames[0]!,
    frameId: "usage",
    sequence: 603,
    kind: "usage",
    toolCallId: null,
    toolName: null,
    jsonDelta: null,
    createdAt: 603,
  });

  const evidence = providerControlPlaneEvidenceFrames(frames);
  assert.equal(evidence.length, 2);
  assert.deepEqual(evidence.map((frame) => frame.kind), ["tool_call_delta", "usage"]);
  assert.equal(evidence[0]?.toolCallId, "call-large-tool");
  assert.equal(evidence[0]?.jsonDelta, null);
});

test("provider route renewal accepts a verified multi-hop pinned lineage", () => {
  const route = (
    routeId: string,
    previousRouteId: string | null,
    createdAt: number,
  ): ProviderRouteLease => ({
    routeId,
    previousRouteId,
    createdAt,
    expiresAt: createdAt + 1_000,
    runId: "run-route",
    taskId: "task-route",
    nodeId: "node-route",
    sessionId: "session-route",
    turnId: "turn-route",
    purpose: "reason",
    catalogRevision: 7,
    providerId: "deepseek",
    modelId: "deepseek-v4-flash",
    credentialId: "credential-route",
    credentialVersion: 3,
    credentialFingerprint: "sha256:credential",
    integrationId: "deepseek-bearer",
    transportId: "openai_chat",
    protocol: "openai_chat",
    baseUrl: "https://api.deepseek.com",
    allowedHosts: ["api.deepseek.com"],
    endpointPath: "/chat/completions",
    requestHeaders: {},
    requestDefaults: {},
    retryPolicy: {
      maximumAttempts: 3,
      baseDelayMilliseconds: 1,
      maximumDelayMilliseconds: 10,
      retryStatuses: [503],
      rotateCredentialOnAuthenticationFailure: true,
      rotateRouteOnProviderUnavailable: true,
    },
    reason: "provider routing; renewed expired route",
    checksum: `sha256:${routeId}`,
  });
  const original = route("route-1", null, 1);
  const child = route("route-2", original.routeId, 2);
  const leaf = route("route-3", child.routeId, 3);
  const routes = new Map([original, child, leaf].map((item) => [item.routeId, item]));

  assert.equal(
    assertProviderRouteRenewalLineage(
      leaf,
      original,
      { runId: original.runId, taskId: original.taskId },
      (routeId) => {
        const selected = routes.get(routeId);
        assert.ok(selected);
        return selected;
      },
    ),
    2,
  );

  const fork = route("route-fork", "route-unrelated", 4);
  assert.throws(
    () => assertProviderRouteRenewalLineage(
      fork,
      original,
      { runId: original.runId, taskId: original.taskId },
      (routeId) => {
        const selected = routes.get(routeId);
        assert.ok(selected);
        return selected;
      },
    ),
  );
});

test("provider control plane rejoins fragmented OpenAI tool arguments by provider index", () => {
  type Frame = Parameters<typeof providerControlPlaneToolSteps>[0][number];
  const frame = (
    sequence: number,
    providerIndex: number,
    toolCallId: string | null,
    toolName: string | null,
    jsonDelta: string | null,
  ): Frame => ({
    frameId: `frame-${sequence}`,
    dispatchId: "dispatch-1",
    routeId: "route-1",
    sequence,
    kind: "tool_call_delta",
    text: null,
    toolCallId,
    toolName,
    jsonDelta,
    usage: {},
    providerEvent: "chat.completion.chunk",
    createdAt: sequence,
    metadata: { providerIndex },
  });
  const steps = providerControlPlaneToolSteps([
    frame(1, 0, "call-shell", "shell", ""),
    frame(2, 0, null, null, '{"command":"ls'),
    frame(3, 0, null, null, ' -la"}'),
    frame(4, 1, "call-read", "file_read", ""),
    frame(5, 1, null, null, '{"path":"README.md"}'),
  ]);
  assert.deepEqual(steps.map((step) => ({
    id: step.step_id,
    name: step.tool_name,
    arguments: step.arguments,
  })), [
    { id: "call-shell", name: "shell", arguments: { command: "ls -la" } },
    { id: "call-read", name: "file_read", arguments: { path: "README.md" } },
  ]);
});

test("session restore preserves the unique active turn boundary", () => {
  const session = RuntimeSession.create(
    "active-session",
    "active-run-one",
    "active-task",
    "active-request",
    [{ role: "user", content: "resume the active turn" }],
  );
  session.beginTurn(0, "resume the active turn");
  const restored = RuntimeSession.restore(session.snapshot(), {
    sessionId: "active-session",
    runId: "active-run-two",
    taskId: "active-task",
    workerRequestId: "active-request",
  });
  assert.throws(() => restored.beginTurn(1, "must not overlap"), /query_turn_already_active/);
  restored.completeTurn(false, "interrupted_before_resume");
  assert.doesNotThrow(() => restored.beginTurn(1, "continue after deterministic settlement"));
});

test("runtime tool projection keeps gateway evidence identity without copying its audit envelope", () => {
  const eventRefs = Array.from({ length: 200 }, (_, index) => `gateway-event-${index}-${"x".repeat(80)}`);
  const output = {
    stdout: "verified output",
    return_code: 0,
    gateway_receipt: {
      schema: "zyra.gateway-execution-receipt.v1",
      receipt_id: "gateway-execution:test",
      receipt_digest: "sha256:receipt",
      outcome: "committed",
      result_digest: "sha256:result",
      invocation: {
        invocation_id: "gateway-invocation:test",
        tool_call_id: "call-projection",
        arguments_digest: "sha256:arguments",
        policy_digest: "sha256:policy",
        binding_digest: "sha256:binding",
        identity: { worker_id: "worker", payload: "y".repeat(10_000) },
      },
      event_refs: eventRefs,
      metadata: {
        return_code: 0,
        termination: "exited",
        workspace_mutation_committed: false,
        unbounded_diagnostics: "z".repeat(10_000),
      },
    },
  } satisfies JsonObject;

  const projected = projectToolOutputForRuntime(output);
  const encoded = JSON.stringify(projected);
  assert.ok(encoded.length < 2_000);
  assert.equal(projected.stdout, "verified output");
  assert.deepEqual(projected.gateway_receipt, {
    schema: "zyra.gateway-execution-receipt.v1",
    receipt_id: "gateway-execution:test",
    receipt_digest: "sha256:receipt",
    outcome: "committed",
    result_digest: "sha256:result",
    compacted_for_runtime: true,
    invocation_ref: {
      invocation_id: "gateway-invocation:test",
      tool_call_id: "call-projection",
      arguments_digest: "sha256:arguments",
      policy_digest: "sha256:policy",
      binding_digest: "sha256:binding",
    },
    metadata: {
      return_code: 0,
      termination: "exited",
      workspace_mutation_committed: false,
    },
    event_ref_count: 200,
  });

  const session = RuntimeSession.create(
    "projection-session",
    "projection-run",
    "projection-task",
    "projection-request",
    [{ role: "user", content: "Run the command" }],
  );
  session.beginTurn(0, "Run the command");
  session.recordToolCall("call-projection", "shell");
  session.recordToolResult("shell", {
    tool_call_id: "call-projection",
    ok: true,
    summary: "Sandbox command completed",
    output,
    artifacts: [],
    error: null,
    metadata: {},
  });
  const recorded = JSON.parse(session.messages.at(-1)?.content ?? "{}") as {
    output?: JsonObject;
  };
  assert.deepEqual(recorded.output, projected);
  assert.ok(!session.messages.at(-1)?.content.includes("gateway-event-199"));
});

test("durable compaction summary keeps objective progress verification and open work", async () => {
  const summary = await durableCompactionSummary({
    trigger: "auto_threshold",
    previousSummary: "",
    systemPrompt: "runtime",
    customInstructions: "preserve work",
    tokenBudget: 4_096,
    messages: [
      {
        id: "goal",
        role: "user",
        content: [{ type: "text", text: "Complete the release workflow." }],
        createdAt: new Date(0).toISOString(),
        turnIndex: null,
        apiRound: 0,
        synthetic: false,
        metadata: {},
      },
      {
        id: "decision",
        role: "assistant",
        content: [{ type: "text", text: "The migrations are complete; start the full stack next." }],
        createdAt: new Date(0).toISOString(),
        turnIndex: 18,
        apiRound: 18,
        synthetic: false,
        metadata: {},
      },
      {
        id: "verification",
        role: "user",
        content: [{
          type: "tool_result",
          toolUseId: "tests",
          content: "138 passed; api_key=must-not-survive; postgresql://worker:db-password@db/task",
          isError: false,
          createdAt: new Date(0).toISOString(),
          compacted: false,
        }],
        createdAt: new Date(0).toISOString(),
        turnIndex: 18,
        apiRound: 18,
        synthetic: false,
        metadata: {},
      },
    ],
  });

  assert.match(summary, /Complete the release workflow/);
  assert.match(summary, /migrations are complete/);
  assert.match(summary, /138 passed/);
  assert.match(summary, /Open work/);
  assert.match(summary, /not an implicit to-do list/);
  assert.match(summary, /without repeating completed inspection/);
  assert.doesNotMatch(summary, /must-not-survive/);
  assert.doesNotMatch(summary, /db-password/);
  assert.match(summary, /\[REDACTED\]/);
});

test("model compaction instructions require concrete source findings", async () => {
  const prompt = modelCompactionPrompt({
    trigger: "auto_threshold",
    previousSummary: "",
    systemPrompt: "runtime",
    customInstructions: "preserve source work",
    tokenBudget: 4_096,
    messages: [],
  });
  assert.match(prompt, /retain concrete symbols, responsibilities, defects, and cross-file relationships/);
  assert.match(prompt, /successful source read must not be summarized as unavailable or unknown/);
});

test("durable compaction carries verified failures across consecutive summary generations", async () => {
  const first = await durableCompactionSummary({
    trigger: "auto_threshold",
    previousSummary: "",
    systemPrompt: "runtime",
    customInstructions: "preserve verified outcomes",
    tokenBudget: 4_096,
    messages: [
      {
        id: "goal",
        role: "user",
        content: [{ type: "text", text: "Build and verify the release." }],
        createdAt: new Date(0).toISOString(),
        turnIndex: null,
        apiRound: 0,
        synthetic: false,
        metadata: {},
      },
      {
        id: "build-result",
        role: "user",
        content: [{
          type: "tool_result",
          toolUseId: "build-call",
          content: JSON.stringify({
            summary: "Sandbox command completed",
            output: {
              terminal_facts: { status: "completed", return_code: 0 },
              content_preview: "Docker Hub authorization failed; BUILD_STATUS=17",
              truncated: true,
            },
            error: null,
            state: "succeeded",
          }),
          isError: false,
          createdAt: new Date(0).toISOString(),
          compacted: false,
        }],
        createdAt: new Date(0).toISOString(),
        turnIndex: 1,
        apiRound: 1,
        synthetic: false,
        metadata: {},
      },
    ],
  });
  assert.match(first, /BUILD_STATUS=17/);
  assert.match(first, /return_code.{0,20}0/);

  const second = await durableCompactionSummary({
    trigger: "auto_threshold",
    previousSummary: first,
    systemPrompt: "runtime",
    customInstructions: "preserve verified outcomes",
    tokenBudget: 4_096,
    messages: [{
      id: "follow-up",
      role: "assistant",
      content: [{ type: "text", text: "Check whether the external registry recovered." }],
      createdAt: new Date(1).toISOString(),
      turnIndex: 2,
      apiRound: 2,
      synthetic: false,
      metadata: {},
    }],
  });
  assert.match(second, /Previous verified observations/);
  assert.match(second, /BUILD_STATUS=17/);
  assert.match(second, /Docker Hub authorization failed/);
});

test("durable compaction summary replaces recursive handoffs and retains concrete actions", async () => {
  const prior = [
    "## Active objective",
    "Repair the release workflow.",
    "",
    "## Current work",
    "The failing migration is isolated in services/api/migrations.py.",
    "",
    "## Open work",
    "Patch the migration and rerun pytest.",
  ].join("\n");
  const summary = await durableCompactionSummary({
    trigger: "auto_threshold",
    previousSummary: prior,
    systemPrompt: "runtime",
    customInstructions: "preserve work",
    tokenBudget: 4_096,
    messages: [
      {
        id: "old-handoff",
        role: "user",
        content: [{ type: "text", text: `## Active objective\n${prior}` }],
        createdAt: new Date(0).toISOString(),
        turnIndex: null,
        apiRound: 0,
        synthetic: true,
        metadata: { compact_summary: true },
      },
      {
        id: "edit",
        role: "assistant",
        content: [{
          type: "tool_use",
          id: "edit-call",
          name: "file_write",
          input: { path: "services/api/migrations.py", content: "fixed" },
        }],
        createdAt: new Date(0).toISOString(),
        turnIndex: 19,
        apiRound: 19,
        synthetic: false,
        metadata: {},
      },
    ],
  });

  assert.match(summary, /Repair the release workflow/);
  assert.match(summary, /services\/api\/migrations\.py/);
  assert.match(summary, /Patch the migration and rerun pytest/);
  assert.equal((summary.match(/## Active objective/g) ?? []).length, 1);
  assert.doesNotMatch(summary, /## Active objective\n## Active objective/);
});

test("provider control checkpoints externalize cumulative prompt content", () => {
  const marker = "large-provider-prompt-marker-".repeat(2_000);
  const projected = e01RuntimeEventPayload("model_request_prepared", {
    provider_request: {
      request_id: "provider-request-one",
      messages_digest: "sha256:prompt",
      tools_digest: "sha256:tools",
      messages: [{ role: "user", content: marker }],
      tools: [{
        type: "function",
        function: {
          name: "shell",
          description: marker,
          parameters: { type: "object", properties: { command: { type: "string" } } },
        },
      }],
      system: [],
    },
  });
  const serialized = JSON.stringify(projected);

  assert.equal(serialized.includes(marker), false);
  assert.match(serialized, /sha256:prompt/);
  assert.match(serialized, /provider_control_plane/);
  assert.ok(serialized.length < 2_000);
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
  const state = e01State(result);
  const canonicalMessages = result.sessionSnapshot.messages as Array<{
    message_id: string;
    metadata: Record<string, unknown>;
  }>;
  assert.equal(result.ok, true);
  assert.ok(result.artifacts.length >= 2);
  assert.ok(result.contextCompactionCount >= 1);
  assert.ok(state.compact.boundaries.length >= 1);
  assert.equal(state.compact.cleanup.generation, 1);
  assert.equal(state.compact.cleanup.classifierApprovalsCleared, 1);
  assert.equal(state.compact.cleanup.speculativeChecksCleared, 1);
  assert.equal(state.compact.cleanup.sessionMessageCacheCleared, 1);
  assert.equal(state.compact.cleanup.lastQuerySource, "ClaudeRuntimeCore.run");
  assert.equal(state.telemetry.compactGeneration, 1);
  assert.ok(state.telemetry.events.some((event) => event.name === "provider.prompt_cache.compacted"));
  assert.ok(canonicalMessages.some((message) => message.message_id.startsWith("compact-boundary-")));
  assert.ok(canonicalMessages.some((message) => message.metadata.compact_boundary !== undefined));
  assert.ok((state.journal.state.context?.revision ?? 0) > 0);
  assert.ok(host.events.some((event) => event.phase === "tool_result_budget_exceeded"));
  assert.ok(host.events.some((event) => event.phase === "context_compacted"));
});

test("runtime commits provider prompt usage and recovery state through default loop", async () => {
  const originalFetch = globalThis.fetch;
  let providerFetchCount = 0;
  const providerBodies: JsonObject[] = [];
  const providerUrls: string[] = [];
  globalThis.fetch = (async (
    resource: Parameters<typeof fetch>[0],
    init?: Parameters<typeof fetch>[1],
  ) => {
    providerFetchCount += 1;
    providerUrls.push(String(resource));
    providerBodies.push(JSON.parse(String(init?.body ?? "{}")) as JsonObject);
    if (providerFetchCount > 1) {
      const finalEvent = {
        id: "provider-final-message",
        object: "chat.completion.chunk",
        model: "zyra-local-code-model",
        choices: [{ index: 0, delta: { content: "The requested file was inspected." }, finish_reason: "stop" }],
        usage: { prompt_tokens: 20, completion_tokens: 6, total_tokens: 26 },
      };
      return new Response(`data: ${JSON.stringify(finalEvent)}\n\ndata: [DONE]\n\n`, {
        status: 200,
        headers: {
          "content-type": "text/event-stream",
          "request-id": "provider-final-upstream-request",
          "x-kong-upstream-latency": "8",
        },
      });
    }
    const toolEvent = {
      id: "provider-success-message",
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [{
        index: 0,
        delta: {
          tool_calls: [{
            index: 0,
            id: "provider-success-tool",
            type: "function",
            function: { name: "read", arguments: JSON.stringify({ path: "a" }) },
          }],
        },
        finish_reason: "tool_calls",
      }],
      usage: { prompt_tokens: 12, completion_tokens: 7, total_tokens: 19 },
    };
    return new Response(`data: ${JSON.stringify(toolEvent)}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: {
        "content-type": "text/event-stream",
        "request-id": "provider-success-upstream-request",
        "x-kong-upstream-latency": "12",
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
        // One tool-bearing turn still owns a final provider-only response
        // round; that round must not expand the executable tool budget.
        maxTurns: 1,
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
  assert.equal(providerFetchCount, 2);
  assert.deepEqual(providerUrls, [
    "https://provider.invalid/v1/chat/completions",
    "https://provider.invalid/v1/chat/completions",
  ]);
  assert.equal(providerBodies[0].stream, true);
  const providerTools = providerBodies[0].tools as JsonObject[];
  const firstProviderTool = (providerTools[0]?.function ?? {}) as JsonObject;
  const firstProviderParameters = (firstProviderTool.parameters ?? {}) as JsonObject;
  const firstProviderProperties = (firstProviderParameters.properties ?? {}) as JsonObject;
  const firstProviderPath = (firstProviderProperties.path ?? {}) as JsonObject;
  assert.equal(providerTools.length, 2);
  assert.equal(firstProviderTool.name, "read");
  assert.equal(firstProviderTool.description, "read");
  assert.equal(firstProviderParameters.type, "object");
  assert.deepEqual(firstProviderParameters.required, ["path"]);
  assert.equal(firstProviderParameters.additionalProperties, false);
  assert.equal(firstProviderPath.type, "string");
  assert.ok(Array.isArray(providerBodies[1].messages));
  assert.ok((providerBodies[1].messages as JsonObject[]).some((message) => message.role === "tool"));
  assert.equal(providerBodies[1].tools, undefined);
  assert.equal(providerBodies[1].tool_choice, undefined);
  assert.ok(
    (providerBodies[1].messages as JsonObject[]).some((message) =>
      /tool-turn budget is now exhausted.*Do not call any tool/i.test(
        String(message.content ?? ""),
      )
    ),
    JSON.stringify(providerBodies[1].messages),
  );
  assert.ok(successState.telemetry.prompts.length >= 1);
  assert.equal(successState.telemetry.samples.length, 2);
  assert.ok(successState.providerPrompt.lastPrompt?.fingerprint);
  assert.deepEqual(successState.providerRequests.requests.map((item) => item.status), ["completed", "completed"]);
  assert.deepEqual(successState.providerRequests.attempts.map((item) => item.status), ["succeeded", "succeeded"]);
  assert.equal(successState.providerRequests.chunks.length, 2);
  assert.deepEqual(successState.providerResponses.responses.map((item) => item.stopReason), ["tool_use", "end_turn"]);
  assert.equal(successState.providerRouting.states.find((item) => item.routeId === "compatible-default")?.successes, 2);
  assert.equal(successState.providerRouting.states.find((item) => item.routeId === "compatible-default")?.inFlight, 0);
  assert.deepEqual(successState.providerRateLimits.reservations.map((item) => item.status), ["committed", "committed"]);
  assert.equal(successState.providerRateLimits.buckets.find((item) => item.limitId === "compatible-default-requests")?.consumed, 2);
  const gatewayEvent = successState.telemetry.events.find((item) =>
    item.name === "provider.model_stream_report.gateway"
  );
  assert.equal(
    gatewayEvent?.attributes.detected_gateway,
    "kong",
    JSON.stringify(successState.telemetry.events),
  );
  assert.deepEqual(successState.provider.requests.map((item) => item.state), ["completed", "completed"]);
  assert.deepEqual(successState.providerTransport.requests.map((item) => item.state), ["completed", "completed"]);
  assert.equal(successState.providerCredentials.records.length, 1);
  assert.equal(successState.providerCredentials.records[0].totalSuccesses, 2);
  assert.deepEqual(successState.tools.calls.map((item) => item.state), ["succeeded"]);
  assert.ok(successState.tools.leases.every((item) => item.releasedAt !== null));
  assert.deepEqual(successState.toolResults.accumulators.map((item) => item.status), ["sealed"]);
  assert.deepEqual(successState.toolResults.deliveries.map((item) => item.success), [true]);
  assert.deepEqual(successState.session.effects.map((item) => item.state), ["committed"]);
  assert.ok(successState.session.messages.length >= 4);
  assert.deepEqual(successState.custody.providers.map((item) => item.state), ["succeeded", "succeeded"]);
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

test("auto compaction replaces the next provider request transcript", async () => {
  const originalFetch = globalThis.fetch;
  const requestBodies: JsonObject[] = [];
  let requestCount = 0;
  let agentRequestCount = 0;
  globalThis.fetch = (async (
    _resource: Parameters<typeof fetch>[0],
    init?: Parameters<typeof fetch>[1],
  ) => {
    requestCount += 1;
    const body = JSON.parse(String(init?.body ?? "{}")) as JsonObject;
    requestBodies.push(body);
    const messages = body.messages as JsonObject[];
    const isCompactionSummary = messages.some((message) =>
      JSON.stringify(message.content ?? "").includes("Create a replacement handoff summary")
    );
    if (isCompactionSummary) {
      const event = {
        id: `compact-summary-response-${requestCount}`,
        object: "chat.completion.chunk",
        model: "zyra-local-code-model",
        choices: [{
          index: 0,
          delta: { content: "## Active objective\nContinue the repair.\n\n## Current work\nThe latest read completed.\n\n## Pending work and next action\nContinue with the next tool." },
          finish_reason: "stop",
        }],
        usage: { prompt_tokens: 600, completion_tokens: 40, total_tokens: 640 },
      };
      return new Response(`data: ${JSON.stringify(event)}\n\ndata: [DONE]\n\n`, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }
    agentRequestCount += 1;
    if (agentRequestCount <= 2) {
      const event = {
        id: `compact-tool-response-${agentRequestCount}`,
        object: "chat.completion.chunk",
        model: "zyra-local-code-model",
        choices: [{
          index: 0,
          delta: {
            tool_calls: [{
              index: 0,
                id: `compact-tool-${agentRequestCount}`,
              type: "function",
              function: {
                name: "read",
                arguments: JSON.stringify({ path: agentRequestCount === 1 ? "a" : "b" }),
              },
            }],
          },
          finish_reason: "tool_calls",
        }],
        usage: { prompt_tokens: 1_000, completion_tokens: 10, total_tokens: 1_010 },
      };
      return new Response(`data: ${JSON.stringify(event)}\n\ndata: [DONE]\n\n`, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }
    const event = {
      id: "compact-final-response",
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [{ index: 0, delta: { content: "done" }, finish_reason: "stop" }],
      usage: { prompt_tokens: 200, completion_tokens: 5, total_tokens: 205 },
    };
    return new Response(`data: ${JSON.stringify(event)}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  const marker = "original-provider-context-marker-".repeat(1_000);
  let result: Awaited<ReturnType<ClaudeRuntimeCore["run"]>>;
  try {
    result = await new ClaudeRuntimeCore().run(input({
      runId: "provider-compact-run",
      sessionId: "provider-compact-session",
      workerRequestId: "provider-compact-request",
      messages: [{ role: "user", content: marker }],
      turns: [],
      config: {
        maxTurns: 2,
        maxQueryContextChars: 500,
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          model_api_key: "test-only-provider-key",
        },
      },
    }), new MemoryHost());
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(result.ok, true);
  assert.equal(agentRequestCount, 3);
  assert.equal(requestCount, 5);
  assert.ok(result.contextCompactionCount >= 2);
  const agentRequestBodies = requestBodies.filter((body) =>
    !(body.messages as JsonObject[]).some((message) =>
      JSON.stringify(message.content ?? "").includes("Create a replacement handoff summary")
    )
  );
  assert.equal(JSON.stringify(agentRequestBodies[0]).includes(marker), true);
  assert.equal(JSON.stringify(agentRequestBodies[1]).includes(marker), true);
  assert.equal(JSON.stringify(agentRequestBodies[2]).includes(marker), false);
  assert.match(JSON.stringify(agentRequestBodies[2]), /Compaction boundary/);
  const modelIteration = result.sessionSnapshot.modelIteration as JsonObject;
  assert.equal(JSON.stringify(modelIteration.transcript ?? []).includes(marker), false);
});

test("runtime continues a length-truncated provider turn before accepting completion", async () => {
  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  const requestBodies: JsonObject[] = [];
  globalThis.fetch = (async (
    _resource: Parameters<typeof fetch>[0],
    init?: Parameters<typeof fetch>[1],
  ) => {
    requestCount += 1;
    requestBodies.push(JSON.parse(String(init?.body ?? "{}")) as JsonObject);
    const choice = requestCount <= 3
      ? { index: 0, delta: { content: "partial analysis" }, finish_reason: "length" }
      : requestCount === 4
      ? {
        index: 0,
        delta: {
          tool_calls: [{
            index: 0,
            id: "post-truncation-read",
            type: "function",
            function: { name: "read", arguments: JSON.stringify({ path: "a" }) },
          }],
        },
        finish_reason: "tool_calls",
      }
      : { index: 0, delta: { content: "Implementation completed." }, finish_reason: "stop" };
    const payload = {
      id: `truncation-provider-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [choice],
      usage: { prompt_tokens: 8, completion_tokens: 4, total_tokens: 12 },
    };
    return new Response(`data: ${JSON.stringify(payload)}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new MemoryHost();
    const result = await new ClaudeRuntimeCore().run(input({
      runId: "provider-truncation-run",
      sessionId: "provider-truncation-session",
      workerRequestId: "provider-truncation-request",
      turns: [],
      config: {
        maxTurns: 8,
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          model_api_key: "test-only-provider-key",
        },
      },
    }), host);

    assert.equal(result.ok, true);
    assert.equal(requestCount, 5);
    assert.equal(host.batches.length, 1);
    assert.equal(host.events.filter((event) =>
      event.phase === "provider_length_continuation_requested"
    ).length, 3);
    assert.ok((requestBodies[1].messages as JsonObject[]).some((message) =>
      /reached its output limit.*make a concrete tool call/i.test(String(message.content ?? ""))
    ));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("benchmark deadline closeout removes tools and returns a final response", async () => {
  const originalFetch = globalThis.fetch;
  const requestBodies: JsonObject[] = [];
  globalThis.fetch = (async (
    _resource: Parameters<typeof fetch>[0],
    init?: Parameters<typeof fetch>[1],
  ) => {
    requestBodies.push(JSON.parse(String(init?.body ?? "{}")) as JsonObject);
    const payload = {
      id: "deadline-closeout-provider",
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [{
        index: 0,
        delta: { content: "Completed from durable workspace state." },
        finish_reason: "stop",
      }],
      usage: { prompt_tokens: 8, completion_tokens: 5, total_tokens: 13 },
    };
    return new Response(`data: ${JSON.stringify(payload)}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new MemoryHost();
    const result = await new ClaudeRuntimeCore().run(input({
      turns: [],
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          model_api_key: "test-only-provider-key",
          external_deadline_epoch_ms: Date.now() + 60_000,
          closeout_reserve_seconds: 120,
        },
      },
    }), host);

    assert.equal(result.ok, true);
    assert.equal(requestBodies.length, 1);
    assert.equal(((requestBodies[0].tools as unknown[]) ?? []).length, 0);
    assert.equal(host.batches.length, 0);
    assert.equal(result.metadata.execution_closeout_requested, "true");
    assert.ok(host.events.some((event) =>
      event.phase === "execution_resource_budget_accepted"
    ));
    assert.ok(host.events.some((event) =>
      event.phase === "execution_closeout_requested"
      && event.tools_advertised === 0
    ));
    assert.ok(host.events.some((event) =>
      event.phase === "execution_closeout_completed"
    ));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("benchmark deadline outside closeout keeps the open tool loop", async () => {
  const originalFetch = globalThis.fetch;
  const requestBodies: JsonObject[] = [];
  globalThis.fetch = (async (
    _resource: Parameters<typeof fetch>[0],
    init?: Parameters<typeof fetch>[1],
  ) => {
    requestBodies.push(JSON.parse(String(init?.body ?? "{}")) as JsonObject);
    const requestCount = requestBodies.length;
    const choice = requestCount === 1
      ? {
        index: 0,
        delta: {
          tool_calls: [{
            index: 0,
            id: "deadline-far-read",
            type: "function",
            function: { name: "read", arguments: JSON.stringify({ path: "a" }) },
          }],
        },
        finish_reason: "tool_calls",
      }
      : {
        index: 0,
        delta: { content: "Normal tool loop completed." },
        finish_reason: "stop",
      };
    const payload = {
      id: `deadline-far-provider-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [choice],
      usage: { prompt_tokens: 8, completion_tokens: 5, total_tokens: 13 },
    };
    return new Response(`data: ${JSON.stringify(payload)}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new MemoryHost();
    const result = await new ClaudeRuntimeCore().run(input({
      turns: [],
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          model_api_key: "test-only-provider-key",
          external_deadline_epoch_ms: Date.now() + 600_000,
          closeout_reserve_seconds: 60,
        },
      },
    }), host);

    assert.equal(result.ok, true);
    assert.equal(requestBodies.length, 2);
    assert.ok(((requestBodies[0].tools as unknown[]) ?? []).length > 0);
    assert.equal(host.batches.length, 1);
    assert.equal(result.metadata.execution_closeout_requested, "false");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("benchmark deadline closeout retries textual DSML instead of accepting it", async () => {
  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  const requestBodies: JsonObject[] = [];
  globalThis.fetch = (async (
    _resource: Parameters<typeof fetch>[0],
    init?: Parameters<typeof fetch>[1],
  ) => {
    requestCount += 1;
    requestBodies.push(JSON.parse(String(init?.body ?? "{}")) as JsonObject);
    const content = requestCount === 1
      ? '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="shell">late inspection</｜｜DSML｜｜invoke>'
      : "The requested artifact and required wait completed successfully.";
    const payload = {
      id: `deadline-dsml-provider-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [{ index: 0, delta: { content }, finish_reason: "stop" }],
      usage: { prompt_tokens: 8, completion_tokens: 5, total_tokens: 13 },
    };
    return new Response(`data: ${JSON.stringify(payload)}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new MemoryHost();
    const result = await new ClaudeRuntimeCore().run(input({
      turns: [],
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          model_api_key: "test-only-provider-key",
          external_deadline_epoch_ms: Date.now() + 120_000,
          closeout_reserve_seconds: 180,
        },
      },
    }), host);

    assert.equal(result.ok, true);
    assert.equal(requestBodies.length, 2);
    assert.ok(requestBodies.every((body) =>
      ((body.tools as unknown[]) ?? []).length === 0
    ));
    assert.equal(host.batches.length, 0);
    assert.ok(host.events.some((event) =>
      event.phase === "execution_closeout_retry_requested"
      && event.tools_advertised === 0
    ));
    assert.ok(host.events.some((event) =>
      event.phase === "execution_closeout_completed"
      && event.attempt === 2
    ));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("benchmark deadline crossing fences an unexecuted provider tool turn", async () => {
  const originalFetch = globalThis.fetch;
  const originalNow = Date.now;
  let nowMs = 2_000_000_000_000;
  let requestCount = 0;
  const requestBodies: JsonObject[] = [];
  Date.now = () => nowMs;
  globalThis.fetch = (async (
    _resource: Parameters<typeof fetch>[0],
    init?: Parameters<typeof fetch>[1],
  ) => {
    requestCount += 1;
    requestBodies.push(JSON.parse(String(init?.body ?? "{}")) as JsonObject);
    const choice = requestCount === 1
      ? {
        index: 0,
        delta: {
          tool_calls: [{
            index: 0,
            id: "late-tool-proposal",
            type: "function",
            function: { name: "write", arguments: JSON.stringify({ path: "late", content: "no" }) },
          }],
        },
        finish_reason: "tool_calls",
      }
      : {
        index: 0,
        delta: { content: "Finalized without starting the late side effect." },
        finish_reason: "stop",
      };
    if (requestCount === 1) nowMs += 100_000;
    const payload = {
      id: `deadline-crossing-provider-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [choice],
      usage: { prompt_tokens: 8, completion_tokens: 5, total_tokens: 13 },
    };
    return new Response(`data: ${JSON.stringify(payload)}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new MemoryHost();
    const result = await new ClaudeRuntimeCore().run(input({
      turns: [],
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          model_api_key: "test-only-provider-key",
          external_deadline_epoch_ms: nowMs + 120_000,
          closeout_reserve_seconds: 60,
        },
      },
    }), host);

    assert.equal(result.ok, true);
    assert.equal(requestBodies.length, 2);
    assert.ok(((requestBodies[0].tools as unknown[]) ?? []).length > 0);
    assert.equal(((requestBodies[1].tools as unknown[]) ?? []).length, 0);
    assert.equal(host.batches.length, 0);
    assert.equal(result.toolCallCount, 0);
    assert.equal(result.metadata.execution_closeout_requested, "true");
  } finally {
    Date.now = originalNow;
    globalThis.fetch = originalFetch;
  }
});

test("runtime fails closed after bounded length continuations are exhausted", async () => {
  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  globalThis.fetch = (async () => {
    requestCount += 1;
    const payload = {
      id: `bounded-truncation-provider-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [{
        index: 0,
        delta: { content: `partial analysis ${requestCount}` },
        finish_reason: "length",
      }],
      usage: { prompt_tokens: 8, completion_tokens: 4, total_tokens: 12 },
    };
    return new Response(`data: ${JSON.stringify(payload)}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new MemoryHost();
    const result = await new ClaudeRuntimeCore().run(input({
      runId: "provider-truncation-exhausted-run",
      sessionId: "provider-truncation-exhausted-session",
      workerRequestId: "provider-truncation-exhausted-request",
      turns: [],
      config: {
        maxTurns: 8,
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          model_api_key: "test-only-provider-key",
          max_length_continuations: 2,
        },
      },
    }), host);

    assert.equal(result.ok, false);
    assert.equal(result.stoppedReason, "model_output_truncated");
    assert.equal(requestCount, 3);
    assert.equal(host.events.filter((event) =>
      event.phase === "provider_length_continuation_requested"
    ).length, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
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

test("model loop observes a failed command and completes a later repair", async () => {
  class RecoveringHost extends MemoryHost {
    executionCount = 0;

    override async executeBatch(
      batch: ToolBatch,
      requests: ToolExecutionRequest[],
    ): Promise<ToolExecutionResponse[]> {
      this.executionCount += 1;
      if (this.executionCount === 1) {
        this.batches.push(batch);
        return requests.map((request) => ({
          tool_call_id: request.toolCallId,
          ok: false,
          summary: "Command exited with a recoverable conflict",
          output: { return_code: 1, stderr: "merge conflict" },
          artifacts: [],
          error: "sandbox_command_failed",
          metadata: { termination: "exited", recovery_required: "false" },
        }));
      }
      return super.executeBatch(batch, requests);
    }
  }

  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  globalThis.fetch = (async () => {
    requestCount += 1;
    const toolCall = requestCount <= 2
      ? {
        index: 0,
        id: requestCount === 1 ? "conflicting-command" : "repair-command",
        type: "function",
        function: {
          name: "read",
          arguments: JSON.stringify({ path: requestCount === 1 ? "conflict" : "resolved" }),
        },
      }
      : null;
    const payload = {
      id: `recoverable-provider-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [{
        index: 0,
        delta: toolCall === null
          ? { content: "Conflict resolved." }
          : { tool_calls: [toolCall] },
        finish_reason: toolCall === null ? "stop" : "tool_calls",
      }],
      usage: { prompt_tokens: 8, completion_tokens: 4, total_tokens: 12 },
    };
    return new Response(`data: ${JSON.stringify(payload)}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new RecoveringHost();
    const result = await new ClaudeRuntimeCore().run(input({
      runId: "tool-recovery-run",
      sessionId: "tool-recovery-session",
      workerRequestId: "tool-recovery-request",
      turns: [],
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          model_api_key: "test-only-provider-key",
        },
      },
    }), host);
    assert.equal(result.ok, true);
    assert.equal(requestCount, 3);
    assert.equal(host.executionCount, 2);
    assert.ok(host.events.some((event) => event.phase === "continue"));
    assert.deepEqual(e01State(result).tools.calls.map((item) => item.state), [
      "failed",
      "succeeded",
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("model loop can inspect and repair after a settled sandbox command timeout", async () => {
  class TimeoutRecoveringHost extends MemoryHost {
    executionCount = 0;

    override async executeBatch(
      batch: ToolBatch,
      requests: ToolExecutionRequest[],
    ): Promise<ToolExecutionResponse[]> {
      this.executionCount += 1;
      if (this.executionCount === 1) {
        this.batches.push(batch);
        return requests.map((request) => ({
          tool_call_id: request.toolCallId,
          ok: false,
          summary: "Sandbox command reached its execution deadline",
          output: { termination: "timed_out" },
          artifacts: [],
          error: "process_timeout",
          metadata: {
            termination: "timed_out",
            recovery_required: "true",
            command_timeout_settled: "true",
            model_recovery_allowed: "true",
            process_tree_controlled: "true",
          },
        }));
      }
      return super.executeBatch(batch, requests);
    }
  }

  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  globalThis.fetch = (async () => {
    requestCount += 1;
    const toolCall = requestCount <= 2
      ? {
        index: 0,
        id: requestCount === 1 ? "long-command" : "inspect-after-timeout",
        type: "function",
        function: {
          name: "read",
          arguments: JSON.stringify({ path: requestCount === 1 ? "install" : "state" }),
        },
      }
      : null;
    const payload = {
      id: `timeout-recovery-provider-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [{
        index: 0,
        delta: toolCall === null
          ? { content: "Recovered after inspecting the timed-out command." }
          : { tool_calls: [toolCall] },
        finish_reason: toolCall === null ? "stop" : "tool_calls",
      }],
      usage: { prompt_tokens: 8, completion_tokens: 4, total_tokens: 12 },
    };
    return new Response(`data: ${JSON.stringify(payload)}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new TimeoutRecoveringHost();
    const result = await new ClaudeRuntimeCore().run(input({
      runId: "settled-timeout-recovery-run",
      sessionId: "settled-timeout-recovery-session",
      workerRequestId: "settled-timeout-recovery-request",
      turns: [],
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          model_api_key: "test-only-provider-key",
        },
      },
    }), host);
    assert.equal(result.ok, true);
    assert.equal(requestCount, 3);
    assert.equal(host.executionCount, 2);
    assert.ok(host.events.some((event) =>
      event.phase === "watchdog_signal"
      && ((event.watchdog_signal ?? {}) as JsonObject).action === "continue"
    ));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("model loop stops three identical failed tool calls instead of burning the turn budget", async () => {
  class AlwaysFailingHost extends MemoryHost {
    executionCount = 0;

    override async executeBatch(
      batch: ToolBatch,
      requests: ToolExecutionRequest[],
    ): Promise<ToolExecutionResponse[]> {
      this.executionCount += 1;
      this.batches.push(batch);
      return requests.map((request) => ({
        tool_call_id: request.toolCallId,
        ok: false,
        summary: "SandboxGateway rejected the command",
        output: { reason: "remove shell composition" },
        artifacts: [],
        error: "ValueError",
        metadata: { termination: "exited", recovery_required: "false" },
      }));
    }
  }

  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  globalThis.fetch = (async () => {
    requestCount += 1;
    const payload = {
      id: `repeated-failure-provider-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [{
        index: 0,
        delta: {
          tool_calls: [{
            index: 0,
            id: `repeated-command-${requestCount}`,
            type: "function",
            function: {
              name: "read",
              arguments: JSON.stringify({ path: "unchanged" }),
            },
          }],
        },
        finish_reason: "tool_calls",
      }],
      usage: { prompt_tokens: 8, completion_tokens: 4, total_tokens: 12 },
    };
    return new Response(`data: ${JSON.stringify(payload)}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new AlwaysFailingHost();
    const result = await new ClaudeRuntimeCore().run(input({
      runId: "repeated-failure-run",
      sessionId: "repeated-failure-session",
      workerRequestId: "repeated-failure-request",
      turns: [],
      config: {
        maxTurns: 20,
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          model_api_key: "test-only-provider-key",
        },
      },
    }), host);

    assert.equal(result.ok, false);
    assert.equal(result.stoppedReason, "repeated_tool_failure");
    assert.equal(host.executionCount, 3);
    assert.equal(requestCount, 3);
    assert.equal(result.metadata.repeated_tool_failure_trips, "1");
    assert.ok(host.events.some((event) =>
      event.phase === "watchdog_signal"
      && ((event.watchdog_signal ?? {}) as JsonObject).action === "stop"
    ));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("model loop fails closed when a tool outcome requires reconciliation", async () => {
  class IndeterminateHost extends MemoryHost {
    override async executeBatch(
      batch: ToolBatch,
      requests: ToolExecutionRequest[],
    ): Promise<ToolExecutionResponse[]> {
      this.batches.push(batch);
      return requests.map((request) => ({
        tool_call_id: request.toolCallId,
        ok: false,
        summary: "Command timed out with an indeterminate outcome",
        output: {},
        artifacts: [],
        error: "tool_execution_timeout",
        metadata: { termination: "timed_out", recovery_required: "true" },
      }));
    }
  }

  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  globalThis.fetch = (async () => {
    requestCount += 1;
    const payload = {
      id: "indeterminate-provider-message",
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [{
        index: 0,
        delta: {
          tool_calls: [{
            index: 0,
            id: "indeterminate-command",
            type: "function",
            function: { name: "read", arguments: JSON.stringify({ path: "a" }) },
          }],
        },
        finish_reason: "tool_calls",
      }],
      usage: { prompt_tokens: 8, completion_tokens: 4, total_tokens: 12 },
    };
    return new Response(`data: ${JSON.stringify(payload)}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new IndeterminateHost();
    const result = await new ClaudeRuntimeCore().run(input({
      runId: "tool-indeterminate-run",
      sessionId: "tool-indeterminate-session",
      workerRequestId: "tool-indeterminate-request",
      turns: [],
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          model_api_key: "test-only-provider-key",
        },
      },
    }), host);
    assert.equal(result.ok, false);
    assert.equal(result.stoppedReason, "tool_execution_timeout");
    assert.equal(requestCount, 1);
    const watchdog = host.events.find((event) => event.phase === "watchdog_signal");
    assert.equal((watchdog?.watchdog_signal as JsonObject).action, "stop");
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
        headers: { "retry-after": "0.6" },
      });
    }
    const payload = requestCount === 2
      ? {
        id: "retry-provider-message",
        object: "chat.completion.chunk",
        model: "zyra-local-code-model",
        choices: [{
          index: 0,
          delta: {
            tool_calls: [{
              index: 0,
              id: "retry-tool-call",
              type: "function",
              function: { name: "read", arguments: JSON.stringify({ path: "a" }) },
            }],
          },
          finish_reason: "tool_calls",
        }],
        usage: { prompt_tokens: 4, completion_tokens: 3, total_tokens: 7 },
      }
      : {
        id: "retry-provider-final",
        object: "chat.completion.chunk",
        model: "zyra-local-code-model",
        choices: [{ index: 0, delta: { content: "Retry completed." }, finish_reason: "stop" }],
        usage: { prompt_tokens: 8, completion_tokens: 2, total_tokens: 10 },
      };
    return new Response(`data: ${JSON.stringify(payload)}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const retryHost = new MemoryHost();
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
    }), retryHost);
    const state = e01State(result);
    assert.equal(result.ok, true);
    assert.equal(requestCount, 3);
    assert.equal(state.recovery.contexts.length, 0);
    const failedRetryReport = retryHost.events.find((event) => {
      const stream = (event.model_stream ?? {}) as JsonObject;
      return event.phase === "model_stream_report" && stream.ok === false;
    });
    const failedRetryStream = (failedRetryReport?.model_stream ?? {}) as JsonObject;
    const retryPlan = (failedRetryStream.recovery_plan ?? {}) as JsonObject;
    assert.equal(retryPlan.delayMs, 600);
    assert.ok(retryHost.events.some((event) => event.phase === "api_retry_report"));
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
  const firstState = e01State(first);
  const resumedState = e01State(resumed);
  assert.equal(resumed.ok, true);
  assert.equal(resumed.metadata.restored, "true");
  assert.equal(
    (resumed.sessionSnapshot.lineage as { restored?: boolean } | null)?.restored,
    true,
  );
  assert.ok(resumedState.journal.revision > firstState.journal.revision);
  assert.ok(resumedState.query.revision > firstState.query.revision);
  assert.ok(resumedState.session.messages.length > firstState.session.messages.length);
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

test("required delivery turns an analysis-only final into an incremental tool action", async () => {
  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  globalThis.fetch = (async () => {
    requestCount += 1;
    const choice = requestCount === 1
      ? { index: 0, delta: { content: "I have enough information to describe the change." }, finish_reason: "stop" }
      : requestCount === 2
        ? {
          index: 0,
          delta: {
            tool_calls: [{
              index: 0,
              id: "progressive-write",
              type: "function",
              function: { name: "write", arguments: '{"path":"draft.txt","content":"minimum result"}' },
            }],
          },
          finish_reason: "tool_calls",
        }
        : { index: 0, delta: { content: "The durable result was created and checked." }, finish_reason: "stop" };
    return new Response(`data: ${JSON.stringify({
      id: `progressive-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [choice],
    })}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new MemoryHost();
    const result = await new ClaudeRuntimeCore().run(input({
      turns: [],
      metadata: {
        delivery_contract: {
          workspace_mutation_required: true,
          verification_required: false,
        },
      },
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
        },
      },
    }), host);

    assert.equal(result.ok, true);
    assert.equal(requestCount, 3);
    assert.equal(host.batches.length, 1);
    assert.equal(host.batches[0]?.steps[0]?.tool_name, "write");
    assert.ok(
      host.events.some((event) => event.phase === "progressive_action_requested"),
      JSON.stringify(host.events.map((event) => event.phase)),
    );
    assert.equal(result.metadata.progressive_real_actions, "1");
    assert.equal(result.metadata.progressive_action_nudges, "1");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("delivered workspace state requests behavioral verification before final response", async () => {
  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  globalThis.fetch = (async () => {
    requestCount += 1;
    const choice = requestCount === 1
      ? {
        index: 0,
        delta: {
          tool_calls: [{
            index: 0,
            id: "verified-write",
            type: "function",
            function: {
              name: "write",
              arguments: '{"path":"result.txt","content":"delivered"}',
            },
          }],
        },
        finish_reason: "tool_calls",
      }
      : requestCount === 2
        ? {
          index: 0,
          delta: { content: "The requested result is complete." },
          finish_reason: "stop",
        }
        : requestCount === 3
          ? {
            index: 0,
            delta: {
              tool_calls: [{
                index: 0,
                id: "behavioral-test",
                type: "function",
                function: {
                  name: "shell",
                  arguments: '{"command":"python -m pytest tests -q"}',
                },
              }],
            },
            finish_reason: "tool_calls",
          }
          : {
            index: 0,
            delta: { content: "The requested result is complete and the tests passed." },
            finish_reason: "stop",
          };
    return new Response(`data: ${JSON.stringify({
      id: `verification-progress-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [choice],
    })}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new MemoryHost();
    const result = await new ClaudeRuntimeCore().run(input({
      turns: [],
      metadata: {
        delivery_contract: {
          workspace_mutation_required: true,
        },
      },
      tools: [
        ...(input().tools ?? []),
        {
          name: "shell",
          purpose: "execute a shell command",
          source: "test",
          input_schema: {
            type: "object",
            required: ["command"],
            properties: { command: { type: "string" } },
          },
          output_schema: {},
          metadata: { read_only: "false", concurrency_safe: "false" },
        },
      ],
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
        },
      },
    }), host);

    assert.equal(result.ok, true);
    assert.equal(requestCount, 4);
    assert.deepEqual(
      host.batches.flatMap((batch) => batch.steps.map((step) => step.tool_name)),
      ["write", "shell"],
    );
    assert.ok(
      host.events.some((event) => event.phase === "progressive_verification_requested"),
      JSON.stringify(host.events.map((event) => event.phase)),
    );
    assert.equal(result.metadata.progressive_verifications, "1");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("required delivery redirects repeated read-only inspection into execution", async () => {
  const originalFetch = globalThis.fetch;
  const requestBodies: string[] = [];
  let requestCount = 0;
  globalThis.fetch = (async (_input: unknown, init?: RequestInit) => {
    requestCount += 1;
    requestBodies.push(String(init?.body ?? ""));
    const choice = requestCount <= 2
      ? {
        index: 0,
        delta: {
          tool_calls: [{
            index: 0,
            id: `inspection-${requestCount}`,
            type: "function",
            function: {
              name: "read",
              arguments: JSON.stringify({ path: `source-${requestCount}.ts` }),
            },
          }],
        },
        finish_reason: "tool_calls",
      }
      : requestCount === 3
        ? {
          index: 0,
          delta: {
            tool_calls: [{
              index: 0,
              id: "delivery-write",
              type: "function",
              function: {
                name: "write",
                arguments: '{"path":"result.txt","content":"delivered"}',
              },
            }],
          },
          finish_reason: "tool_calls",
        }
        : {
          index: 0,
          delta: { content: "The requested result was implemented and verified." },
          finish_reason: "stop",
        };
    return new Response(`data: ${JSON.stringify({
      id: `inspection-progress-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [choice],
    })}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new MemoryHost();
    const result = await new ClaudeRuntimeCore().run(input({
      turns: [],
      metadata: {
        delivery_contract: {
          workspace_mutation_required: true,
          verification_required: false,
        },
      },
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          pre_delivery_observation_nudge_after: 2,
        },
      },
    }), host);

    assert.equal(result.ok, true);
    assert.equal(requestCount, 4);
    assert.deepEqual(
      host.batches.flatMap((batch) => batch.steps.map((step) => step.tool_name)),
      ["read", "read", "write"],
    );
    assert.match(requestBodies[2] ?? "", /Stop broad repository inspection/);
    assert.match(requestBodies[2] ?? "", /map every public requirement and acceptance condition/);
    assert.ok(
      host.events.some((event) => (
        event.phase === "progressive_action_requested"
        && event.consecutive_pre_delivery_observations === 2
      )),
    );
    assert.equal(result.metadata.progressive_action_nudges, "1");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("unverified restored delivery requests verification after a tool observation", async () => {
  const originalFetch = globalThis.fetch;
  const requestBodies: string[] = [];
  let requestCount = 0;
  globalThis.fetch = (async (_input: unknown, init?: RequestInit) => {
    requestCount += 1;
    requestBodies.push(String(init?.body ?? ""));
    const choice = requestCount === 1
      ? {
        index: 0,
        delta: {
          tool_calls: [{
            index: 0,
            id: "post-restore-read",
            type: "function",
            function: { name: "read", arguments: '{"path":"tests/public/test_metrics.py"}' },
          }],
        },
        finish_reason: "tool_calls",
      }
      : requestCount === 2
        ? {
          index: 0,
          delta: {
            tool_calls: [{
              index: 0,
              id: "post-restore-tests",
              type: "function",
              function: { name: "shell", arguments: '{"command":"python -m pytest tests -q"}' },
            }],
          },
          finish_reason: "tool_calls",
        }
        : {
          index: 0,
          delta: { content: "The restored delivery is now behaviorally verified." },
          finish_reason: "stop",
        };
    return new Response(`data: ${JSON.stringify({
      id: `post-tool-verification-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [choice],
    })}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new MemoryHost();
    const result = await new ClaudeRuntimeCore().run(input({
      turns: [],
      metadata: {
        delivery_contract: { workspace_mutation_required: true },
        task_handoff_progress: {
          requiredDeliveryMissing: false,
          workspaceMutationCount: 3,
          verificationCount: 0,
        },
      },
      tools: [
        ...(input().tools ?? []),
        {
          name: "shell",
          purpose: "execute a shell command",
          source: "test",
          input_schema: {
            type: "object",
            required: ["command"],
            properties: { command: { type: "string" } },
          },
          output_schema: {},
          metadata: { read_only: "false", concurrency_safe: "false" },
        },
      ],
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
        },
      },
    }), host);

    assert.equal(result.ok, true);
    assert.equal(requestCount, 3);
    assert.deepEqual(
      host.batches.flatMap((batch) => batch.steps.map((step) => step.tool_name)),
      ["read", "shell"],
    );
    assert.match(requestBodies[1] ?? "", /Before continuing broad inspection/);
    assert.ok(host.events.some((event) => (
      event.phase === "progressive_verification_requested"
      && event.post_tool === true
    )));
    assert.equal(result.metadata.progressive_verifications, "1");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("required delivery circuit declines further inspection and accepts the next edit", async () => {
  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  globalThis.fetch = (async () => {
    requestCount += 1;
    const choice = requestCount <= 5
      ? {
        index: 0,
        delta: {
          tool_calls: [{
            index: 0,
            id: `circuit-read-${requestCount}`,
            type: "function",
            function: { name: "read", arguments: JSON.stringify({ path: `source-${requestCount}.ts` }) },
          }],
        },
        finish_reason: "tool_calls",
      }
      : requestCount === 6
        ? {
          index: 0,
          delta: {
            tool_calls: [{
              index: 0,
              id: "circuit-write",
              type: "function",
              function: { name: "write", arguments: '{"path":"result.txt","content":"delivered"}' },
            }],
          },
          finish_reason: "tool_calls",
        }
        : { index: 0, delta: { content: "Implemented and verified." }, finish_reason: "stop" };
    return new Response(`data: ${JSON.stringify({
      id: `inspection-circuit-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [choice],
    })}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new MemoryHost();
    const result = await new ClaudeRuntimeCore().run(input({
      turns: [],
      metadata: {
        delivery_contract: {
          workspace_mutation_required: true,
          verification_required: false,
        },
      },
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          pre_delivery_observation_nudge_after: 2,
          pre_delivery_inspection_block_after_nudges: 2,
        },
      },
    }), host);

    assert.equal(result.ok, true);
    assert.equal(requestCount, 7);
    assert.deepEqual(
      host.batches.flatMap((batch) => batch.steps.map((step) => step.tool_name)),
      ["read", "read", "read", "read", "write"],
    );
    assert.ok(host.events.some((event) => event.phase === "pre_delivery_inspection_blocked"));
    assert.equal(result.metadata.progressive_action_nudges, "2");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("required delivery circuit applies cross-session handoff progress before tools run", async () => {
  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  globalThis.fetch = (async () => {
    requestCount += 1;
    const choice = requestCount === 1
      ? {
        index: 0,
        delta: {
          tool_calls: [{
            index: 0,
            id: "continued-read",
            type: "function",
            function: { name: "read", arguments: '{"path":"another-source.ts"}' },
          }],
        },
        finish_reason: "tool_calls",
      }
      : requestCount === 2
        ? {
          index: 0,
          delta: {
            tool_calls: [{
              index: 0,
              id: "continued-write",
              type: "function",
              function: { name: "write", arguments: '{"path":"result.txt","content":"delivered"}' },
            }],
          },
          finish_reason: "tool_calls",
        }
        : { index: 0, delta: { content: "Implemented and verified." }, finish_reason: "stop" };
    return new Response(`data: ${JSON.stringify({
      id: `continued-circuit-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [choice],
    })}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new MemoryHost();
    const result = await new ClaudeRuntimeCore().run(input({
      turns: [],
      metadata: {
        delivery_contract: {
          workspace_mutation_required: true,
          verification_required: false,
        },
        task_handoff_progress: {
          requiredDeliveryMissing: true,
          providerRounds: 9,
          preDeliveryObservationCount: 6,
          consecutivePreDeliveryObservations: 6,
          actionNudgeCount: 4,
          lastActionNudgeObservationCount: 6,
          lastActionNudgeProviderRound: 7,
        },
      },
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          pre_delivery_inspection_block_after_nudges: 3,
        },
      },
    }), host);

    assert.equal(result.ok, true);
    assert.equal(requestCount, 3);
    assert.deepEqual(
      host.batches.flatMap((batch) => batch.steps.map((step) => step.tool_name)),
      ["write"],
    );
    assert.ok(host.events.some((event) => event.phase === "pre_delivery_inspection_blocked"));
    assert.equal(result.metadata.progressive_action_nudges, "4");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("failed delivery attempt permits one recovery inspection before the circuit closes again", async () => {
  const originalFetch = globalThis.fetch;
  let requestCount = 0;
  globalThis.fetch = (async () => {
    requestCount += 1;
    const tool = requestCount === 1
      ? { name: "write", arguments: '{"path":"result.txt","content":"stale","fail":true}' }
      : requestCount <= 3
        ? { name: "read", arguments: JSON.stringify({ path: `target-${requestCount}.txt` }) }
        : requestCount === 4
          ? { name: "write", arguments: '{"path":"result.txt","content":"delivered"}' }
          : null;
    const choice = tool
      ? {
        index: 0,
        delta: {
          tool_calls: [{
            index: 0,
            id: `recovery-inspection-${requestCount}`,
            type: "function",
            function: tool,
          }],
        },
        finish_reason: "tool_calls",
      }
      : { index: 0, delta: { content: "Implemented and verified." }, finish_reason: "stop" };
    return new Response(`data: ${JSON.stringify({
      id: `recovery-inspection-round-${requestCount}`,
      object: "chat.completion.chunk",
      model: "zyra-local-code-model",
      choices: [choice],
    })}\n\ndata: [DONE]\n\n`, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as unknown as typeof fetch;
  try {
    const host = new MemoryHost();
    const result = await new ClaudeRuntimeCore().run(input({
      turns: [],
      metadata: {
        delivery_contract: {
          workspace_mutation_required: true,
          verification_required: false,
        },
        task_handoff_progress: {
          requiredDeliveryMissing: true,
          providerRounds: 9,
          actionNudgeCount: 4,
        },
      },
      config: {
        runtimeConstraints: {
          model_transport: "http_sse",
          model_api_base_url: "https://provider.invalid/v1",
          pre_delivery_inspection_block_after_nudges: 3,
        },
      },
    }), host);

    assert.equal(result.ok, true);
    assert.equal(requestCount, 5);
    assert.deepEqual(
      host.batches.flatMap((batch) => batch.steps.map((step) => step.tool_name)),
      ["write", "read", "write"],
    );
    assert.equal(
      host.events.filter((event) => event.phase === "pre_delivery_inspection_blocked").length,
      1,
    );
    assert.equal(result.metadata.progressive_real_actions, "2");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("progressive execution requests action after analysis-only loops and records delivery", () => {
  let clock = 1_000;
  const progressive = new ProgressiveExecutionRuntime({
    now: () => clock,
    constraints: {},
    deliveryContract: {
      workspace_mutation_required: true,
      verification_required: false,
    },
  });
  progressive.observeProviderRound("Inspect the repository and consider options.", 0);
  clock += 1_000;
  progressive.observeProviderRound("Inspect the repository and consider options.", 0);
  const stalled = progressive.decide(1_000, 10_000);

  assert.equal(stalled.action, "nudge_action");
  assert.equal(stalled.snapshot.requiredDeliveryMissing, true);
  assert.equal(stalled.snapshot.repeatedAnalysisRounds, 1);

  progressive.observeToolResult({
    toolCallId: "write-1",
    toolName: "write",
    arguments: { path: "result.txt" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "batch-1",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: {},
  }, {
    tool_call_id: "write-1",
    ok: true,
    summary: "created an incremental result",
    output: {},
    artifacts: [{ artifact_id: "artifact-1", kind: "file", uri: "workspace:result.txt", title: "result" }],
    metadata: {
      physical_effect_executed: "true",
      workspace_mutation_committed: "true",
    },
  }, false);
  const delivered = progressive.decide(2_000, 10_000);

  assert.equal(delivered.action, "continue");
  assert.equal(delivered.snapshot.requiredDeliveryMissing, false);
  assert.equal(delivered.snapshot.workspaceMutationCount, 1);
  assert.equal(delivered.snapshot.artifactCount, 1);
});

test("progressive execution keeps verification debt until a behavioral command passes", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
  });
  const response = (
    toolCallId: string,
    workspaceMutationCommitted = false,
  ): ToolExecutionResponse => ({
    tool_call_id: toolCallId,
    ok: true,
    summary: "command completed",
    output: {},
    artifacts: [],
    metadata: {
      workspace_mutation_committed: String(workspaceMutationCommitted),
    },
  });
  const request = (
    toolCallId: string,
    metadata: JsonObject = {},
  ): ToolExecutionRequest => ({
    toolCallId,
    toolName: "shell",
    arguments: { command: "command" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "batch-verification",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata,
  });

  progressive.observeToolResult(
    request("delivery"),
    response("delivery", true),
    false,
  );
  assert.equal(progressive.decide(1_000, 10_000).action, "nudge_verification");

  progressive.observeToolResult(
    request("format-check"),
    response("format-check"),
    true,
  );
  assert.equal(progressive.snapshot().verificationCount, 0);
  const nudged = progressive.recordVerificationNudge();
  assert.equal(nudged.verificationNudgeCount, 1);

  progressive.observeToolResult(
    request("tests", { progressive_verification_driving: true }),
    response("tests"),
    false,
  );
  assert.equal(progressive.snapshot().verificationCount, 1);
  assert.equal(progressive.decide(1_000, 10_000).action, "continue");

  progressive.observeToolResult(
    request("later-delivery"),
    response("later-delivery", true),
    false,
  );
  assert.equal(progressive.snapshot().verificationCount, 0);
  assert.equal(progressive.snapshot().verificationNudgeCount, 0);
  assert.equal(progressive.decide(1_000, 10_000).action, "nudge_verification");
});

test("progressive execution restores unverified delivery debt across fenced sessions", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      providerRounds: 12,
      realActionCount: 9,
      workspaceMutationCount: 3,
      verificationCount: 0,
      verificationNudgeCount: 1,
      lastVerificationNudgeProviderRound: 12,
      artifactCount: 0,
    },
  });

  const restored = progressive.snapshot();
  assert.equal(restored.requiredDeliveryMissing, false);
  assert.equal(restored.workspaceMutationCount, 3);
  assert.equal(restored.verificationCount, 0);
  assert.equal(restored.verificationNudgeCount, 1);
  assert.equal(progressive.decide(1_000, 10_000).action, "nudge_verification");
});

test("progressive execution does not treat command execution as a workspace mutation", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
  });

  progressive.observeToolResult({
    toolCallId: "shell-1",
    toolName: "shell",
    arguments: { executable: "git", argv: ["status", "--short"] },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "batch-1",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: {},
  }, {
    tool_call_id: "shell-1",
    ok: true,
    summary: "command completed",
    output: {},
    artifacts: [],
    metadata: { physical_effect_executed: "true" },
  }, false);

  const observed = progressive.snapshot();
  assert.equal(observed.realActionCount, 1);
  assert.equal(observed.workspaceMutationCount, 0);
  assert.equal(observed.requiredDeliveryMissing, true);
  assert.equal(observed.preDeliveryObservationCount, 1);
  assert.equal(observed.consecutivePreDeliveryObservations, 1);
  assert.match(observed.progressReasons.join("\n"), /pre_delivery_non_delivery_action/);
  assert.doesNotMatch(observed.progressReasons.join("\n"), /workspace_mutation_committed/);
});

test("progressive execution counts each background job once and observes its terminal result", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
  });
  const request = (toolCallId: string, toolName: string): ToolExecutionRequest => ({
    toolCallId,
    toolName,
    arguments: {},
    turnIndex: 0,
    stepIndex: 0,
    batchId: "batch-background",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: {},
  });
  const response = (
    toolCallId: string,
    status: "running" | "completed",
  ): ToolExecutionResponse => ({
    tool_call_id: toolCallId,
    ok: true,
    summary: status,
    output: { status },
    artifacts: [],
    metadata: {},
  });

  assert.equal(progressive.backgroundShellSlotsRemaining(), 2);
  progressive.observeToolResult(
    request("shell-start", "shell"),
    response("shell-start", "running"),
    false,
  );
  assert.equal(progressive.backgroundShellSlotsRemaining(), 1);
  progressive.observeToolResult(
    request("shell-start-second", "shell"),
    response("shell-start-second", "running"),
    false,
  );
  assert.equal(progressive.backgroundShellSlotsRemaining(), 0);
  progressive.observeToolResult(
    request("shell-poll", "shell_wait"),
    response("shell-poll", "running"),
    true,
  );
  assert.equal(progressive.snapshot().activeBackgroundCount, 2);
  assert.equal(progressive.backgroundShellSlotsRemaining(), 0);
  assert.equal(progressive.snapshot().preDeliveryObservationCount, 0);

  progressive.observeToolResult(
    request("shell-complete", "shell_wait"),
    response("shell-complete", "completed"),
    true,
  );
  assert.equal(progressive.snapshot().activeBackgroundCount, 1);
  assert.equal(progressive.backgroundShellSlotsRemaining(), 1);
  assert.equal(progressive.snapshot().preDeliveryObservationCount, 1);
  progressive.observeToolResult(
    request("shell-complete-second", "shell_wait"),
    response("shell-complete-second", "completed"),
    true,
  );
  assert.equal(progressive.snapshot().activeBackgroundCount, 0);
  assert.equal(progressive.backgroundShellSlotsRemaining(), 2);
});

test("background verification launch cannot clear an earlier failed semantic scope", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 2,
      repairMutationCount: 2,
      unresolvedVerificationScopes: ["shell:afctl:test:integration"],
      unresolvedVerificationFailures: [{
        scope: "shell:afctl:test:integration",
        failedChecks: ["opaque-state"],
        failedCount: 1,
        failureKind: "reported_checks",
        attemptCount: 2,
        lastObservedWorkspaceMutationCount: 2,
      }],
      targetedRepairReserveVersion: 4,
    },
  });
  progressive.observeToolResult({
    toolCallId: "integration-background-start",
    toolName: "shell",
    arguments: { command: "python tools/afctl.py test integration" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "integration-background-start",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: {
      progressive_verification_driving: true,
      progressive_verification_scope: "shell:afctl:test:integration",
    },
  }, {
    tool_call_id: "integration-background-start",
    ok: true,
    summary: "Sandbox command is still running in the background.",
    output: { status: "running", job_id: "job-integration" },
    artifacts: [],
    metadata: { background_status: "running" },
  }, false);

  const snapshot = progressive.snapshot();
  assert.equal(snapshot.verificationCount, 0);
  assert.deepEqual(snapshot.unresolvedVerificationScopes, ["shell:afctl:test:integration"]);
  assert.equal(snapshot.unresolvedVerificationFailures[0].attemptCount, 2);
  assert.doesNotMatch(snapshot.progressReasons.join("\n"), /post_delivery_verification_passed/);
});

test("runtime stops admitting shell commands until active background jobs are reconciled", async () => {
  class BackgroundHost extends MemoryHost {
    readonly shellRequests: ToolExecutionRequest[] = [];

    override async executeBatch(
      batch: ToolBatch,
      requests: ToolExecutionRequest[],
    ): Promise<ToolExecutionResponse[]> {
      this.batches.push(batch);
      this.shellRequests.push(...requests);
      return requests.map((request) => ({
        tool_call_id: request.toolCallId,
        ok: true,
        summary: "Sandbox command is still running in the background.",
        output: { status: "running", job_id: `job-${request.toolCallId}` },
        artifacts: [],
        metadata: { background_status: "running" },
      }));
    }
  }

  const host = new BackgroundHost();
  await new ClaudeRuntimeCore().run(input({
    turns: [
      [{ tool_name: "shell", arguments: { command: "echo first" } }],
      [{ tool_name: "shell", arguments: { command: "echo second" } }],
      [{ tool_name: "shell", arguments: { command: "echo third" } }],
    ],
    tools: [{
      name: "shell",
      purpose: "shell",
      source: "test",
      input_schema: {
        type: "object",
        required: ["command"],
        properties: { command: { type: "string" } },
      },
      output_schema: {},
      metadata: { read_only: "false", concurrency_safe: "false" },
    }],
  }), host);

  assert.equal(host.shellRequests.length, 2);
  assert.deepEqual(
    host.shellRequests.map((request) => request.arguments.command),
    ["echo first", "echo second"],
  );
  assert.ok(host.checkpoints.some((checkpoint) =>
    JSON.stringify(checkpoint).includes("active_background_shell_limit_reached")
  ));
});

test("progressive execution does not treat read-only diagnostic artifacts as delivery", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
  });

  progressive.observeToolResult({
    toolCallId: "browser-state",
    toolName: "browser",
    arguments: { action: "open_url", url: "http://127.0.0.1/events" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "browser-observation",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "concurrent_read_only",
    metadata: {},
  }, {
    tool_call_id: "browser-state",
    ok: true,
    summary: "captured browser state",
    output: {},
    artifacts: [
      { artifact_id: "html", kind: "file", uri: "artifact:state.html", title: "browser raw HTML" },
      { artifact_id: "json", kind: "structured_data", uri: "artifact:state.json", title: "browser state snapshot" },
    ],
    metadata: {},
  }, true);

  const observed = progressive.snapshot();
  assert.equal(observed.artifactCount, 2);
  assert.equal(observed.workspaceMutationCount, 0);
  assert.equal(observed.requiredDeliveryMissing, true);
  assert.equal(observed.consecutiveNoDeliveryObservations, 1);
  assert.doesNotMatch(observed.progressReasons.join("\n"), /artifact_receipt_committed/);
});

test("progressive execution nudges after sustained pre-delivery inspection", () => {
  let clock = 1_000;
  const progressive = new ProgressiveExecutionRuntime({
    now: () => clock,
    constraints: { pre_delivery_observation_nudge_after: 3 },
    deliveryContract: { workspace_mutation_required: true },
  });
  const readResult = (index: number) => {
    progressive.observeProviderRound(`Inspect source area ${index}.`, 1);
    progressive.observeToolResult({
      toolCallId: `read-${index}`,
      toolName: "read",
      arguments: { path: `source-${index}.ts` },
      turnIndex: index,
      stepIndex: 0,
      batchId: `batch-${index}`,
      batchIndex: 0,
      batchSize: 1,
      executionMode: "concurrent_read_only",
      metadata: {},
    }, {
      tool_call_id: `read-${index}`,
      ok: true,
      summary: "source inspected",
      output: {},
      artifacts: [],
      metadata: {},
    }, true);
    clock += 1_000;
  };

  readResult(1);
  readResult(2);
  assert.equal(progressive.decide(1_000, 10_000).action, "continue");
  readResult(3);
  const stalled = progressive.decide(1_000, 10_000);

  assert.equal(stalled.action, "nudge_action");
  assert.match(stalled.reason, /read-only inspection/);
  assert.equal(stalled.snapshot.preDeliveryObservationCount, 3);
  assert.equal(stalled.snapshot.consecutivePreDeliveryObservations, 3);
  assert.equal(stalled.snapshot.verificationCount, 0);
  assert.equal(stalled.snapshot.requiredDeliveryMissing, true);

  const nudged = progressive.recordActionNudge();
  assert.equal(nudged.actionNudgeCount, 1);
  assert.equal(nudged.lastActionNudgeObservationCount, 3);
  assert.equal(progressive.decide(1_000, 10_000).action, "continue");
  readResult(4);
  readResult(5);
  assert.equal(progressive.decide(1_000, 10_000).action, "continue");
  readResult(6);
  assert.equal(progressive.decide(1_000, 10_000).action, "nudge_action");
});

test("progressive action nudge cadence survives checkpoint restore", () => {
  const progressive = new ProgressiveExecutionRuntime({
    constraints: { pre_delivery_observation_nudge_after: 3 },
    deliveryContract: { workspace_mutation_required: true },
    restored: {
      version: "zyra.progressive-execution/v1",
      providerRounds: 8,
      preDeliveryObservationCount: 5,
      consecutivePreDeliveryObservations: 5,
      actionNudgeCount: 1,
      lastActionNudgeObservationCount: 3,
      lastActionNudgeProviderRound: 6,
      workspaceMutationCount: 0,
      artifactCount: 0,
    },
  });

  assert.equal(progressive.decide(1_000, 10_000).action, "continue");
  progressive.observeToolResult({
    toolCallId: "read-restored",
    toolName: "read",
    arguments: { path: "restored.ts" },
    turnIndex: 9,
    stepIndex: 0,
    batchId: "batch-restored",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "concurrent_read_only",
    metadata: {},
  }, {
    tool_call_id: "read-restored",
    ok: true,
    summary: "source inspected",
    output: {},
    artifacts: [],
    metadata: {},
  }, true);
  assert.equal(progressive.decide(1_000, 10_000).action, "nudge_action");
});

test("progressive execution nudges sustained inspection after a restored delivery", () => {
  const progressive = new ProgressiveExecutionRuntime({
    constraints: { pre_delivery_observation_nudge_after: 3 },
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      providerRounds: 40,
      realActionCount: 20,
      workspaceMutationCount: 8,
      verificationCount: 8,
      artifactCount: 2,
    },
  });
  const inspect = (index: number) => {
    progressive.observeToolResult({
      toolCallId: `post-delivery-read-${index}`,
      toolName: "read",
      arguments: { path: `source-${index}.ts` },
      turnIndex: index,
      stepIndex: 0,
      batchId: `post-delivery-batch-${index}`,
      batchIndex: 0,
      batchSize: 1,
      executionMode: "concurrent_read_only",
      metadata: {},
    }, {
      tool_call_id: `post-delivery-read-${index}`,
      ok: true,
      summary: "source inspected",
      output: {},
      artifacts: [],
      metadata: {},
    }, true);
  };

  inspect(1);
  inspect(2);
  assert.equal(progressive.decide(1_000, 10_000).action, "continue");
  inspect(3);
  const stalled = progressive.decide(1_000, 10_000);

  assert.equal(stalled.action, "nudge_action");
  assert.match(stalled.reason, /last durable delivery/);
  assert.equal(stalled.snapshot.requiredDeliveryMissing, false);
  assert.equal(stalled.snapshot.workspaceMutationCount, 8);
  assert.equal(stalled.snapshot.preDeliveryObservationCount, 0);
  assert.equal(stalled.snapshot.noDeliveryObservationCount, 3);
  assert.equal(stalled.snapshot.consecutiveNoDeliveryObservations, 3);

  progressive.recordActionNudge();
  assert.equal(progressive.decide(1_000, 10_000).action, "continue");
  inspect(4);
  inspect(5);
  inspect(6);
  assert.equal(progressive.decide(1_000, 10_000).action, "nudge_action");
});

test("progressive execution resets post-delivery inspection streak on new progress", () => {
  const progressive = new ProgressiveExecutionRuntime({
    constraints: { pre_delivery_observation_nudge_after: 3 },
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 1,
      verificationCount: 1,
    },
  });
  const request = (toolCallId: string, metadata: JsonObject = {}): ToolExecutionRequest => ({
    toolCallId,
    toolName: "shell",
    arguments: { command: "command" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "post-delivery-progress",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata,
  });
  const response = (toolCallId: string, mutated = false): ToolExecutionResponse => ({
    tool_call_id: toolCallId,
    ok: true,
    summary: "command completed",
    output: {},
    artifacts: [],
    metadata: { workspace_mutation_committed: String(mutated) },
  });

  progressive.observeToolResult(request("inspect-1"), response("inspect-1"), true);
  progressive.observeToolResult(request("inspect-2"), response("inspect-2"), true);
  progressive.observeToolResult(request("edit"), response("edit", true), false);
  assert.equal(progressive.snapshot().consecutiveNoDeliveryObservations, 0);

  progressive.observeToolResult(request("inspect-3"), response("inspect-3"), true);
  progressive.observeToolResult(
    request("tests", { progressive_verification_driving: true }),
    response("tests"),
    false,
  );
  assert.equal(progressive.snapshot().consecutiveNoDeliveryObservations, 0);
  assert.equal(progressive.decide(1_000, 10_000).action, "continue");
});

test("progressive execution does not accept a zero-exit failed verification report", () => {
  const progressive = new ProgressiveExecutionRuntime({
    constraints: { pre_delivery_observation_nudge_after: 1 },
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 1,
      verificationCount: 1,
      verificationNudgeCount: 3,
    },
  });
  const request: ToolExecutionRequest = {
    toolCallId: "opaque-validation",
    toolName: "shell",
    arguments: { command: "./run-integration.sh" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "opaque-validation",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: { progressive_verification_driving: true },
  };
  progressive.recordActionNudge();
  progressive.observeToolResult(request, {
    tool_call_id: request.toolCallId,
    ok: true,
    summary: "command completed",
    output: {
      stdout: JSON.stringify({
        status: "failed",
        counts: { passed: 8, failed: 2 },
        failed_shards: ["security", "cross-language"],
      }),
      return_code: 0,
    },
    artifacts: [],
    metadata: { workspace_mutation_committed: "false" },
  }, false);

  const failed = progressive.snapshot();
  assert.equal(failed.verificationCount, 0);
  assert.equal(failed.verificationNudgeCount, 0);
  assert.equal(failed.recoveryInspectionAllowance, 8);
  assert.equal(failed.postDeliveryActionNudgeCount, 1);
  assert.ok(failed.progressReasons.includes("post_delivery_verification_failed"));
  assert.equal(progressive.decide(1_000, 10_000).action, "nudge_verification");

  progressive.observeToolResult({ ...request, toolCallId: "passing-tests" }, {
    tool_call_id: "passing-tests",
    ok: true,
    summary: "command completed",
    output: { stdout: "138 passed in 4.56s", return_code: 0 },
    artifacts: [],
    metadata: { workspace_mutation_committed: "false" },
  }, false);
  assert.equal(progressive.snapshot().verificationCount, 1);
  assert.equal(progressive.snapshot().postDeliveryActionNudgeCount, 0);
});

test("a passing verification cannot erase debt from a different failed scope", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 1,
    },
  });
  const scopedRequest = (toolCallId: string, scope: string): ToolExecutionRequest => ({
    toolCallId,
    toolName: "shell",
    arguments: { command: scope },
    turnIndex: 0,
    stepIndex: 0,
    batchId: toolCallId,
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: {
      progressive_verification_driving: true,
      progressive_verification_scope: scope,
    },
  });
  const observe = (request: ToolExecutionRequest, stdout: string, ok = true) => {
    progressive.observeToolResult(request, {
      tool_call_id: request.toolCallId,
      ok,
      summary: "command completed",
      output: { stdout, return_code: 0 },
      artifacts: [],
      metadata: { workspace_mutation_committed: "false" },
    }, false);
  };

  observe(
    scopedRequest("integration-failed", "integration-suite"),
    '{"status":"failed","failed_shards":["opaque-a"]}',
  );
  assert.deepEqual(progressive.snapshot().unresolvedVerificationScopes, ["integration-suite"]);
  assert.deepEqual(progressive.snapshot().unresolvedVerificationFailures, [{
    scope: "integration-suite",
    failedChecks: ["opaque-a"],
    failedCount: null,
    failureKind: "reported_checks",
    attemptCount: 1,
    lastObservedWorkspaceMutationCount: 1,
  }]);

  observe(scopedRequest("public-passed", "public-suite"), "140 passed in 12.0s");
  const stillFailed = progressive.snapshot();
  assert.equal(stillFailed.verificationCount, 0);
  assert.deepEqual(stillFailed.unresolvedVerificationScopes, ["integration-suite"]);
  assert.equal(progressive.decide(1_000, 10_000).action, "nudge_verification");

  observe(scopedRequest("integration-passed", "integration-suite"), "10 passed in 20.0s");
  const settled = progressive.snapshot();
  assert.equal(settled.verificationCount, 1);
  assert.deepEqual(settled.unresolvedVerificationScopes, []);
  assert.deepEqual(settled.unresolvedVerificationFailures, []);

  observe(
    scopedRequest("integration-physical-failure", "integration-suite"),
    "2 failed in 3.0s",
    false,
  );
  assert.equal(progressive.snapshot().verificationCount, 0);
  assert.deepEqual(progressive.snapshot().unresolvedVerificationScopes, ["integration-suite"]);
});

test("repeating the same failed verification without an edit does not refill diagnostics", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 1,
    },
  });
  const verification: ToolExecutionRequest = {
    toolCallId: "integration-failed",
    toolName: "shell",
    arguments: { command: "python tools/afctl.py test integration" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "integration-failed",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: {
      progressive_verification_driving: true,
      progressive_verification_scope: "shell:afctl:test:integration",
    },
  };
  const failedResponse: ToolExecutionResponse = {
    tool_call_id: verification.toolCallId,
    ok: true,
    summary: "command completed",
    output: {
      stdout: '{"status":"failed","counts":{"passed":7,"failed":3},"failed_shards":["security","state","cross-language"]}',
      return_code: 0,
    },
    artifacts: [],
    metadata: { workspace_mutation_committed: "false" },
  };

  progressive.recordActionNudge();
  progressive.recordActionNudge();
  progressive.observeToolResult(verification, failedResponse, false);
  assert.equal(progressive.snapshot().recoveryInspectionAllowance, 8);
  for (let index = 0; index < 8; index += 1) {
    assert.equal(progressive.consumeRecoveryInspectionAllowance(), true);
  }
  progressive.observeToolResult(
    { ...verification, toolCallId: "integration-same-bytes" },
    { ...failedResponse, tool_call_id: "integration-same-bytes" },
    false,
  );
  assert.equal(progressive.snapshot().recoveryInspectionAllowance, 0);
  assert.equal(progressive.snapshot().unresolvedVerificationFailures[0].attemptCount, 2);

  progressive.observeToolResult({
    ...verification,
    toolCallId: "targeted-edit",
    toolName: "write",
    arguments: { path: "src/fix.ts", content: "fixed" },
    metadata: {},
  }, {
    tool_call_id: "targeted-edit",
    ok: true,
    summary: "updated",
    output: {},
    artifacts: [],
    metadata: { workspace_mutation_committed: "true" },
  }, false);
  progressive.observeToolResult(
    { ...verification, toolCallId: "integration-after-edit" },
    { ...failedResponse, tool_call_id: "integration-after-edit" },
    false,
  );
  const afterEdit = progressive.snapshot();
  assert.equal(afterEdit.recoveryInspectionAllowance, 8);
  assert.equal(afterEdit.unresolvedVerificationFailures[0].attemptCount, 3);
  assert.equal(afterEdit.unresolvedVerificationFailures[0].lastObservedWorkspaceMutationCount, 2);
});

test("failed verification meters diagnostic inspection immediately", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 1,
    },
  });
  const verification: ToolExecutionRequest = {
    toolCallId: "integration-failed-before-nudge",
    toolName: "shell",
    arguments: { command: "python tools/afctl.py test integration" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "integration-failed-before-nudge",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: {
      progressive_verification_driving: true,
      progressive_verification_scope: "shell:afctl:test:integration",
    },
  };

  progressive.observeToolResult(verification, {
    tool_call_id: verification.toolCallId,
    ok: true,
    summary: "command completed",
    output: {
      stdout: '{"status":"failed","failed_shards":["opaque-state"]}',
      return_code: 0,
    },
    artifacts: [],
    metadata: { workspace_mutation_committed: "false" },
  }, false);

  assert.equal(progressive.snapshot().actionNudgeCount, 0);
  assert.equal(progressive.inspectionCircuitOpen(), true);
  assert.equal(progressive.snapshot().recoveryInspectionAllowance, 8);
  for (let index = 0; index < 8; index += 1) {
    assert.equal(progressive.consumeRecoveryInspectionAllowance(), true);
  }
  assert.equal(progressive.consumeRecoveryInspectionAllowance(), false);
});

test("failed verification preserves six named source reads after broad diagnostics", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 1,
    },
  });
  const verification: ToolExecutionRequest = {
    toolCallId: "integration-failed-targeted-reserve",
    toolName: "shell",
    arguments: { command: "python tools/afctl.py test integration" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "integration-failed-targeted-reserve",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: {
      progressive_verification_driving: true,
      progressive_verification_scope: "shell:afctl:test:integration",
    },
  };
  progressive.observeToolResult(verification, {
    tool_call_id: verification.toolCallId,
    ok: true,
    summary: "command completed",
    output: {
      stdout: '{"status":"failed","failed_shards":["opaque-state"]}',
      return_code: 0,
    },
    artifacts: [],
    metadata: { workspace_mutation_committed: "false" },
  }, false);

  for (let index = 0; index < 8; index += 1) {
    assert.equal(progressive.consumeRecoveryInspectionAllowance(), true);
  }
  assert.equal(progressive.consumeRecoveryInspectionAllowance(), false);
  for (let index = 0; index < 6; index += 1) {
    assert.equal(progressive.consumeRecoveryInspectionAllowance(true), true);
  }
  assert.equal(progressive.consumeRecoveryInspectionAllowance(true), false);
});

test("verification-generated files do not impersonate a repair mutation", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 1,
    },
  });
  const verification = (toolCallId: string): ToolExecutionRequest => ({
    toolCallId,
    toolName: "shell",
    arguments: { command: "python tools/afctl.py simulate" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: toolCallId,
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: {
      progressive_verification_driving: true,
      progressive_verification_scope: "shell:afctl:simulate",
    },
  });
  const failedResponse = (toolCallId: string, mutated = false): ToolExecutionResponse => ({
    tool_call_id: toolCallId,
    ok: true,
    summary: "simulation failed",
    output: { stdout: '{"status":"failed","failed_shards":["state"]}', return_code: 0 },
    artifacts: [],
    metadata: { workspace_mutation_committed: String(mutated) },
  });

  progressive.observeToolResult(verification("first-failure"), failedResponse("first-failure"), false);
  for (let index = 0; index < 8; index += 1) {
    assert.equal(progressive.consumeRecoveryInspectionAllowance(), true);
  }
  for (let index = 0; index < 6; index += 1) {
    assert.equal(progressive.consumeRecoveryInspectionAllowance(true), true);
  }

  progressive.observeToolResult(
    verification("same-code-generated-report"),
    failedResponse("same-code-generated-report", true),
    false,
  );
  const snapshot = progressive.snapshot();
  assert.equal(snapshot.workspaceMutationCount, 2);
  assert.equal(snapshot.repairMutationCount, 1);
  assert.equal(snapshot.recoveryInspectionAllowance, 0);
  assert.equal(snapshot.targetedRepairInspectionAllowance, 0);
  assert.equal(snapshot.unresolvedVerificationFailures[0].lastObservedWorkspaceMutationCount, 1);
});

test("submission report updates do not impersonate a repair mutation", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 9,
      repairMutationCount: 4,
    },
  });
  progressive.observeToolResult({
    toolCallId: "update-report",
    toolName: "shell",
    arguments: { command: "python update_report.py submission/test-report.json" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "update-report",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: { progressive_repair_driving: false },
  }, {
    tool_call_id: "update-report",
    ok: true,
    summary: "report updated",
    output: {},
    artifacts: [],
    metadata: { workspace_mutation_committed: "true" },
  }, false);

  const snapshot = progressive.snapshot();
  assert.equal(snapshot.workspaceMutationCount, 10);
  assert.equal(snapshot.repairMutationCount, 4);
});

test("generated reports preserve the remaining named-source repair reserve", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 9,
      repairMutationCount: 4,
      unresolvedVerificationScopes: ["shell:afctl:test:integration"],
      unresolvedVerificationFailures: [{
        scope: "shell:afctl:test:integration",
        failedChecks: ["opaque-state"],
        failedCount: 1,
        failureKind: "reported_checks",
        attemptCount: 2,
        lastObservedWorkspaceMutationCount: 4,
      }],
      targetedRepairInspectionAllowance: 2,
      targetedRepairReserveVersion: 4,
    },
  });
  progressive.observeToolResult({
    toolCallId: "update-recovery-report",
    toolName: "shell",
    arguments: { command: "python update_report.py submission/recovery-report.json" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "update-recovery-report",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: { progressive_repair_driving: false },
  }, {
    tool_call_id: "update-recovery-report",
    ok: true,
    summary: "report updated",
    output: {},
    artifacts: [],
    metadata: { workspace_mutation_committed: "true" },
  }, false);

  const snapshot = progressive.snapshot();
  assert.equal(snapshot.workspaceMutationCount, 10);
  assert.equal(snapshot.repairMutationCount, 4);
  assert.equal(snapshot.recoveryInspectionAllowance, 0);
  assert.equal(snapshot.targetedRepairInspectionAllowance, 2);
  assert.equal(progressive.consumeRecoveryInspectionAllowance(true), true);
});

test("legacy verification debt receives the named-source reserve only once", () => {
  const legacyContinuity: JsonObject = {
    requiredDeliveryMissing: false,
    workspaceMutationCount: 7,
    unresolvedVerificationScopes: ["shell:afctl:test:integration"],
    unresolvedVerificationFailures: [{
      scope: "shell:afctl:test:integration",
      failedChecks: ["opaque-state"],
      failedCount: 1,
      failureKind: "reported_checks",
      attemptCount: 1,
      lastObservedWorkspaceMutationCount: 7,
    }],
  };
  const migrated = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: legacyContinuity,
  }).snapshot();
  assert.equal(migrated.targetedRepairInspectionAllowance, 6);
  assert.equal(migrated.targetedRepairReserveVersion, 4);

  const exhausted = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      ...legacyContinuity,
      targetedRepairInspectionAllowance: 0,
      targetedRepairReserveVersion: 4,
    },
  }).snapshot();
  assert.equal(exhausted.targetedRepairInspectionAllowance, 0);
});

test("unscoped failed continuation cannot refill existing verification diagnostics", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 5,
    },
  });
  const failed = (toolCallId: string, scope = ""): ToolExecutionRequest => ({
    toolCallId,
    toolName: scope ? "shell" : "shell_wait",
    arguments: scope
      ? { command: "python tools/afctl.py test integration" }
      : { job_id: "restored-job" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: toolCallId,
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: {
      progressive_verification_driving: true,
      progressive_verification_scope: scope,
    },
  });
  const response = (toolCallId: string): ToolExecutionResponse => ({
    tool_call_id: toolCallId,
    ok: true,
    summary: "command completed",
    output: {
      stdout: '{"status":"failed","failed_shards":["opaque-state"]}',
      return_code: 0,
    },
    artifacts: [],
    metadata: { workspace_mutation_committed: "false" },
  });

  const scoped = failed("scoped-failure", "shell:afctl:test:integration");
  progressive.observeToolResult(scoped, response(scoped.toolCallId), false);
  for (let index = 0; index < 8; index += 1) {
    assert.equal(progressive.consumeRecoveryInspectionAllowance(), true);
  }

  const unscoped = failed("unscoped-restored-failure");
  progressive.observeToolResult(unscoped, response(unscoped.toolCallId), false);
  const snapshot = progressive.snapshot();
  assert.equal(snapshot.recoveryInspectionAllowance, 0);
  assert.deepEqual(snapshot.unresolvedVerificationScopes, ["shell:afctl:test:integration"]);
  assert.equal(snapshot.unresolvedVerificationFailures[0].attemptCount, 2);
  assert.equal(snapshot.unresolvedVerificationFailures[0].lastObservedWorkspaceMutationCount, 5);
});

test("structured verification debt survives a fenced execution continuation", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 7,
      verificationCount: 4,
      unresolvedVerificationScopes: ["shell:afctl:test:integration"],
      unresolvedVerificationFailures: [{
        scope: "shell:afctl:test:integration",
        failedChecks: ["security", "cross-language"],
        failedCount: 2,
        failureKind: "reported_checks",
        attemptCount: 3,
        lastObservedWorkspaceMutationCount: 7,
      }],
    },
  });

  const restored = progressive.snapshot();
  assert.equal(restored.verificationCount, 0);
  assert.deepEqual(restored.unresolvedVerificationScopes, ["shell:afctl:test:integration"]);
  assert.deepEqual(restored.unresolvedVerificationFailures[0].failedChecks, [
    "security",
    "cross-language",
  ]);
  const decision = progressive.decide(1_000, 10_000);
  assert.equal(decision.action, "nudge_verification");
  assert.match(decision.reason, /security,cross-language/);
});

test("progressive execution rejects a zero-exit verification traceback", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 2,
      verificationCount: 1,
    },
  });
  const request: ToolExecutionRequest = {
    toolCallId: "wrapped-worker-check",
    toolName: "shell",
    arguments: { command: "python verify_worker.py || true" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "wrapped-worker-check",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: { progressive_verification_driving: true },
  };

  progressive.observeToolResult(request, {
    tool_call_id: request.toolCallId,
    ok: true,
    summary: "command completed",
    output: {
      stdout: [
        "Traceback (most recent call last):",
        "  File 'verify_worker.py', line 10, in <module>",
        "urllib.error.URLError: <urlopen error [Errno 111] Connection refused>",
        "RC=0",
      ].join("\n"),
      return_code: 0,
    },
    artifacts: [],
    metadata: { workspace_mutation_committed: "false" },
  }, false);

  const failed = progressive.snapshot();
  assert.equal(failed.verificationCount, 0);
  assert.ok(failed.progressReasons.includes("post_delivery_verification_failed"));
});

test("progressive execution rejects an explicit command error hidden by zero exit", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 2,
      verificationCount: 1,
    },
  });
  const request: ToolExecutionRequest = {
    toolCallId: "wrapped-database-check",
    toolName: "shell",
    arguments: { command: "database-client --query verify; echo RC=$?" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "wrapped-database-check",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: { progressive_verification_driving: true },
  };

  progressive.observeToolResult(request, {
    tool_call_id: request.toolCallId,
    ok: true,
    summary: "command completed",
    output: {
      stdout: [
        "ERROR:  column released_at does not exist",
        "LINE 1: SELECT count(*) FROM worker_leases WHERE released_at IS NULL",
        "RC=0",
      ].join("\n"),
      return_code: 0,
    },
    artifacts: [],
    metadata: { workspace_mutation_committed: "false" },
  }, false);

  const failed = progressive.snapshot();
  assert.equal(failed.verificationCount, 0);
  assert.ok(failed.progressReasons.includes("post_delivery_verification_failed"));
});

test("progressive execution rejects a zero-exit transport failure", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 2,
      verificationCount: 1,
    },
  });
  const request: ToolExecutionRequest = {
    toolCallId: "device-simulation",
    toolName: "shell",
    arguments: { command: "python scripts/run_device_simulation.py" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "device-simulation",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: { progressive_verification_driving: true },
  };

  progressive.observeToolResult(request, {
    tool_call_id: request.toolCallId,
    ok: true,
    summary: "command completed",
    output: {
      stdout: "request failed: <urlopen error [Errno 101] Network unreachable>",
      return_code: 0,
    },
    artifacts: [],
    metadata: { workspace_mutation_committed: "false" },
  }, false);

  const failed = progressive.snapshot();
  assert.equal(failed.verificationCount, 0);
  assert.ok(failed.progressReasons.includes("post_delivery_verification_failed"));
});

test("progressive execution rejects a compound verification that crashes after tests pass", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 2,
      verificationCount: 1,
    },
  });
  const request: ToolExecutionRequest = {
    toolCallId: "compound-check",
    toolName: "shell",
    arguments: { command: "pytest && python verify_wiring.py" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "compound-check",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: { progressive_verification_driving: true },
  };

  progressive.observeToolResult(request, {
    tool_call_id: request.toolCallId,
    ok: true,
    summary: "command completed",
    output: {
      stdout: [
        "138 passed in 12.45s",
        "Traceback (most recent call last):",
        "  File '<stdin>', line 3, in <module>",
        "ModuleNotFoundError: No module named 'psycopg'",
      ].join("\n"),
      return_code: 0,
    },
    artifacts: [],
    metadata: { workspace_mutation_committed: "false" },
  }, false);

  assert.equal(progressive.snapshot().verificationCount, 0);

  progressive.observeToolResult({ ...request, toolCallId: "expected-error-tests" }, {
    tool_call_id: "expected-error-tests",
    ok: true,
    summary: "command completed",
    output: {
      stdout: [
        "Traceback (most recent call last):",
        "ValueError: expected fixture error",
        "5 passed in 0.10s",
      ].join("\n"),
      return_code: 0,
    },
    artifacts: [],
    metadata: { workspace_mutation_committed: "false" },
  }, false);
  assert.equal(progressive.snapshot().verificationCount, 1);
});

test("repeated successful verification does not reopen stalled inspection", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 1,
      verificationCount: 1,
      consecutiveNoDeliveryObservations: 8,
      postDeliveryActionNudgeCount: 2,
    },
  });
  const request: ToolExecutionRequest = {
    toolCallId: "repeated-typecheck",
    toolName: "shell",
    arguments: { command: "npm run typecheck" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "repeated-typecheck",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: { progressive_verification_driving: true },
  };
  progressive.observeToolResult(request, {
    tool_call_id: request.toolCallId,
    ok: true,
    summary: "command completed",
    output: { stdout: "typecheck passed", return_code: 0 },
    artifacts: [],
    metadata: { workspace_mutation_committed: "false" },
  }, false);

  const repeated = progressive.snapshot();
  assert.equal(repeated.verificationCount, 1);
  assert.equal(repeated.postDeliveryActionNudgeCount, 2);
  assert.equal(repeated.consecutiveNoDeliveryObservations, 9);
  assert.ok(repeated.progressReasons.includes("post_delivery_verification_repeated_without_delivery"));
  assert.equal(progressive.inspectionCircuitOpen(), true);
});

test("pre-delivery inspection circuit opens after repeated durable nudges", () => {
  const progressive = new ProgressiveExecutionRuntime({
    constraints: { pre_delivery_inspection_block_after_nudges: 3 },
    deliveryContract: { workspace_mutation_required: true },
  });
  assert.equal(progressive.inspectionCircuitOpen(), false);
  progressive.recordActionNudge();
  progressive.recordActionNudge();
  assert.equal(progressive.inspectionCircuitOpen(), false);
  progressive.recordActionNudge();
  assert.equal(progressive.inspectionCircuitOpen(), true);
});

test("post-delivery inspection circuit uses a tighter independent default", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 1,
      verificationCount: 1,
    },
  });
  progressive.recordActionNudge();
  assert.equal(progressive.inspectionCircuitOpen(), false);
  progressive.recordActionNudge();
  assert.equal(progressive.inspectionCircuitOpen(), true);

  const configured = new ProgressiveExecutionRuntime({
    constraints: { post_delivery_inspection_block_after_nudges: 3 },
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      workspaceMutationCount: 1,
      verificationCount: 1,
    },
  });
  configured.recordActionNudge();
  configured.recordActionNudge();
  assert.equal(configured.inspectionCircuitOpen(), false);
  configured.recordActionNudge();
  assert.equal(configured.inspectionCircuitOpen(), true);
});

test("pre-delivery inspection circuit carries bounded debt across fenced sessions", () => {
  const carried = new ProgressiveExecutionRuntime({
    constraints: { pre_delivery_inspection_block_after_nudges: 3 },
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: true,
      providerRounds: 9,
      preDeliveryObservationCount: 6,
      consecutivePreDeliveryObservations: 6,
      actionNudgeCount: 4,
      lastActionNudgeObservationCount: 6,
      lastActionNudgeProviderRound: 7,
    },
  });
  assert.equal(carried.inspectionCircuitOpen(), true);
  assert.equal(carried.snapshot().providerRounds, 9);
  assert.equal(carried.snapshot().actionNudgeCount, 4);

  const delivered = new ProgressiveExecutionRuntime({
    constraints: { pre_delivery_inspection_block_after_nudges: 3 },
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      providerRounds: 9,
      actionNudgeCount: 4,
    },
  });
  assert.equal(delivered.inspectionCircuitOpen(), false);
  assert.equal(delivered.snapshot().actionNudgeCount, 0);
});

test("post-delivery inspection circuit carries stall debt and resets on progress", () => {
  const progressive = new ProgressiveExecutionRuntime({
    constraints: { pre_delivery_inspection_block_after_nudges: 3 },
    deliveryContract: { workspace_mutation_required: true },
    continuityProgress: {
      requiredDeliveryMissing: false,
      providerRounds: 14,
      workspaceMutationCount: 3,
      verificationCount: 1,
      noDeliveryObservationCount: 24,
      consecutiveNoDeliveryObservations: 24,
      actionNudgeCount: 5,
      postDeliveryActionNudgeCount: 3,
      lastActionNudgeNoDeliveryObservationCount: 24,
      lastActionNudgeProviderRound: 14,
    },
  });
  assert.equal(progressive.inspectionCircuitOpen(), true);

  progressive.observeToolResult(
    {
      toolCallId: "post-delivery-edit",
      toolName: "write",
      arguments: { path: "result.txt", content: "progress" },
      turnIndex: 0,
      stepIndex: 0,
      batchId: "post-delivery",
      batchIndex: 0,
      batchSize: 1,
      executionMode: "serial_non_read_only",
      metadata: {},
    },
    {
      tool_call_id: "post-delivery-edit",
      ok: true,
      summary: "updated",
      output: {},
      artifacts: [],
      metadata: { workspace_mutation_committed: "true" },
    },
    false,
  );
  assert.equal(progressive.snapshot().postDeliveryActionNudgeCount, 0);
  assert.equal(progressive.inspectionCircuitOpen(), false);
});

test("failed delivery grants exactly one bounded recovery inspection", () => {
  const progressive = new ProgressiveExecutionRuntime({
    constraints: { pre_delivery_inspection_block_after_nudges: 3 },
    deliveryContract: { workspace_mutation_required: true },
  });
  progressive.recordActionNudge();
  progressive.recordActionNudge();
  progressive.recordActionNudge();
  progressive.observeToolResult({
    toolCallId: "stale-edit",
    toolName: "file_edit",
    arguments: { path: "result.txt" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "stale-edit-batch",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: {},
  }, {
    tool_call_id: "stale-edit",
    ok: false,
    summary: "old text was not found",
    output: {},
    artifacts: [],
    error: "edit_old_text_not_found",
    metadata: {},
  }, false);

  assert.equal(progressive.snapshot().recoveryInspectionAllowance, 1);
  assert.equal(progressive.consumeRecoveryInspectionAllowance(), true);
  assert.equal(progressive.snapshot().recoveryInspectionAllowance, 0);
  assert.equal(progressive.consumeRecoveryInspectionAllowance(), false);
  assert.equal(progressive.inspectionCircuitOpen(), true);
});

test("pre-delivery verification opens a bounded diagnostic inspection window", () => {
  const progressive = new ProgressiveExecutionRuntime({
    constraints: {
      pre_delivery_inspection_block_after_nudges: 3,
      post_verification_diagnostic_inspection_limit: 5,
    },
    deliveryContract: { workspace_mutation_required: true },
  });
  progressive.recordActionNudge();
  progressive.recordActionNudge();
  progressive.recordActionNudge();
  progressive.observeToolResult({
    toolCallId: "public-tests",
    toolName: "shell",
    arguments: { command: "python -m pytest -q" },
    turnIndex: 0,
    stepIndex: 0,
    batchId: "public-test-batch",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: { progressive_verification_driving: true },
  }, {
    tool_call_id: "public-tests",
    ok: true,
    summary: "command completed with failure details in piped output",
    output: { stdout: "4 failed, 134 passed" },
    artifacts: [],
    metadata: { return_code: "0" },
  }, false);

  assert.equal(progressive.inspectionCircuitOpen(), true);
  assert.equal(progressive.snapshot().recoveryInspectionAllowance, 5);
  for (let index = 0; index < 5; index += 1) {
    assert.equal(progressive.consumeRecoveryInspectionAllowance(), true);
  }
  assert.equal(progressive.consumeRecoveryInspectionAllowance(), false);
});

test("pre-delivery inspection classifier blocks reads but permits delivery and verification", () => {
  const shell = (command: string) => ({ tool_name: "shell", arguments: { command } });
  const structuredShell = (executable: string, argv: string[]) => ({
    tool_name: "shell",
    arguments: { executable, argv },
  });
  assert.equal(isClearlyPreDeliveryInspection(shell("cat src/app.ts && git diff --stat"), false), true);
  assert.equal(isClearlyPreDeliveryInspection(shell("find src -type f | xargs grep -n TODO 2>/dev/null | head"), false), true);
  assert.equal(isClearlyPreDeliveryInspection(shell("docker compose ps && docker compose config --services"), false), true);
  assert.equal(isClearlyPreDeliveryInspection(shell("python -c \"from pathlib import Path; print(Path('src/app.ts').read_text())\""), false), true);
  assert.equal(isClearlyPreDeliveryInspection(shell("python - <<'PY'\nfrom pathlib import Path\nprint(Path('src/app.ts').read_text())\nPY"), false), true);
  assert.equal(isClearlyPreDeliveryInspection(shell("some-opaque-command --inspect src"), false), true);
  assert.equal(isClearlyPreDeliveryInspection(shell("cat > src/app.ts <<'EOF'\nchanged\nEOF"), false), false);
  assert.equal(isClearlyPreDeliveryInspection(shell("python -c \"from pathlib import Path; Path('src/app.ts').write_text('changed')\""), false), false);
  assert.equal(isClearlyPreDeliveryInspection(shell("python tools/afctl.py simulate"), false), false);
  assert.equal(isClearlyPreDeliveryInspection(shell("python -m pytest tests"), false), false);
  assert.equal(isClearlyPreDeliveryInspection(shell("docker compose up -d --build"), false), false);
  assert.equal(isClearlyPreDeliveryInspection(shell("curl https://service.invalid/status"), false), true);
  assert.equal(isClearlyPreDeliveryInspection(shell("curl -X POST https://service.invalid/runs -d '{}'"), false), false);
  assert.equal(isClearlyPreDeliveryInspection(structuredShell("python", ["-c", "from pathlib import Path; Path('src/app.ts').write_text('changed')"]), false), false);
  assert.equal(isClearlyPreDeliveryInspection(structuredShell("python", ["-c", "from pathlib import Path; print(Path('src/app.ts').read_text())"]), false), true);
  assert.equal(isClearlyPreDeliveryInspection(structuredShell("python", ["tools/afctl.py", "bootstrap"]), false), false);
  assert.equal(isClearlyPreDeliveryInspection({ tool_name: "read", arguments: { path: "src/app.ts" } }, true), true);
  assert.equal(isClearlyPreDeliveryInspection({ tool_name: "write", arguments: { path: "src/app.ts" } }, false), false);
  assert.equal(isTargetedRepairInspection({ tool_name: "file_read", arguments: { path: "services/worker/state_machine.py" } }, true), true);
  assert.equal(isTargetedRepairInspection({ tool_name: "file_read", arguments: { path: "services/worker/src" } }, true), false);
  assert.equal(isTargetedRepairInspection({ tool_name: "file_read", arguments: { path: "submission/test-report.json" } }, true), false);
  assert.equal(isTargetedRepairInspection({ tool_name: "file_read", arguments: { path: "evidence/test-farm/latest.json" } }, true), false);
  assert.equal(isTargetedRepairInspection({ tool_name: "read", arguments: { path: ".runtime/venv/lib/source.py" } }, true), false);
  assert.equal(isTargetedRepairInspection(shell("cat services/worker/state_machine.py"), false), false);
  assert.equal(isClearlyRepairDrivingTool({ tool_name: "file_write", arguments: { path: "services/worker/state.py" } }), true);
  assert.equal(isClearlyRepairDrivingTool({ tool_name: "file_write", arguments: { path: "submission/test-report.json" } }), false);
  assert.equal(isClearlyRepairDrivingTool(shell("python tools/afctl.py test integration")), false);
  assert.equal(isClearlyRepairDrivingTool(shell("python -c \"from pathlib import Path; Path('submission/test-report.json').write_text('x')\"")), false);
  assert.equal(isClearlyRepairDrivingTool(shell("cat result.json > evidence/test-farm/latest.json")), false);
  assert.equal(isClearlyRepairDrivingTool(shell("apply_patch <<'PATCH'\n*** Update File: services/worker/state.py\nPATCH")), true);
  assert.equal(isClearlyRepairDrivingTool(shell("sed -i 's/a/b/' services/worker/state.py")), true);
  assert.equal(isClearlyRepairDrivingTool(shell("docker compose up -d --build")), false);
  assert.equal(isClearlyRepairDrivingTool(shell("npm run build")), false);
  assert.equal(isGeneratedDeliveryMutation({ tool_name: "file_write", arguments: { path: "submission/test-report.json" } }), true);
  assert.equal(isGeneratedDeliveryMutation({ tool_name: "file_edit", arguments: { path: "evidence/test-farm/latest.json" } }), true);
  assert.equal(isGeneratedDeliveryMutation({ tool_name: "file_write", arguments: { path: "services/worker/state.py" } }), false);
  assert.equal(isGeneratedDeliveryMutation(shell("cat result.json > evidence/test-farm/latest.json")), true);
  assert.equal(isGeneratedDeliveryMutation(shell("cat result.json > submission/result.json && sed -i 's/a/b/' services/worker/state.py")), false);
  assert.equal(isGeneratedDeliveryInspection({ tool_name: "file_read", arguments: { path: "submission/test-report.json" } }, true), true);
  assert.equal(isGeneratedDeliveryInspection(shell("cat evidence/test-farm/latest.json"), false), true);
  assert.equal(isGeneratedDeliveryInspection(shell("cat .runtime/simulation-result.json && docker ps"), false), true);
  assert.equal(isGeneratedDeliveryInspection(shell("ls evidence/test-farm 2>/dev/null; ls .runtime/ 2>/dev/null"), false), true);
  assert.equal(isGeneratedDeliveryInspection(shell("cat tools/regenerate_submission.py"), false), true);
  assert.equal(isGeneratedDeliveryInspection(shell("cat task-contract.json"), false), false);
  assert.equal(isGeneratedDeliveryInspection({ tool_name: "file_read", arguments: { path: "services/worker/state.py" } }, true), false);
  assert.equal(isClearlyRepairDrivingTool(shell("ls evidence/test-farm 2>/dev/null; ls .runtime/ 2>/dev/null")), false);
  assert.equal(isClearlyVerificationDrivingTool(shell("python -m pytest tests -q")), true);
  assert.equal(isClearlyVerificationDrivingTool(shell("npm run typecheck && npm test")), true);
  assert.equal(isClearlyVerificationDrivingTool(shell("python tools/afctl.py simulate")), true);
  assert.equal(isClearlyVerificationDrivingTool(shell("python tools/afctl.py request-acceptance")), false);
  assert.equal(isClearlyVerificationDrivingTool(structuredShell(".runtime/venv/bin/python", ["-m", "pytest", "-q"])), true);
  assert.equal(isClearlyVerificationDrivingTool(structuredShell("python", ["tools/afctl.py", "simulate"])), true);
  assert.equal(isClearlyVerificationDrivingTool(structuredShell("python", ["-c", "import json; print(json.load(open('submission/manifest.json')))"])), false);
  assert.equal(isClearlyVerificationDrivingTool(shell("cat submission/manifest.json")), false);
  assert.equal(isClearlyVerificationDrivingTool(shell("git status --short && sha256sum submission/*")), false);
  assert.equal(isClearlyVerificationDrivingTool(shell("python -c \"import json; json.load(open('submission/manifest.json'))\"")), false);
  assert.equal(isClearlyVerificationDrivingTool(shell("cat tests/public/test_metrics.py")), false);
  assert.equal(isClearlyVerificationDrivingTool(shell("sed -n '1,200p' /workspace/tests/integration/test_release.py")), false);
  assert.equal(isClearlyVerificationDrivingTool(shell("grep -n 'pytest|failed_shards|test integration' tools/afctl.py")), false);
  assert.equal(isClearlyVerificationDrivingTool(shell("./scripts/verify-release.sh --all")), true);
  assert.equal(isClearlyVerificationDrivingTool(shell("/workspace/tools/integration-check.py --live")), true);
  assert.equal(isClearlyVerificationDrivingTool({ tool_name: "read", arguments: { path: "test.log" } }), false);
});

test("verification scopes remain stable across diagnostic wrappers", () => {
  const scope = (command: string) => verificationScopeForTool({
    tool_name: "shell",
    arguments: { command },
  });
  const integrationCommands = [
    "python tools/afctl.py test integration",
    "timeout 400 python tools/afctl.py test integration 2>&1 | tail -40",
    "cd /workspace && .runtime/venv/bin/python tools/afctl.py test integration > /tmp/integration-9.log; grep failed /tmp/integration-9.log",
    "timeout 240 python tools/afctl.py test integration; echo EXIT_CODE=0",
  ];
  assert.deepEqual(
    integrationCommands.map(scope),
    integrationCommands.map(() => "shell:afctl:test:integration"),
  );
  assert.equal(scope("python tools/afctl.py test public"), "shell:afctl:test:public");
  assert.equal(
    scope("cd /workspace && timeout 240 python tools/afctl.py test public; echo EXIT_CODE=0"),
    "shell:afctl:test:public",
  );
  assert.equal(scope("timeout 300 python tools/afctl.py build 2>&1 | tail -5"), "shell:afctl:build");
  assert.equal(scope("python tools/afctl.py simulate > /tmp/sim.log"), "shell:afctl:simulate");
  assert.equal(scope("npm run test -- --runInBand"), "shell:npm:test");
  assert.notEqual(scope("python tools/afctl.py test public"), scope(integrationCommands[0]));
});

test("query engine propagates background verification lineage to shell_wait", () => {
  const origin = {
    toolCallId: "call-integration",
    name: "shell",
    arguments: {
      command: "python tools/afctl.py test integration",
    },
  };
  const response: ToolExecutionResponse = {
    tool_call_id: "call-wait",
    ok: true,
    summary: "Sandbox command completed",
    output: {
      stdout: JSON.stringify({
        schema: "example.test-job-summary/v1",
        status: "failed",
        counts: { passed: 8, failed: 2 },
      }),
      return_code: 0,
      gateway_receipt: {
        invocation_ref: {
          tool_call_id: origin.toolCallId,
        },
      },
    },
    artifacts: [],
    metadata: {},
  };

  assert.equal(isVerificationDrivingToolResult({
    tool_name: "shell_wait",
    arguments: { job_id: "job-integration" },
  }, response, [origin]), true);
  assert.equal(
    verificationScopeForToolResult({
      tool_name: "shell_wait",
      arguments: { job_id: "job-integration" },
    }, response, [origin]),
    verificationScopeForTool({ tool_name: origin.name, arguments: origin.arguments }),
  );
  assert.notEqual(
    verificationScopeForTool({ tool_name: origin.name, arguments: origin.arguments }),
    verificationScopeForTool({
      tool_name: "shell",
      arguments: { command: "python tools/afctl.py test public" },
    }),
  );
  assert.equal(isVerificationDrivingToolResult({
    tool_name: "shell_wait",
    arguments: { job_id: "job-unrelated" },
  }, response, [{ ...origin, name: "read" }]), false);

  const physicalResponse: ToolExecutionResponse = {
    ...response,
    ok: false,
    error: "sandbox_command_failed",
    output: {
      stdout: "138 passed\n",
      stderr: "sh: syntax error: bad substitution\n",
      return_code: 2,
      gateway_receipt: {
        invocation: {
          tool_call_id: origin.toolCallId,
          causation_id: origin.toolCallId,
        },
      },
    },
    metadata: {
      originating_tool_call_id: origin.toolCallId,
      return_code: "2",
    },
  };
  assert.equal(isVerificationDrivingToolResult({
    tool_name: "shell_wait",
    arguments: { job_id: "gateway-command-job:integration" },
  }, physicalResponse, [origin]), true);
});

test("progressive execution reapplies the current delivery contract after restore", () => {
  const progressive = new ProgressiveExecutionRuntime({
    deliveryContract: { workspace_mutation_required: true },
    restored: {
      version: "zyra.progressive-execution/v1",
      requiredDeliveryMissing: false,
      workspaceMutationCount: 0,
      artifactCount: 2,
    },
  });

  assert.equal(progressive.snapshot().requiredDeliveryMissing, true);
});

test("progressive closeout accounts for artifacts, verification, background work, and context", () => {
  const progressive = new ProgressiveExecutionRuntime({
    now: () => 10_000,
    constraints: {
      external_deadline_epoch_ms: 20_000,
      closeout_reserve_seconds: 2,
    },
  });
  const normal = progressive.decide(1_000, 10_000);
  const contextBoundary = progressive.decide(9_800, 10_000);

  assert.equal(normal.action, "continue");
  assert.equal(contextBoundary.action, "closeout");
  assert.match(contextBoundary.reason, /resource boundary/);
  assert.equal(contextBoundary.snapshot.phase, "closeout");
});
