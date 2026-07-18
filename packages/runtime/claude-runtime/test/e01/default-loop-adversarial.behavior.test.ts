import { describe, expect, test } from "bun:test";

import { PermissionedCapabilityHost } from "../../src/capability-host.ts";
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
import { TypeScriptCapabilityRuntime } from "../../src/capabilities.ts";
import {
  consumeCompatibleStream,
  createCompatibleEnvelope,
  parseServerSentEvents,
} from "../../src/provider/compatible-runtime.ts";
import { ProviderTransportRuntime } from "../../src/provider/transport-runtime.ts";
import { ModelIterationRuntime } from "../../src/loop/model-iteration-runtime.ts";
import { ToolExecutionSettlementRuntime } from "../../src/tools/execution-settlement-runtime.ts";

class GatewayHost implements RuntimeHost {
  readonly delegated: ToolExecutionRequest[][] = [];
  readonly events: RuntimeEvent[] = [];
  readonly artifacts: ArtifactRequest[] = [];

  async emitEvent(event: RuntimeEvent): Promise<void> {
    this.events.push(structuredClone(event));
  }

  async executeBatch(_batch: ToolBatch, requests: ToolExecutionRequest[]): Promise<ToolExecutionResponse[]> {
    const allowed = requests.filter((request) => {
      const decision = request.permissionDecision as unknown as { effect?: string } | undefined;
      return decision?.effect === "allow";
    });
    if (allowed.length > 0) {
      this.delegated.push(structuredClone(allowed));
    }
    return requests.map((request) => ({
      tool_call_id: request.toolCallId,
      ok: allowed.includes(request),
      summary: allowed.includes(request)
        ? `gateway executed ${request.toolName}`
        : `gateway blocked ${request.toolName}`,
      output: allowed.includes(request)
        ? { delegated: true, tool_name: request.toolName }
        : { delegated: false, tool_name: request.toolName },
      artifacts: [],
      error: allowed.includes(request)
        ? null
        : (request.permissionDecision as unknown as { effect?: string }).effect === "ask"
          ? "permission_approval_required"
          : "permission_denied",
      completed_at: new Date().toISOString(),
      metadata: {
        ...request.metadata,
        permission_effect: (request.permissionDecision as unknown as { effect?: string }).effect ?? "deny",
        permission_commit_only: "false",
        permission_abort_loop: (request.permissionDecision as unknown as { effect?: string }).effect === "ask" ? "true" : "false",
        permission_reason: String((request.permissionDecision as unknown as { reason?: string }).reason ?? "blocked"),
        permission_delegated: String(allowed.includes(request)),
        canonical_permission_owner: "python-durable-gateway",
        gateway_execution: String(allowed.includes(request)),
      },
    }));
  }

  async externalize(request: ArtifactRequest): Promise<ArtifactReceipt> {
    this.artifacts.push(structuredClone(request));
    return {
      artifact_id: `artifact-${this.artifacts.length}`,
      title: request.title,
      kind: request.kind,
      extension: request.extension,
      byte_size: request.content.length,
      sha256: "test-sha256",
      metadata: request.metadata,
    } as unknown as ArtifactReceipt;
  }

  isAborted(): boolean {
    return false;
  }
}

function runtimeInput(
  permissionPolicy: JsonObject,
  workspaceRoot = "G:\\agent-zoo\\zyra",
): RuntimeRunInput {
  return {
    runId: "adversarial-run",
    sessionId: "adversarial-session",
    taskId: "adversarial-task",
    workerRequestId: "adversarial-request",
    messages: [{ role: "user", content: "Exercise permission custody." }],
    turns: [],
    tools: [
      {
        name: "file_read",
        description: "Read one file",
        input_schema: {
          type: "object",
          properties: { path: { type: "string" } },
          required: ["path"],
        },
        read_only: true,
      },
      {
        name: "file_write",
        description: "Write one file",
        input_schema: {
          type: "object",
          properties: { path: { type: "string" }, content: { type: "string" } },
          required: ["path", "content"],
        },
        read_only: false,
      },
    ],
    config: {
      maxTurns: null,
      maxToolResultChars: 8_000,
      maxTurnToolResultChars: null,
      maxQueryContextChars: 32_000,
      continueOnError: false,
      maxReadOnlyConcurrency: 4,
      emitToolUseSummaries: true,
      allowEmptyTurns: false,
      modelName: "test-model",
      runtimeConstraints: { workspaceRoot },
      permissionPolicy,
      controlCommands: [],
    },
    restoredState: null,
  } as unknown as RuntimeRunInput;
}

function batch(requests: readonly ToolExecutionRequest[]): ToolBatch {
  return {
    batchId: "adversarial-batch",
    turnIndex: 0,
    batchIndex: 0,
    executionMode: "serial_non_read_only",
    steps: requests.map((request) => ({
      step_id: request.toolCallId,
      tool_name: request.toolName,
      arguments: request.arguments,
      prompt: "",
      metadata: {},
    })),
  } as unknown as ToolBatch;
}

function request(
  toolCallId: string,
  toolName: string,
  args: JsonObject,
  index: number,
): ToolExecutionRequest {
  return {
    toolCallId,
    toolName,
    arguments: args,
    turnIndex: 0,
    stepIndex: index,
    batchId: "adversarial-batch",
    batchIndex: 0,
    batchSize: 2,
    executionMode: "serial_non_read_only",
    metadata: {},
  };
}

describe("default permission execution custody", () => {
  test("e01.mutation.permission-deny-is-never-delegated", async () => {
    const gateway = new GatewayHost();
    const input = runtimeInput({ mode: "auto", interactive: false, headless: true });
    const capabilities = await TypeScriptCapabilityRuntime.open(input);
    try {
      const host = new PermissionedCapabilityHost(gateway, input, capabilities);
      const selected = [request(
        "deny-call",
        "file_write",
        { path: "C:\\outside\\blocked.txt", content: "blocked" },
        0,
      )];
      const result = await host.executeBatch(batch(selected), selected);

      expect(gateway.delegated).toHaveLength(0);
      expect(result).toHaveLength(1);
      expect(result[0]?.ok).toBe(false);
      expect(result[0]?.error).toBe("permission_denied");
      expect(result[0]?.metadata.permission_delegated).toBe("false");

      const snapshot = host.snapshot();
      const settlement = snapshot.settlement as JsonObject;
      const calls = settlement.calls as JsonObject[];
      expect(calls[0]?.state).toBe("permission_blocked");
      expect(calls[0]?.delegated).toBe(false);
    } finally {
      await capabilities.close();
    }
  });

  test("e01.mutation.permission-ask-is-suspended-without-gateway-execution", async () => {
    const gateway = new GatewayHost();
    const input = runtimeInput({ mode: "default", interactive: true, headless: false });
    const capabilities = await TypeScriptCapabilityRuntime.open(input);
    try {
      const host = new PermissionedCapabilityHost(gateway, input, capabilities);
      const selected = [request(
        "ask-call",
        "file_write",
        { path: "G:\\agent-zoo\\zyra\\allowed.txt", content: "approval" },
        0,
      )];
      const result = await host.executeBatch(batch(selected), selected);

      expect(gateway.delegated).toHaveLength(0);
      expect(result[0]?.error).toBe("permission_approval_required");
      expect(result[0]?.metadata.permission_abort_loop).toBe("true");
      expect(result[0]?.metadata.canonical_permission_owner).toBe("python-durable-gateway");
    } finally {
      await capabilities.close();
    }
  });

  test("e01.mutation.permission-mixed-batch-delegates-only-allowed-calls", async () => {
    const gateway = new GatewayHost();
    const input = runtimeInput({ mode: "default", interactive: true, headless: false });
    const capabilities = await TypeScriptCapabilityRuntime.open(input);
    try {
      const host = new PermissionedCapabilityHost(gateway, input, capabilities);
      const selected = [
        request("allow-call", "file_read", { path: "G:\\agent-zoo\\zyra\\package.json" }, 0),
        request("outside-call", "file_write", { path: "C:\\outside\\owned.txt", content: "no" }, 1),
      ];
      const result = await host.executeBatch(batch(selected), selected);

      expect(gateway.delegated).toHaveLength(1);
      expect(gateway.delegated[0]?.map((item) => item.toolCallId)).toEqual(["allow-call"]);
      expect(result.map((item) => item.tool_call_id)).toEqual(["allow-call", "outside-call"]);
      expect(result[0]?.ok).toBe(true);
      expect(result[1]?.ok).toBe(false);
      expect(result[1]?.error).toBe("permission_denied");
      expect(result[1]?.metadata.permission_reason).toContain("outside the bound workspace");

      const snapshot = host.snapshot();
      const settlement = snapshot.settlement as JsonObject;
      const calls = settlement.calls as JsonObject[];
      expect(calls.find((item) => item.callId === "allow-call")?.delegated).toBe(true);
      expect(calls.find((item) => item.callId === "outside-call")?.delegated).toBe(false);
    } finally {
      await capabilities.close();
    }
  });
});

describe("compatible provider protocol", () => {
  test("e01.mutation.compatible-envelope-uses-chat-completions-and-bearer-auth", () => {
    const envelope = createCompatibleEnvelope({
      baseUrl: "https://provider.example/v1",
      model: "provider-model",
      messages: [{ role: "user", content: "Inspect package.json" }],
      tools: [{
        name: "read",
        description: "Read a file",
        input_schema: { type: "object", properties: { path: { type: "string" } } },
      }],
      stream: true,
      maximumTokens: 1024,
    }, "provider-secret");

    expect(envelope.url).toBe("https://provider.example/v1/chat/completions");
    expect(envelope.headers.authorization).toBe("Bearer provider-secret");
    expect(envelope.headers["anthropic-version"]).toBeUndefined();
    expect(envelope.body.stream).toBe(true);
    expect(envelope.body.tools).toBeArray();
    expect(envelope.requestDigest).toHaveLength(64);
  });

  test("e01.mutation.compatible-stream-assembles-tool-deltas", async () => {
    const frames = [
      {
        id: "response-one",
        model: "provider-model",
        choices: [{
          index: 0,
          delta: {
            tool_calls: [{
              index: 0,
              id: "call-one",
              type: "function",
              function: { name: "read", arguments: "{\"path\":" },
            }],
          },
          finish_reason: null,
        }],
      },
      {
        id: "response-one",
        model: "provider-model",
        choices: [{
          index: 0,
          delta: { tool_calls: [{ index: 0, function: { arguments: "\"package.json\"}" } }] },
          finish_reason: "tool_calls",
        }],
        usage: { prompt_tokens: 10, completion_tokens: 4, total_tokens: 14 },
      },
    ];
    async function* source(): AsyncIterable<string> {
      yield `data: ${JSON.stringify(frames[0])}\n\n`;
      yield `data: ${JSON.stringify(frames[1])}\n\ndata: [DONE]\n\n`;
    }

    const response = await consumeCompatibleStream(source());
    expect(response.toolCalls).toHaveLength(1);
    expect(response.toolCalls[0]?.id).toBe("call-one");
    expect(response.toolCalls[0]?.name).toBe("read");
    expect(response.toolCalls[0]?.input).toEqual({ path: "package.json" });
    expect(response.finishReason).toBe("tool_use");
    expect(response.usage.totalTokens).toBe(14);
  });

  test("compatible SSE parser rejects malformed event JSON", () => {
    expect(() => parseServerSentEvents("data: {not-json}\n\n")).toThrow("invalid JSON");
  });

  test("e01.mutation.transport-slot-closes-after-parser-failure", async () => {
    const transport = new ProviderTransportRuntime(async () => new Response(
      "data: {not-json}\n\n",
      { status: 200, headers: { "content-type": "text/event-stream" } },
    ));
    const response = await transport.execute({
      url: "https://provider.example/v1/chat/completions",
      method: "POST",
      headers: { "x-client-request-id": "malformed-stream" },
      body: "{}",
      timeoutMs: 1_000,
    });
    await expect(consumeCompatibleStream(response.stream!)).rejects.toThrow("invalid JSON");
    const snapshot = transport.snapshot();
    expect(snapshot.endpoints[0]?.activeRequests).toBe(0);
    expect(snapshot.endpoints[0]?.queuedRequests).toBe(0);
  });
});

describe("model iteration recovery", () => {
  test("e01.mutation.provider-observation-requires-a-new-provider-round", () => {
    const runtime = new ModelIterationRuntime({
      sessionId: "iteration-session",
      runId: "iteration-run",
      taskId: "iteration-task",
      workerRequestId: "iteration-request",
    });
    runtime.start([{ role: "user", content: "Inspect package.json" }]);
    const first = runtime.beginProviderRound({
      requestKey: "round-zero",
      model: "provider-model",
      messages: [{ role: "user", content: "Inspect package.json" }],
    });
    runtime.acceptProviderResult({
      roundId: first.roundId,
      providerRequestId: "provider-request-one",
      model: "provider-model",
      stopReason: "tool_use",
      finalText: "",
      steps: [{ step_id: "tool-one", tool_name: "read", arguments: { path: "package.json" } }],
    });
    runtime.markToolRunning("tool-one", "turn-one");
    runtime.recordToolObservation({
      callId: "tool-one",
      turnId: "turn-one",
      ok: true,
      summary: "package.json read",
      output: { text: "{}" },
      error: null,
    });
    const revised = runtime.buildRevisionMessages(first.roundId);
    expect(revised.at(-1)?.role).toBe("user");
    expect(revised.at(-1)?.content).toBeArray();

    const second = runtime.beginProviderRound({
      requestKey: "round-one",
      model: "provider-model",
      messages: revised,
    });
    runtime.acceptProviderResult({
      roundId: second.roundId,
      providerRequestId: "provider-request-two",
      model: "provider-model",
      stopReason: "end_turn",
      finalText: "Inspection complete.",
      steps: [],
    });
    const audit = runtime.audit();
    expect(audit.ok).toBe(true);
    expect(audit.roundCount).toBe(2);
    expect(audit.toolCount).toBe(1);
    expect(runtime.snapshot().phase).toBe("completed");
  });

  test("e01.mutation.iteration-resume-does-not-replay-transition-ids", () => {
    const first = new ModelIterationRuntime({
      sessionId: "resume-session",
      runId: "resume-run-one",
      taskId: "resume-task",
      workerRequestId: "resume-request",
    });
    first.start([{ role: "user", content: "Continue work" }]);
    const before = first.snapshot();
    const restored = new ModelIterationRuntime({
      sessionId: "resume-session",
      runId: "resume-run-two",
      taskId: "resume-task",
      workerRequestId: "resume-request",
    });
    restored.restore(before, true);
    const after = restored.snapshot();
    const priorIds = new Set(before.transitions.map((item) => item.transitionId));
    const added = after.transitions.slice(before.transitions.length);
    expect(added).toHaveLength(1);
    expect(added.every((item) => !priorIds.has(item.transitionId))).toBe(true);
    expect(after.restartEpoch).toBe(before.restartEpoch + 1);
    expect(restored.audit().ok).toBe(true);
  });

  test("e01.mutation.iteration-repeated-tool-id-is-rejected-across-rounds", () => {
    const runtime = new ModelIterationRuntime({
      sessionId: "duplicate-session",
      runId: "duplicate-run",
      taskId: "duplicate-task",
      workerRequestId: "duplicate-request",
    });
    runtime.start([{ role: "user", content: "Run two rounds" }]);
    const first = runtime.beginProviderRound({
      requestKey: "duplicate-round-zero",
      model: "provider-model",
      messages: [{ role: "user", content: "Run two rounds" }],
    });
    runtime.acceptProviderResult({
      roundId: first.roundId,
      providerRequestId: "duplicate-provider-one",
      model: "provider-model",
      stopReason: "tool_use",
      finalText: "",
      steps: [{ step_id: "repeated-call", tool_name: "read", arguments: { path: "one" } }],
    });
    runtime.recordToolObservation({
      callId: "repeated-call",
      turnId: "duplicate-turn",
      ok: true,
      summary: "first result",
      output: { value: 1 },
      error: null,
    });
    const messages = runtime.buildRevisionMessages(first.roundId);
    const second = runtime.beginProviderRound({
      requestKey: "duplicate-round-one",
      model: "provider-model",
      messages,
    });
    expect(() => runtime.acceptProviderResult({
      roundId: second.roundId,
      providerRequestId: "duplicate-provider-two",
      model: "provider-model",
      stopReason: "tool_use",
      finalText: "",
      steps: [{ step_id: "repeated-call", tool_name: "read", arguments: { path: "two" } }],
    })).toThrow("repeated tool call id");
  });

  test("e01.mutation.iteration-snapshot-checksum-rejects-tampering", () => {
    const runtime = new ModelIterationRuntime({
      sessionId: "checksum-session",
      runId: "checksum-run",
      taskId: "checksum-task",
      workerRequestId: "checksum-request",
    });
    runtime.start([{ role: "user", content: "Protect this snapshot" }]);
    const snapshot = runtime.snapshot();
    snapshot.transcript[0] = { role: "user", content: "tampered" };
    const restored = new ModelIterationRuntime({
      sessionId: "checksum-session",
      runId: "checksum-run",
      taskId: "checksum-task",
      workerRequestId: "checksum-request",
    });
    expect(() => restored.restore(snapshot)).toThrow("checksum mismatch");
  });
});

describe("execution settlement custody", () => {
  test("e01.mutation.settlement-blocked-call-cannot-reach-gateway", () => {
    const runtime = settlementRuntime();
    runtime.planBatch({
      batchId: "settlement-batch",
      executionMode: "serial",
      calls: [plannedCall("blocked", 0), plannedCall("allowed", 1)],
    });
    runtime.recordPermission("blocked", "deny", "policy denied", "deny-rule");
    runtime.recordPermission("allowed", "allow", "policy allowed", "allow-rule");
    expect(() => runtime.beginDelegation("settlement-batch", ["blocked", "allowed"]))
      .toThrow("does not equal the allowed call set");
    runtime.beginDelegation("settlement-batch", ["allowed"]);
    expect(() => runtime.recordGatewayReceipt({
      callId: "blocked",
      ok: true,
      summary: "should not execute",
      output: {},
      error: null,
    })).toThrow("non-delegated call");
  });

  test("settlement preserves request order across blocked and delegated calls", () => {
    const runtime = settlementRuntime();
    runtime.planBatch({
      batchId: "ordered-batch",
      executionMode: "serial",
      calls: [plannedCall("first", 0), plannedCall("second", 1), plannedCall("third", 2)],
    });
    runtime.recordPermission("first", "allow", "allowed");
    runtime.recordPermission("second", "ask", "approval required");
    runtime.recordPermission("third", "allow", "allowed");
    runtime.beginDelegation("ordered-batch", ["first", "third"]);
    for (const callId of ["third", "first"]) {
      runtime.recordGatewayReceipt({
        callId,
        ok: true,
        summary: `${callId} completed`,
        output: { call_id: callId },
        error: null,
      });
    }
    runtime.completeBatch("ordered-batch");
    expect(runtime.orderedReceipts("ordered-batch").map((item) => item.callId))
      .toEqual(["first", "second", "third"]);
    expect(runtime.audit().ok).toBe(true);
  });

  test("settlement restore fences an in-flight delegated side effect", () => {
    const first = settlementRuntime("settlement-run-one");
    first.planBatch({
      batchId: "restart-batch",
      executionMode: "serial",
      calls: [plannedCall("in-flight", 0)],
    });
    first.recordPermission("in-flight", "allow", "allowed");
    first.beginDelegation("restart-batch", ["in-flight"]);
    const snapshot = first.snapshot();
    const restored = settlementRuntime("settlement-run-two");
    restored.restore(snapshot, true);
    expect(restored.call("in-flight")?.state).toBe("protocol_failed");
    expect(restored.call("in-flight")?.error).toBe("execution_interrupted_by_restart");
    expect(restored.audit().ok).toBe(true);
  });

  test("settlement progress enforces sequence and bounded storage", () => {
    const runtime = settlementRuntime();
    runtime.planBatch({
      batchId: "progress-batch",
      executionMode: "serial",
      calls: [plannedCall("progress-call", 0)],
    });
    runtime.recordPermission("progress-call", "allow", "allowed");
    runtime.beginDelegation("progress-batch", ["progress-call"]);
    const chunk = runtime.appendProgress("progress-call", 0, "stdout", "abcdefghij", 4);
    expect(chunk.content).toBe("abcd");
    expect(chunk.truncated).toBe(true);
    expect(() => runtime.appendProgress("progress-call", 2, "stdout", "gap"))
      .toThrow("sequence gap");
  });
});

function settlementRuntime(runId = "settlement-run"): ToolExecutionSettlementRuntime {
  return new ToolExecutionSettlementRuntime({
    runtimeId: "settlement-runtime",
    sessionId: "settlement-session",
    runId,
    workerRequestId: "settlement-request",
  });
}

function plannedCall(callId: string, position: number) {
  return {
    callId,
    toolName: "read",
    arguments: { path: `${callId}.txt` },
    localCapability: false,
    executionOwner: "python-tool-executor",
    position,
    metadata: {},
  };
}
