import assert from "node:assert/strict";
import test from "node:test";

import {
  RuntimeWatchdogObserver,
  runtimeWatchdogContract,
} from "../src/watchdog/runtime.ts";
import type {
  JsonObject,
  RuntimeEvent,
  RuntimeRunInput,
  ToolBatch,
  ToolExecutionRequest,
  ToolExecutionResponse,
} from "../src/contracts.ts";

function input(): RuntimeRunInput {
  return {
    runId: "run-watchdog-1",
    taskId: "task-watchdog-1",
    nodeId: "node-watchdog-1",
    workerRequestId: "attempt-watchdog-1",
    sessionId: "session-watchdog-1",
    messages: [],
    turns: [],
    tools: [],
    config: {
      modelName: "model-test",
      runtimeConstraints: { provider_id: "provider-test" },
    },
    metadata: {
      worker_id: "worker-test",
      backend_id: "backend-test",
      workspace_id: "workspace-test",
    },
  };
}

function batch(): { batch: ToolBatch; requests: ToolExecutionRequest[] } {
  const value: ToolBatch = {
    batchId: "batch-watchdog-1",
    turnIndex: 0,
    executionMode: "serial_non_read_only",
    steps: [{ tool_name: "shell", arguments: {} }],
  };
  return {
    batch: value,
    requests: [{
      toolCallId: "tool-call-watchdog-1",
      toolName: "shell",
      arguments: {},
      turnIndex: 0,
      stepIndex: 0,
      batchId: value.batchId,
      batchIndex: 0,
      batchSize: 1,
      executionMode: value.executionMode,
      metadata: {},
    }],
  };
}

test("runtime watchdog emits structured tool timeout and provider signals", async () => {
  const events: RuntimeEvent[] = [];
  const watchdog = new RuntimeWatchdogObserver(async (event) => {
    events.push(structuredClone(event));
  });
  watchdog.configure(input());
  const values = batch();
  const responses: ToolExecutionResponse[] = [{
    tool_call_id: values.requests[0].toolCallId,
    ok: false,
    summary: "timed out",
    output: {},
    artifacts: [],
    error: "timeout prose is not an identity source",
    metadata: { error_code: "tool_timeout" },
  }];
  await watchdog.observeToolBatch(values.batch, values.requests, responses, 31_000, 30_000);
  await watchdog.observeProviderError({
    name: "ProviderError",
    code: "rate_limited",
    status: 429,
    retryable: true,
  });

  assert.equal(events.length, 2);
  assert.equal(events[0].phase, "tool_failure_signal");
  assert.equal(events[0].fault_kind, "tool_timeout");
  assert.equal((events[0].refs as JsonObject).tool_call_id, "tool-call-watchdog-1");
  assert.equal(events[0].critical_ref_source, "structured_refs_only");
  assert.equal(events[1].fault_kind, "model_rate_limit");
  assert.equal((events[1].refs as JsonObject).provider_id, "provider-test");
  assert.equal(watchdog.snapshot().requirement_changed_is_fault, false);
});

test("disabled observer stops real emission and transport refs stay explicit", async () => {
  const events: RuntimeEvent[] = [];
  const watchdog = new RuntimeWatchdogObserver(async (event) => {
    events.push(event);
  });
  watchdog.configure(input());
  watchdog.disable("ts-tool-deadline");
  const values = batch();
  await watchdog.observeToolBatch(values.batch, values.requests, [{
    tool_call_id: values.requests[0].toolCallId,
    ok: false,
    summary: "timeout",
    output: {},
    artifacts: [],
    error: "timeout",
    metadata: { error_code: "tool_timeout" },
  }], 31_000, 30_000);
  assert.equal(events.length, 0);

  await watchdog.observeTransportClosed(
    "transport_closed",
    { mcpServerId: "mcp-server-1" },
    { reconnect_breaker: "closed" },
  );
  assert.equal(events.length, 1);
  assert.equal(events[0].fault_kind, "mcp_disconnected");
  assert.equal((events[0].refs as JsonObject).mcp_server_id, "mcp-server-1");
});

test("permission observer accepts the canonical Python host settlement metadata", async () => {
  const events: RuntimeEvent[] = [];
  const watchdog = new RuntimeWatchdogObserver(async (event) => {
    events.push(structuredClone(event));
  });
  watchdog.configure(input());
  const values = batch();

  await watchdog.observeToolBatch(values.batch, values.requests, [{
    tool_call_id: values.requests[0].toolCallId,
    ok: false,
    summary: "denied by TypeScript permission policy",
    output: {},
    artifacts: [],
    error: "permission_denied",
    metadata: {
      permission_effect: "deny",
      canonical_permission_owner: "typescript",
    },
  }], 3, 30_000);

  assert.equal(events.length, 1);
  assert.equal(events[0].fault_kind, "permission_denied");
  const observation = events[0].observation as JsonObject;
  const refs = observation.refs as JsonObject;
  assert.equal(observation.observation_id, refs.observation_id);
  assert.equal(refs.tool_call_id, "tool-call-watchdog-1");
});

test("watchdog contract records OMP supplementary and Python canonical ownership", () => {
  const contract = runtimeWatchdogContract();
  assert.equal(contract.canonical_signal_owner, "python.FaultStateStore");
  assert.equal(contract.requirement_changed_is_fault, false);
  assert.equal((contract.source_roles as JsonObject).oh_my_pi, "supplementary");
  assert.equal(contract.event_phase, "tool_failure_signal");
});
