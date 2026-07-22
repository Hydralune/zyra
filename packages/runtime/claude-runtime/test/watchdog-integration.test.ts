import assert from "node:assert/strict";
import test from "node:test";

import {
  CrossRuntimeFaultSupervisor,
  McpTransportSupervisor,
  ProviderStreamSupervisor,
  ToolExecutionSupervisor,
  WorkerRestartSupervisor,
  type SupplementaryObservationEmitter,
} from "../src/watchdog/index.ts";
import type { RuntimeRunInput, ToolExecutionRequest } from "../src/contracts.ts";
import type { WatchdogObservation, WatchdogRefs } from "../src/watchdog/runtime.ts";

function refs(overrides: Partial<WatchdogRefs> = {}): WatchdogRefs {
  return {
    runId: "run-watchdog-integration",
    taskId: "task-watchdog-integration",
    observationId: "observation-watchdog-integration",
    sessionId: "session-watchdog-integration",
    nodeId: "node-watchdog-integration",
    attemptId: "attempt-watchdog-integration",
    toolCallId: "",
    toolName: "",
    workerId: "",
    backendId: "",
    providerId: "",
    workspaceId: "",
    browserSessionId: "",
    mcpServerId: "",
    subagentTaskId: "",
    sourceStateRevision: 0,
    ...overrides,
  };
}

function observations(): {
  emitted: Array<{ observerId: string; observation: WatchdogObservation }>;
  emit: SupplementaryObservationEmitter;
} {
  const emitted: Array<{ observerId: string; observation: WatchdogObservation }> = [];
  return {
    emitted,
    emit: async (observerId, observation) => {
      emitted.push({ observerId, observation: structuredClone(observation) });
    },
  };
}

test("OMP-derived tool deadline aborts execution and preserves committed effects", async () => {
  let now = 1_000;
  const capture = observations();
  const runtime = new ToolExecutionSupervisor(capture.emit, () => now);
  const lease = runtime.arm({
    refs: refs({
      observationId: "tool-binding-1",
      toolCallId: "tool-call-live-1",
      toolName: "shell",
      sourceStateRevision: 7,
    }),
    generation: 7,
    deadlineMs: 10,
    batchId: "tool-batch-1",
    batchIndex: 0,
    batchSize: 2,
    executionMode: "concurrent_read_only",
    interruptible: true,
  });
  runtime.start("tool-call-live-1", 7);
  runtime.prepareEffect("tool-call-live-1", 7, {
    effectId: "effect-committed-1",
    idempotencyKey: "effect-idem-1",
    effectKind: "artifact_write",
    targetDigest: "sha256:target-1",
  });
  runtime.beginEffect("tool-call-live-1", 7, "effect-committed-1");
  const committed = runtime.commitEffect(
    "tool-call-live-1",
    7,
    "effect-committed-1",
    "sha256:result-1",
  );
  assert.equal(committed.committed, true);
  runtime.prepareEffect("tool-call-live-1", 7, {
    effectId: "effect-pending-1",
    idempotencyKey: "effect-idem-2",
    effectKind: "workspace_write",
    targetDigest: "sha256:target-2",
  });

  now = 1_011;
  assert.equal(await runtime.expire("tool-call-live-1", 7, "test_deadline"), true);
  assert.equal(lease.signal.aborted, true);
  assert.equal(capture.emitted.length, 1);
  assert.equal(capture.emitted[0]?.observation.code, "tool_timeout");
  assert.deepEqual(capture.emitted[0]?.observation.details.committed_effect_ids, ["effect-committed-1"]);
  assert.deepEqual(capture.emitted[0]?.observation.details.cancelled_effect_ids, ["effect-pending-1"]);
  assert.equal(capture.emitted[0]?.observation.details.committed_effects_may_repeat, false);

  const late = runtime.settle("tool-call-live-1", 7, "late-result-1", true);
  assert.equal(late.accepted, false);
  assert.equal(late.lateAfterTimeout, true);
  const replay = runtime.replayEffect("effect-idem-1");
  assert.equal(replay?.duplicate, true);
  assert.equal(replay?.resultDigest, "sha256:result-1");
  runtime.dispose();
});

test("OMP-derived MCP timeout aborts requests and crash storm opens breaker", async () => {
  let now = 2_000;
  const capture = observations();
  const runtime = new McpTransportSupervisor(capture.emit, () => now);
  runtime.connect({
    refs: refs({
      observationId: "mcp-binding-1",
      mcpServerId: "mcp-server-live-1",
      sourceStateRevision: 2,
    }),
    generation: 2,
    maxReconnectAttempts: 4,
    reconnectWindowMs: 100,
    reconnectBurstLimit: 1,
    baseBackoffMs: 5,
    maxBackoffMs: 20,
  });
  const request = runtime.beginRequest("mcp-server-live-1", 2, {
    requestId: "mcp-request-1",
    method: "tools/call",
    timeoutMs: 5,
    idempotencyKey: "mcp-request-idem-1",
    sideEffecting: true,
  });
  assert.equal(await runtime.timeoutRequest("mcp-server-live-1", 2, "mcp-request-1"), true);
  assert.equal(request.signal.aborted, true);
  assert.equal(capture.emitted[0]?.observation.code, "request_timeout");

  await runtime.disconnected("mcp-server-live-1", 2, "stdio_pipe_closed");
  assert.equal(capture.emitted.at(-1)?.observation.code, "transport_closed");
  let reconnectCalls = 0;
  assert.equal(
    await runtime.reconnect("mcp-server-live-1", 2, async () => {
      reconnectCalls += 1;
      return false;
    }),
    false,
  );
  now += 5;
  assert.equal(await runtime.reconnect("mcp-server-live-1", 2, async () => true), false);
  assert.equal(reconnectCalls, 1);
  const snapshot = runtime.snapshot();
  const connection = (snapshot.connections as Record<string, Record<string, unknown>>)["mcp-server-live-1"];
  assert.equal(connection?.phase, "breaker_open");
  assert.equal(snapshot.injection_fallback_when_disabled, false);
});

test("default capability supervision applies MCP timeout to the real execution signal", async () => {
  const capture = observations();
  const runtime = new CrossRuntimeFaultSupervisor(capture.emit);
  const input: RuntimeRunInput = {
    runId: "run-mcp-capability",
    taskId: "task-mcp-capability",
    nodeId: "node-mcp-capability",
    workerRequestId: "worker-request-mcp-capability",
    sessionId: "session-mcp-capability",
    messages: [],
    turns: [],
    tools: [],
    config: { modelName: "model-test", runtimeConstraints: {} },
    metadata: {},
  };
  runtime.configure(input);
  const request: ToolExecutionRequest = {
    toolCallId: "mcp-tool-call-live-1",
    toolName: "mcp:server-live:tools/call",
    arguments: {},
    turnIndex: 0,
    stepIndex: 0,
    batchId: "mcp-capability-batch-1",
    batchIndex: 0,
    batchSize: 1,
    executionMode: "serial_non_read_only",
    metadata: { mcp_request_timeout_ms: 5 },
  };

  await assert.rejects(
    runtime.superviseCapability(
      request,
      {
        namespace: "mcp",
        serverId: "server-live",
        version: "1",
        schemaDigest: "sha256:mcp-schema",
      },
      async (signal) => await new Promise((_resolve, reject) => {
        assert.ok(signal);
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      }),
    ),
    /MCP request timeout/,
  );
  assert.equal(capture.emitted.length, 1);
  assert.equal(capture.emitted[0]?.observation.category, "mcp");
  assert.equal(capture.emitted[0]?.observation.code, "request_timeout");
  assert.equal(capture.emitted[0]?.observation.refs.mcpServerId, "server-live");
  runtime.dispose();
});

test("provider partial stream resumes without replaying committed side effects", async () => {
  let now = 3_000;
  const capture = observations();
  const runtime = new ProviderStreamSupervisor(capture.emit, () => now);
  const providerRefs = refs({
    observationId: "provider-binding-1",
    providerId: "provider-live-1",
    backendId: "cloud-route-1",
    sourceStateRevision: 1,
  });
  runtime.begin({
    refs: providerRefs,
    streamId: "provider-stream-1",
    generation: 1,
    attemptNumber: 1,
    maxAttempts: 3,
  });
  assert.equal(runtime.acceptChunk("provider-stream-1", 1, {
    chunkId: "provider-chunk-1",
    sequence: 1,
    contentDigest: "sha256:chunk-1",
    textBytes: 12,
    toolCallIds: ["tool-provider-1"],
  }), true);
  assert.equal(runtime.commitEffect("provider-stream-1", 1, {
    effectId: "provider-effect-1",
    idempotencyKey: "provider-effect-idem-1",
    toolCallId: "tool-provider-1",
    resultDigest: "sha256:provider-effect-result-1",
    chunkSequence: 1,
  }), true);
  now += 20;
  const cursor = await runtime.interrupt("provider-stream-1", 1, {
    errorCode: "transport_closed",
    errorType: "ProviderTransportError",
    statusCode: 503,
    retryable: true,
    terminal: false,
    retryAfterMs: 5,
  });
  assert.ok(cursor);
  assert.equal(cursor?.lastCommittedChunkSequence, 1);
  assert.deepEqual(cursor?.committedEffectKeys, ["provider-effect-idem-1"]);
  assert.equal(capture.emitted[0]?.observation.details.partial_content_present, true);

  runtime.resume("provider-stream-1", cursor!, {
    refs: { ...providerRefs, observationId: "provider-binding-2", sourceStateRevision: 2 },
    streamId: "provider-stream-2",
    generation: 2,
    attemptNumber: 2,
    maxAttempts: 3,
  });
  assert.equal(runtime.shouldExecuteEffect("provider-effect-idem-1"), false);
  assert.equal(runtime.acceptChunk("provider-stream-2", 2, {
    chunkId: "provider-chunk-1",
    sequence: 1,
    contentDigest: "sha256:chunk-1",
    textBytes: 12,
  }), false);
  assert.equal(runtime.acceptChunk("provider-stream-2", 2, {
    chunkId: "provider-chunk-2",
    sequence: 2,
    contentDigest: "sha256:chunk-2",
    textBytes: 9,
  }), true);
  runtime.complete("provider-stream-2", 2, "sha256:provider-terminal");
  const snapshot = runtime.snapshot();
  const resumed = (snapshot.streams as Record<string, Record<string, unknown>>)["provider-stream-2"];
  assert.equal(resumed?.phase, "completed");
  assert.equal(resumed?.chunk_count, 2);
  assert.deepEqual(resumed?.committed_effect_keys, ["provider-effect-idem-1"]);
});

test("default model stream events drive provider supervision and typed interruption", async () => {
  const capture = observations();
  const runtime = new CrossRuntimeFaultSupervisor(capture.emit);
  runtime.configure({
    runId: "run-provider-events",
    taskId: "task-provider-events",
    nodeId: "node-provider-events",
    workerRequestId: "worker-request-provider-events",
    sessionId: "session-provider-events",
    messages: [],
    turns: [],
    tools: [],
    config: {
      modelName: "model-provider-events",
      runtimeConstraints: {
        provider_id: "provider-events",
        api_retry_max_attempts: 2,
      },
    },
    metadata: {},
  });

  await runtime.observeRuntimeEvent({
    phase: "model_request_prepared",
    provider_request: { request_id: "request-provider-events:1" },
  });
  await runtime.observeRuntimeEvent({
    phase: "model_stream_frame",
    model_stream_frame: {
      request_id: "request-provider-events:1",
      kind: "sse_chunk",
      frame_index: 1,
      model: "model-provider-events",
      chunk: { choices: [{ delta: { content: "partial" } }] },
    },
  });
  await runtime.observeRuntimeEvent({
    phase: "model_stream_report",
    model_stream: {
      request_id: "request-provider-events:1",
      ok: false,
      status: 503,
      decision: "retry",
      model: "model-provider-events",
      transport: "http_sse",
      recovery_plan: { delay_ms: 7 },
    },
  });

  assert.equal(capture.emitted.length, 1);
  assert.equal(capture.emitted[0]?.observation.category, "provider");
  assert.equal(capture.emitted[0]?.observation.details.partial_content_present, true);
  assert.equal(capture.emitted[0]?.observation.details.retry_after_ms, 7);
  const snapshot = runtime.snapshot();
  const streams = (snapshot.provider_streams as Record<string, unknown>).streams as Record<
    string,
    Record<string, unknown>
  >;
  assert.equal(streams["request-provider-events:1"]?.phase, "interrupted");
  assert.equal(streams["request-provider-events:1"]?.chunk_count, 1);
  runtime.dispose();
});

test("worker process restart requeues durable checkpointed job exactly once", async () => {
  let now = 4_000;
  const capture = observations();
  const runtime = new WorkerRestartSupervisor(capture.emit, () => now);
  const workerRefs = refs({
    observationId: "worker-binding-1",
    workerId: "worker-live-1",
    backendId: "edge-route-1",
    sourceStateRevision: 1,
  });
  runtime.attach({
    refs: workerRefs,
    generation: 1,
    pid: 1234,
    heartbeatIntervalMs: 10,
    heartbeatGraceIntervals: 2,
    maxRestarts: 2,
    restartWindowMs: 100,
  });
  runtime.registerJob("worker-live-1", 1, {
    jobId: "durable-job-1",
    attemptId: "attempt-worker-1",
    checkpointId: "checkpoint-before-start",
    idempotencyKey: "durable-job-idem-1",
    payloadDigest: "sha256:job-payload-1",
  });
  runtime.startJob("worker-live-1", 1, "durable-job-1");
  runtime.checkpointJob("worker-live-1", 1, "durable-job-1", "checkpoint-running-1");
  const requeued = await runtime.processExited("worker-live-1", 1, 17, "process_exit_17");
  assert.equal(requeued.length, 1);
  assert.equal(requeued[0]?.phase, "requeued");
  assert.equal(requeued[0]?.checkpointId, "checkpoint-running-1");
  assert.equal(capture.emitted[0]?.observation.code, "worker_lost");

  assert.equal(runtime.beginRestart("worker-live-1", 1, now), true);
  const restored = runtime.finishRestart("worker-live-1", 1, 2, 5678);
  assert.equal(restored.length, 1);
  assert.equal(restored[0]?.generation, 2);
  runtime.startJob("worker-live-1", 2, "durable-job-1");
  const completed = runtime.completeJob(
    "worker-live-1",
    2,
    "durable-job-1",
    "sha256:job-result-1",
  );
  assert.equal(completed.phase, "completed");
  const duplicate = runtime.registerJob("worker-live-1", 2, {
    jobId: "different-job-id",
    attemptId: "attempt-worker-2",
    checkpointId: "checkpoint-running-1",
    idempotencyKey: "durable-job-idem-1",
    payloadDigest: "sha256:job-payload-1",
  });
  assert.equal(duplicate.duplicate, true);
  assert.equal(duplicate.jobId, "durable-job-1");
});
