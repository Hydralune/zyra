import assert from "node:assert/strict";
import test from "node:test";

import {
  ClaudeRuntimeCore,
  type ArtifactReceipt,
  type ArtifactRequest,
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
      output: request.arguments.large === true
        ? { content: "x".repeat(1000) }
        : { arguments: request.arguments },
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
  assert.ok(host.events.some((event) => event.phase === "tool_result_budget_exceeded"));
  assert.ok(host.events.some((event) => event.phase === "context_compacted"));
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
  assert.equal(resumed.sessionSnapshot.lineage.restored, true);
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
