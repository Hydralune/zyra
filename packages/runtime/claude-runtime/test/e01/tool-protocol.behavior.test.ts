import { describe, expect, test } from "bun:test";

import {
  DeterministicIdFactory,
  ManualClock,
  type JsonRecord,
} from "../../src/core/runtime-primitives.ts";
import { RuntimeCommandRuntime } from "../../src/protocol/command-runtime.ts";
import { ProtocolFramingRuntime } from "../../src/protocol/framing-runtime.ts";
import { ToolResultRuntime } from "../../src/tools/result-runtime.ts";

function ids(seed: string, sequence = 0): DeterministicIdFactory {
  return new DeterministicIdFactory(seed, sequence);
}

function resultBudget(overrides: Partial<{
  maximumCharacters: number;
  maximumTokens: number;
  maximumBlocks: number;
  maximumChunks: number;
  maximumInlineBytes: number;
  preserveHeadCharacters: number;
  preserveTailCharacters: number;
  includeDiagnostics: boolean;
  allowedSensitivity: "public" | "internal" | "sensitive" | "secret";
}> = {}) {
  return {
    maximumCharacters: overrides.maximumCharacters ?? 10_000,
    maximumTokens: overrides.maximumTokens ?? 5_000,
    maximumBlocks: overrides.maximumBlocks ?? 100,
    maximumChunks: overrides.maximumChunks ?? 100,
    maximumInlineBytes: overrides.maximumInlineBytes ?? 100_000,
    preserveHeadCharacters: overrides.preserveHeadCharacters ?? 100,
    preserveTailCharacters: overrides.preserveTailCharacters ?? 100,
    includeDiagnostics: overrides.includeDiagnostics ?? true,
    allowedSensitivity: overrides.allowedSensitivity ?? "internal",
  };
}

function beginResult(runtime: ToolResultRuntime, toolCallId = "tool-call-1") {
  return runtime.begin({
    resultId: `result-${toolCallId}`,
    toolCallId,
    toolName: "read_file",
    sessionId: "session-1",
    runId: "run-1",
    metadata: { fixture: "tool-result" },
  });
}

function textBlock(
  text: string,
  overrides: Partial<{
    blockId: string;
    kind: "text" | "diagnostic" | "error";
    sensitivity: "public" | "internal" | "sensitive" | "secret";
    metadata: JsonRecord;
  }> = {},
) {
  return {
    blockId: overrides.blockId,
    kind: overrides.kind ?? ("text" as const),
    text,
    json: null,
    mediaType: null,
    artifactId: null,
    sensitivity: overrides.sensitivity ?? ("internal" as const),
    metadata: overrides.metadata ?? {},
  };
}

function jsonBlock(value: JsonRecord, blockId = "json-block") {
  return {
    blockId,
    kind: "json" as const,
    text: null,
    json: value,
    mediaType: "application/json",
    artifactId: null,
    sensitivity: "internal" as const,
    metadata: {},
  };
}

function commandDescriptor(overrides: Partial<{
  commandName: string;
  risk: "read" | "mutate" | "interrupt" | "destructive";
  requiredCapabilities: string[];
  allowedSessionStates: string[];
  timeoutMilliseconds: number;
  idempotent: boolean;
  fields: Array<{
    name: string;
    type: "string" | "number" | "boolean" | "object" | "array";
    required: boolean;
    enumValues: Array<string | number | boolean | null>;
    minimum: number | null;
    maximum: number | null;
    maximumLength: number | null;
  }>;
}> = {}) {
  return {
    commandName: overrides.commandName ?? "runtime.inspect",
    owner: "test",
    description: "Inspect a runtime projection.",
    risk: overrides.risk ?? "read",
    fields: overrides.fields ?? [],
    requiredCapabilities: overrides.requiredCapabilities ?? ["runtime.read"],
    allowedSessionStates: overrides.allowedSessionStates ?? ["active"],
    timeoutMilliseconds: overrides.timeoutMilliseconds ?? 1_000,
    idempotent: overrides.idempotent ?? true,
    enabled: true,
    revision: 1,
    metadata: { fixture: "command" },
  };
}

function commandRequest(overrides: Partial<{
  commandId: string;
  commandName: string;
  sessionId: string;
  runId: string;
  actorId: string;
  actorCapabilities: string[];
  sessionState: string;
  expectedSessionRevision: number | null;
  idempotencyKey: string;
  correlationId: string;
  causationId: string | null;
  arguments: JsonRecord;
  requestedAt: number;
  expiresAt: number | null;
}> = {}) {
  return {
    commandId: overrides.commandId,
    commandName: overrides.commandName ?? "runtime.inspect",
    sessionId: overrides.sessionId ?? "session-1",
    runId: overrides.runId ?? "run-1",
    actorId: overrides.actorId ?? "operator-1",
    actorCapabilities: overrides.actorCapabilities ?? ["runtime.read"],
    sessionState: overrides.sessionState ?? "active",
    expectedSessionRevision: overrides.expectedSessionRevision ?? 10,
    idempotencyKey: overrides.idempotencyKey ?? "command-once",
    correlationId: overrides.correlationId ?? "correlation-command",
    causationId: overrides.causationId ?? null,
    arguments: overrides.arguments ?? {},
    requestedAt: overrides.requestedAt,
    expiresAt: overrides.expiresAt,
    metadata: { fixture: "command-request" },
  };
}

function allowAuthorization(clock = 1_000) {
  return {
    decisionId: "authorization-allow",
    allowed: true,
    reason: "capability and policy allow",
    decidedAt: clock,
    policyRevision: 7,
    obligations: { audit: true },
  };
}

function activateFraming(runtime: ProtocolFramingRuntime): ProtocolFramingRuntime {
  const hello = runtime.createOutbound(
    "hello",
    { protocol: "zyra-runtime-framing/v1", role: "behavior-test" },
    "handshake",
    null,
  );
  const receipt = runtime.feed(runtime.encode(hello))[0];
  if (receipt?.accepted !== true) {
    throw new Error(`test framing handshake failed: ${receipt?.reason ?? "missing receipt"}`);
  }
  runtime.ackOutbound(hello.sequence);
  return runtime;
}

function activeFraming(
  runId = "run-1",
  sessionId = "session-1",
): ProtocolFramingRuntime {
  return activateFraming(new ProtocolFramingRuntime(runId, sessionId));
}

describe("tool result custody", () => {
  test("assembles ordered chunks into a sealed delivery", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ToolResultRuntime({
      clock,
      ids: ids("result-assemble"),
    });
    const accumulator = beginResult(runtime);
    runtime.append({
      resultId: accumulator.resultId,
      chunkId: "chunk-1",
      sequence: 1,
      blocks: [textBlock("first", { blockId: "block-1" })],
    });
    clock.advance(10);
    runtime.append({
      resultId: accumulator.resultId,
      chunkId: "chunk-2",
      sequence: 2,
      blocks: [jsonBlock({ value: 2 }, "block-2")],
    });
    runtime.seal({
      resultId: accumulator.resultId,
      success: true,
      metadata: { exitCode: 0 },
    });
    const delivery = runtime.deliver(accumulator.resultId, resultBudget());
    expect(delivery.success).toBe(true);
    expect(delivery.content.map((block) => block.blockId)).toEqual([
      "block-1",
      "block-2",
    ]);
    expect(delivery.omittedBlockIds).toEqual([]);
    expect(delivery.truncated).toBe(false);
    expect(delivery.sourceDigest).toBeDefined();
    expect(runtime.getDelivery(delivery.deliveryId).deliveryDigest).toBe(
      delivery.deliveryDigest,
    );
  });

  test("rejects a chunk sequence gap without advancing expected sequence", () => {
    const runtime = new ToolResultRuntime({ ids: ids("result-gap") });
    const accumulator = beginResult(runtime);
    expect(() =>
      runtime.append({
        resultId: accumulator.resultId,
        sequence: 2,
        blocks: [textBlock("late")],
      }),
    ).toThrow("tool_result_chunk_sequence_gap");
    expect(runtime.getResult(accumulator.resultId).expectedSequence).toBe(1);
    expect(runtime.getResult(accumulator.resultId).chunks).toEqual([]);
  });

  test("deduplicates a repeated chunk and rejects a conflicting digest", () => {
    const runtime = new ToolResultRuntime({ ids: ids("result-idem") });
    const accumulator = beginResult(runtime);
    const input = {
      resultId: accumulator.resultId,
      chunkId: "stable-chunk",
      sequence: 1,
      blocks: [textBlock("stable", { blockId: "stable-block" })],
    };
    const first = runtime.append(input);
    const second = runtime.append(input);
    expect(second.digest).toBe(first.digest);
    expect(runtime.getResult(accumulator.resultId).chunks).toHaveLength(1);
    expect(() =>
      runtime.append({
        ...input,
        blocks: [textBlock("changed", { blockId: "stable-block" })],
      }),
    ).toThrow("tool_result_chunk_conflict");
  });

  test("truncates a large text block while preserving head and tail", () => {
    const runtime = new ToolResultRuntime({ ids: ids("result-truncate") });
    const accumulator = beginResult(runtime);
    const source = `${"H".repeat(100)}${"M".repeat(500)}${"T".repeat(100)}`;
    runtime.append({
      resultId: accumulator.resultId,
      sequence: 1,
      blocks: [textBlock(source, { blockId: "large-block" })],
      final: true,
    });
    const delivery = runtime.deliver(
      accumulator.resultId,
      resultBudget({
        maximumCharacters: 180,
        maximumTokens: 1_000,
        maximumInlineBytes: 1_000,
        preserveHeadCharacters: 60,
        preserveTailCharacters: 60,
      }),
    );
    expect(delivery.truncated).toBe(true);
    expect(delivery.content).toHaveLength(1);
    expect(delivery.content[0]?.text?.startsWith("H".repeat(60))).toBe(true);
    expect(delivery.content[0]?.text).toContain("chars omitted");
    expect(delivery.content[0]?.text?.endsWith("T".repeat(60))).toBe(true);
    expect(delivery.deliveredCharacters).toBeLessThanOrEqual(180);
  });

  test("withholds a block above the caller sensitivity ceiling", () => {
    const runtime = new ToolResultRuntime({ ids: ids("result-sensitive") });
    const accumulator = beginResult(runtime);
    runtime.append({
      resultId: accumulator.resultId,
      sequence: 1,
      blocks: [
        textBlock("production secret", {
          blockId: "secret-block",
          sensitivity: "secret",
        }),
      ],
      final: true,
    });
    const delivery = runtime.deliver(
      accumulator.resultId,
      resultBudget({ allowedSensitivity: "internal" }),
    );
    expect(delivery.content[0]?.text).toBe(
      "[content withheld by sensitivity policy]",
    );
    expect(delivery.content[0]?.metadata.redactionReason).toBe(
      "sensitivity_policy",
    );
    expect(delivery.redacted).toBe(true);
  });

  test("redacts bearer tokens from internal text results", () => {
    const runtime = new ToolResultRuntime({ ids: ids("result-bearer") });
    const accumulator = beginResult(runtime);
    runtime.append({
      resultId: accumulator.resultId,
      sequence: 1,
      blocks: [
        textBlock("authorization: Bearer abc.def-123", {
          blockId: "bearer-block",
          sensitivity: "internal",
        }),
      ],
      final: true,
    });
    const delivery = runtime.deliver(accumulator.resultId, resultBudget());
    expect(delivery.content[0]?.text).toBe(
      "authorization: Bearer [REDACTED]",
    );
    expect(delivery.content[0]?.metadata.redactionRuleIds).toEqual([
      "bearer-token",
    ]);
    expect(delivery.redacted).toBe(true);
  });

  test("redacts a complete private key block", () => {
    const runtime = new ToolResultRuntime({ ids: ids("result-key") });
    const accumulator = beginResult(runtime);
    runtime.append({
      resultId: accumulator.resultId,
      sequence: 1,
      blocks: [
        textBlock(
          "prefix\n-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\nsuffix",
          { blockId: "key-block", sensitivity: "sensitive" },
        ),
      ],
      final: true,
    });
    const delivery = runtime.deliver(
      accumulator.resultId,
      resultBudget({ allowedSensitivity: "sensitive" }),
    );
    expect(delivery.content[0]?.text).toContain("[PRIVATE KEY REDACTED]");
    expect(delivery.content[0]?.text).not.toContain("\nsecret\n");
  });

  test("omits diagnostics when delivery policy excludes them", () => {
    const runtime = new ToolResultRuntime({ ids: ids("result-diagnostic") });
    const accumulator = beginResult(runtime);
    runtime.append({
      resultId: accumulator.resultId,
      sequence: 1,
      blocks: [
        textBlock("useful", { blockId: "text-block" }),
        textBlock("debug trace", {
          blockId: "diagnostic-block",
          kind: "diagnostic",
        }),
      ],
      final: true,
    });
    const delivery = runtime.deliver(
      accumulator.resultId,
      resultBudget({ includeDiagnostics: false }),
    );
    expect(delivery.content.map((block) => block.blockId)).toEqual([
      "text-block",
    ]);
    expect(delivery.omittedBlockIds).toEqual(["diagnostic-block"]);
    expect(delivery.truncated).toBe(true);
  });

  test("enforces maximum block count across chunks", () => {
    const runtime = new ToolResultRuntime({ ids: ids("result-block-limit") });
    const accumulator = beginResult(runtime);
    runtime.append({
      resultId: accumulator.resultId,
      sequence: 1,
      blocks: [
        textBlock("one", { blockId: "block-1" }),
        textBlock("two", { blockId: "block-2" }),
        textBlock("three", { blockId: "block-3" }),
      ],
      final: true,
    });
    const delivery = runtime.deliver(
      accumulator.resultId,
      resultBudget({ maximumBlocks: 2 }),
    );
    expect(delivery.content).toHaveLength(2);
    expect(delivery.omittedBlockIds).toEqual(["block-3"]);
  });

  test("cannot deliver an open result before a terminal state", () => {
    const runtime = new ToolResultRuntime({ ids: ids("result-open") });
    const accumulator = beginResult(runtime);
    runtime.append({
      resultId: accumulator.resultId,
      sequence: 1,
      blocks: [textBlock("partial")],
    });
    expect(() => runtime.deliver(accumulator.resultId, resultBudget())).toThrow(
      "open_tool_result_not_deliverable",
    );
  });

  test("cancels an open result and delivers a failed terminal projection", () => {
    const runtime = new ToolResultRuntime({ ids: ids("result-cancel") });
    const accumulator = beginResult(runtime);
    runtime.append({
      resultId: accumulator.resultId,
      sequence: 1,
      blocks: [textBlock("partial")],
    });
    const cancelled = runtime.cancel(accumulator.resultId, "query cancelled");
    expect(cancelled.status).toBe("cancelled");
    expect(cancelled.errorCode).toBe("cancelled");
    const delivery = runtime.deliver(accumulator.resultId, resultBudget());
    expect(delivery.success).toBe(false);
    expect(delivery.content[0]?.text).toBe("partial");
  });

  test("marks a sealed result as late without changing its content", () => {
    const runtime = new ToolResultRuntime({ ids: ids("result-late") });
    const accumulator = beginResult(runtime);
    runtime.append({
      resultId: accumulator.resultId,
      sequence: 1,
      blocks: [textBlock("late value")],
      final: true,
    });
    const late = runtime.markLate(accumulator.resultId, "turn already closed");
    expect(late.status).toBe("late");
    const delivery = runtime.deliver(accumulator.resultId, resultBudget());
    expect(delivery.late).toBe(true);
    expect(delivery.content[0]?.text).toBe("late value");
  });

  test("deduplicates begin calls by tool call identity", () => {
    const runtime = new ToolResultRuntime({ ids: ids("result-begin-idem") });
    const first = beginResult(runtime, "tool-call-stable");
    const repeated = runtime.begin({
      resultId: "different-id",
      toolCallId: "tool-call-stable",
      toolName: "read_file",
      sessionId: "session-1",
      runId: "run-1",
    });
    expect(repeated.resultId).toBe(first.resultId);
    expect(runtime.snapshot().accumulators).toHaveLength(1);
    expect(() =>
      runtime.begin({
        toolCallId: "tool-call-stable",
        toolName: "write_file",
        sessionId: "session-1",
        runId: "run-1",
      }),
    ).toThrow("tool_result_begin_conflict");
  });

  test("restores accumulators deliveries and redaction rules", () => {
    const runtime = new ToolResultRuntime({ ids: ids("result-snapshot") });
    const accumulator = beginResult(runtime);
    runtime.append({
      resultId: accumulator.resultId,
      sequence: 1,
      blocks: [textBlock("snapshot value", { blockId: "snapshot-block" })],
      final: true,
    });
    const delivery = runtime.deliver(accumulator.resultId, resultBudget());
    const snapshot = runtime.snapshot();
    const restored = new ToolResultRuntime({ ids: ids("result-after") });
    restored.restore(snapshot);
    expect(restored.getResult(accumulator.resultId).status).toBe("sealed");
    expect(restored.getDelivery(delivery.deliveryId)).toEqual(delivery);
    expect(restored.snapshot().redactionRules).toHaveLength(2);
    snapshot.accumulators[0]!.toolName = "tampered";
    expect(() => restored.restore(snapshot)).toThrow(
      "tool_result_snapshot_checksum_mismatch",
    );
  });
});

describe("runtime command protocol", () => {
  test("accepts authorizes executes and commits an inspected result", async () => {
    const clock = new ManualClock(1_000);
    const runtime = new RuntimeCommandRuntime({
      clock,
      ids: ids("command-complete"),
    });
    runtime.register(commandDescriptor(), (context) => ({
      result: {
        sessionId: context.sessionId,
        revision: context.expectedSessionRevision,
      },
      effectIds: ["audit-effect-1"],
      sessionRevision: 10,
    }));
    const request = commandRequest({ requestedAt: 1_000, expiresAt: 2_000 });
    const accepted = runtime.accept(request);
    expect(accepted.status).toBe("accepted");
    const authorized = await runtime.authorize(
      accepted.commandId,
      request,
      () => allowAuthorization(1_001),
    );
    expect(authorized.status).toBe("authorized");
    const committed = await runtime.execute(accepted.commandId);
    expect(committed.status).toBe("committed");
    expect(committed.result).toEqual({ sessionId: "session-1", revision: 10 });
    expect(committed.effectIds).toEqual(["audit-effect-1"]);
    expect(committed.metadata.resultingSessionRevision).toBe(10);
  });

  test("rejects actors missing a required command capability", () => {
    const runtime = new RuntimeCommandRuntime({ ids: ids("command-capability") });
    runtime.register(commandDescriptor(), () => ({ result: {} }));
    expect(() =>
      runtime.accept(commandRequest({ actorCapabilities: [] })),
    ).toThrow("runtime_command_capability_missing");
    expect(runtime.list()).toEqual([]);
  });

  test("rejects commands in an incompatible session state", () => {
    const runtime = new RuntimeCommandRuntime({ ids: ids("command-state") });
    runtime.register(commandDescriptor(), () => ({ result: {} }));
    expect(() =>
      runtime.accept(commandRequest({ sessionState: "completed" })),
    ).toThrow("runtime_command_session_state_denied");
  });

  test("validates required string number enum and maximum length fields", () => {
    const runtime = new RuntimeCommandRuntime({ ids: ids("command-schema") });
    runtime.register(
      commandDescriptor({
        commandName: "runtime.configure",
        fields: [
          {
            name: "mode",
            type: "string",
            required: true,
            enumValues: ["safe", "fast"],
            minimum: null,
            maximum: null,
            maximumLength: 8,
          },
          {
            name: "limit",
            type: "number",
            required: true,
            enumValues: [],
            minimum: 1,
            maximum: 10,
            maximumLength: null,
          },
        ],
      }),
      () => ({ result: {} }),
    );
    expect(() =>
      runtime.accept(
        commandRequest({
          commandName: "runtime.configure",
          arguments: { mode: "unsafe", limit: 5 },
        }),
      ),
    ).toThrow("runtime_command_argument_enum");
    expect(() =>
      runtime.accept(
        commandRequest({
          commandName: "runtime.configure",
          idempotencyKey: "command-two",
          arguments: { mode: "safe", limit: 11 },
        }),
      ),
    ).toThrow("runtime_command_argument_maximum");
    expect(() =>
      runtime.accept(
        commandRequest({
          commandName: "runtime.configure",
          idempotencyKey: "command-three",
          arguments: { mode: "safe" },
        }),
      ),
    ).toThrow("runtime_command_argument_required");
  });

  test("rejects unknown command arguments instead of silently dropping them", () => {
    const runtime = new RuntimeCommandRuntime({ ids: ids("command-unknown") });
    runtime.register(commandDescriptor(), () => ({ result: {} }));
    expect(() =>
      runtime.accept(commandRequest({ arguments: { unexpected: true } })),
    ).toThrow("runtime_command_unknown_arguments");
  });

  test("persists an authorization denial as a terminal command", async () => {
    const runtime = new RuntimeCommandRuntime({ ids: ids("command-deny") });
    runtime.register(commandDescriptor(), () => ({ result: {} }));
    const request = commandRequest();
    const accepted = runtime.accept(request);
    const denied = await runtime.authorize(accepted.commandId, request, () => ({
      decisionId: "authorization-deny",
      allowed: false,
      reason: "maintenance window",
      decidedAt: 1_000,
      policyRevision: 8,
      obligations: {},
    }));
    expect(denied.status).toBe("rejected");
    expect(denied.errorCode).toBe("authorization_denied");
    expect(denied.errorMessage).toBe("maintenance window");
    await expect(runtime.execute(accepted.commandId)).rejects.toThrow(
      "runtime_command_status_conflict",
    );
  });

  test("deduplicates accepted commands by session and idempotency key", () => {
    const runtime = new RuntimeCommandRuntime({ ids: ids("command-idem") });
    runtime.register(commandDescriptor(), () => ({ result: {} }));
    const first = runtime.accept(commandRequest());
    const repeated = runtime.accept(
      commandRequest({ commandId: "other-command-id" }),
    );
    expect(repeated.commandId).toBe(first.commandId);
    expect(runtime.list()).toHaveLength(1);
    expect(() =>
      runtime.accept(
        commandRequest({
          commandId: "conflict",
          arguments: { changed: true },
        }),
      ),
    ).toThrow("runtime_command_unknown_arguments");
  });

  test("records handler failure and permits retry for an idempotent command", async () => {
    const runtime = new RuntimeCommandRuntime({ ids: ids("command-retry") });
    let attempt = 0;
    runtime.register(commandDescriptor(), () => {
      attempt += 1;
      if (attempt === 1) {
        throw new Error("temporary failure");
      }
      return { result: { attempt } };
    });
    const request = commandRequest();
    const accepted = runtime.accept(request);
    await runtime.authorize(accepted.commandId, request, () =>
      allowAuthorization(),
    );
    const failed = await runtime.execute(accepted.commandId);
    expect(failed.status).toBe("failed");
    expect(failed.errorCode).toBe("command_failed");
    expect(runtime.retry(accepted.commandId).status).toBe("authorized");
    const committed = await runtime.execute(accepted.commandId);
    expect(committed.status).toBe("committed");
    expect(committed.result).toEqual({ attempt: 2 });
  });

  test("does not retry a non-idempotent command", async () => {
    const runtime = new RuntimeCommandRuntime({ ids: ids("command-no-retry") });
    runtime.register(
      commandDescriptor({ idempotent: false, risk: "mutate" }),
      () => {
        throw new Error("effect outcome unknown");
      },
    );
    const request = commandRequest();
    const accepted = runtime.accept(request);
    await runtime.authorize(accepted.commandId, request, () =>
      allowAuthorization(),
    );
    expect((await runtime.execute(accepted.commandId)).status).toBe("failed");
    expect(() => runtime.retry(accepted.commandId)).toThrow(
      "runtime_command_not_retryable",
    );
  });

  test("times out a slow command and stores deterministic failure", async () => {
    const runtime = new RuntimeCommandRuntime({ ids: ids("command-timeout") });
    runtime.register(
      commandDescriptor({ timeoutMilliseconds: 5 }),
      async () => {
        await new Promise((resolve) => setTimeout(resolve, 25));
        return { result: { late: true } };
      },
    );
    const request = commandRequest();
    const accepted = runtime.accept(request);
    await runtime.authorize(accepted.commandId, request, () =>
      allowAuthorization(),
    );
    const failed = await runtime.execute(accepted.commandId);
    expect(failed.status).toBe("failed");
    expect(failed.errorCode).toBe("operation_timed_out");
    expect(failed.result).toBeNull();
  });

  test("expires accepted commands before authorization", () => {
    const clock = new ManualClock(1_000);
    const runtime = new RuntimeCommandRuntime({
      clock,
      ids: ids("command-expire"),
    });
    runtime.register(commandDescriptor(), () => ({ result: {} }));
    const accepted = runtime.accept(
      commandRequest({ requestedAt: 1_000, expiresAt: 1_010 }),
    );
    clock.advance(10);
    expect(runtime.expire()).toEqual([accepted.commandId]);
    expect(runtime.get(accepted.commandId).status).toBe("expired");
  });

  test("restores descriptors and rebinds existing local handlers", async () => {
    const runtime = new RuntimeCommandRuntime({ ids: ids("command-snapshot") });
    runtime.register(commandDescriptor(), () => ({ result: { source: "before" } }));
    const snapshot = runtime.snapshot();
    const restored = new RuntimeCommandRuntime({ ids: ids("command-after") });
    restored.register(commandDescriptor(), () => ({ result: { source: "after" } }));
    restored.restore(snapshot);
    const request = commandRequest({ commandId: "restored-command" });
    const accepted = restored.accept(request);
    await restored.authorize(accepted.commandId, request, () =>
      allowAuthorization(),
    );
    expect((await restored.execute(accepted.commandId)).result).toEqual({
      source: "after",
    });
    snapshot.descriptors[0]!.enabled = false;
    expect(() => restored.restore(snapshot)).toThrow(
      "runtime_command_snapshot_checksum_mismatch",
    );
  });
});

describe("runtime protocol framing", () => {
  test("assigns monotonic outbound sequence and tracks acknowledgements", () => {
    const runtime = activeFraming();
    const first = runtime.createOutbound(
      "start",
      { queryId: "query-1" },
      "correlation-1",
      null,
    );
    const second = runtime.createOutbound(
      "event",
      { phase: "reasoning" },
      "correlation-1",
      first.frameId,
    );
    expect(first.sequence).toBe(2);
    expect(second.sequence).toBe(3);
    expect(runtime.unacknowledged().map((frame) => frame.sequence)).toEqual([
      2,
      3,
    ]);
    runtime.ackOutbound(2);
    expect(runtime.unacknowledged().map((frame) => frame.sequence)).toEqual([3]);
    runtime.ackOutbound(3);
    expect(runtime.unacknowledged()).toEqual([]);
  });

  test("encodes a frame as JSONL and feeds partial transport chunks", () => {
    const sender = activeFraming();
    const receiver = activeFraming();
    const frame = sender.createOutbound(
      "event",
      { phase: "tool", effective: true },
      "correlation-1",
      null,
    );
    const encoded = sender.encode(frame);
    expect(encoded.endsWith("\n")).toBe(true);
    const split = Math.floor(encoded.length / 2);
    expect(receiver.feed(encoded.slice(0, split))).toEqual([]);
    const receipts = receiver.feed(encoded.slice(split));
    expect(receipts).toHaveLength(1);
    expect(receiver.project().inbound_sequence).toBe(2);
    expect(receiver.project().buffered_chars).toBe(0);
  });

  test("rejects an inbound sequence gap", () => {
    const sender = activeFraming();
    const receiver = activeFraming();
    sender.createOutbound("event", { value: 1 }, "correlation-1", null);
    const second = sender.createOutbound(
      "event",
      { value: 2 },
      "correlation-1",
      null,
    );
    const receipt = receiver.feed(sender.encode(second))[0];
    expect(receipt?.accepted).toBe(false);
    expect(receipt?.reason).toBe("sequence_gap_expected_2");
    expect(receiver.project().inbound_sequence).toBe(1);
  });

  test("does not advance inbound sequence for a checksum-tampered frame", () => {
    const sender = activeFraming();
    const receiver = activeFraming();
    const frame = sender.createOutbound(
      "event",
      { value: "original" },
      "correlation-1",
      null,
    );
    const encoded = sender.encode(frame).replace("original", "tampered");
    expect(() => receiver.feed(encoded)).toThrow();
    expect(receiver.project().inbound_sequence).toBe(1);
  });

  test("rejects a frame from a different run identity", () => {
    const sender = activeFraming("run-other", "session-1");
    const receiver = activeFraming();
    const frame = sender.createOutbound(
      "event",
      { value: 1 },
      "correlation-1",
      null,
    );
    const receipt = receiver.feed(sender.encode(frame))[0];
    expect(receipt?.accepted).toBe(false);
    expect(receipt?.reason).toBe("identity_mismatch");
  });

  test("enforces maximum encoded frame bytes", () => {
    const runtime = new ProtocolFramingRuntime("run-1", "session-1", {
      maximumFrameBytes: 256,
    });
    activateFraming(runtime);
    expect(() =>
      runtime.createOutbound(
        "event",
        { text: "x".repeat(1_000) },
        "correlation-1",
        null,
      ),
    ).toThrow();
  });

  test("round-trips framing state and unacknowledged frames", () => {
    const runtime = activeFraming();
    runtime.createOutbound(
      "start",
      { queryId: "query-1" },
      "correlation-1",
      null,
    );
    runtime.createOutbound(
      "event",
      { phase: "context" },
      "correlation-1",
      null,
    );
    runtime.ackOutbound(2);
    const snapshot = runtime.snapshot();
    const restored = new ProtocolFramingRuntime("run-1", "session-1");
    restored.restore(snapshot);
    expect(restored.unacknowledged().map((frame) => frame.sequence)).toEqual([3]);
    const third = restored.createOutbound(
      "result",
      { complete: true },
      "correlation-1",
      null,
    );
    expect(third.sequence).toBe(4);
  });

  test("rejects framing snapshots with a different session identity", () => {
    const runtime = activeFraming();
    runtime.createOutbound("event", { value: 1 }, "correlation-1", null);
    const snapshot = runtime.snapshot();
    const other = new ProtocolFramingRuntime("run-1", "session-2");
    expect(() => other.restore(snapshot)).toThrow();
  });

  test("rejects a checksum-tampered framing snapshot", () => {
    const runtime = activeFraming();
    runtime.createOutbound("event", { value: 1 }, "correlation-1", null);
    const snapshot = runtime.snapshot();
    snapshot.outboundSequence += 1;
    const restored = new ProtocolFramingRuntime("run-1", "session-1");
    expect(() => restored.restore(snapshot)).toThrow();
  });
});
