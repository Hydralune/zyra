import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "bun:test";

import {
  PermissionedCapabilityHost,
  TypeScriptCapabilityRuntime,
  normalizeBenchmarkContainerFileArguments,
  type ArtifactReceipt,
  type ArtifactRequest,
  type CapabilitySupervisionIdentity,
  type JsonObject,
  type RuntimeEvent,
  type RuntimeHost,
  type RuntimeRunInput,
  type ToolBatch,
  type ToolExecutionRequest,
  type ToolExecutionResponse,
} from "../../src/index.ts";

test("benchmark file tools map only container-workdir absolute paths", () => {
  assert.deepEqual(
    normalizeBenchmarkContainerFileArguments(
      "file_write",
      { path: "/app/src/program.py", content: "result" },
      "/app",
    ),
    { path: "src/program.py", content: "result" },
  );
  assert.deepEqual(
    normalizeBenchmarkContainerFileArguments(
      "file_read",
      { path: "/app/../etc/passwd" },
      "/app",
    ),
    { path: "/app/../etc/passwd" },
  );
  assert.deepEqual(
    normalizeBenchmarkContainerFileArguments(
      "shell",
      { command: "ls -la /app" },
      "/app",
    ),
    { command: "ls -la /app" },
  );
});

class CommitOnlyGateway implements RuntimeHost {
  readonly delegated: ToolExecutionRequest[] = [];
  settlements = 0;

  async emitEvent(_event: RuntimeEvent): Promise<void> {}

  async executeBatch(
    _batch: ToolBatch,
    requests: ToolExecutionRequest[],
  ): Promise<ToolExecutionResponse[]> {
    this.delegated.push(...requests);
    return requests.map((request) => ({
      tool_call_id: request.toolCallId,
      ok: true,
      summary: "permission commit accepted",
      output: {},
      artifacts: [],
      error: null,
      metadata: {
        permission_effect: "allow",
        permission_commit_only: request.permissionOnly ? "true" : "false",
        python_capability_fallback: "false",
      },
    }));
  }

  async settleCapability(): Promise<void> {
    this.settlements += 1;
  }

  async externalize(request: ArtifactRequest): Promise<ArtifactReceipt> {
    return {
      artifact_id: `artifact-${request.requestId}`,
      kind: request.kind,
      uri: `memory://${request.requestId}`,
      title: request.title,
    };
  }

  isAborted(): boolean {
    return false;
  }
}

class SupervisedGateway extends CommitOnlyGateway {
  readonly controller = new AbortController();

  superviseCapability<T>(
    _request: ToolExecutionRequest,
    _identity: CapabilitySupervisionIdentity,
    operation: (signal?: AbortSignal) => Promise<T>,
  ): Promise<T> {
    return operation(this.controller.signal);
  }
}

test("permissioned child hosts combine parent cancellation with delegated supervision", async () => {
  const parent = new AbortController();
  const gateway = new SupervisedGateway();
  const host = new PermissionedCapabilityHost(
    gateway,
    {
      runId: "variant-parent-run",
      sessionId: "variant-parent-session",
      workerRequestId: "variant-parent-worker",
      restoredState: null,
    } as RuntimeRunInput,
    {} as TypeScriptCapabilityRuntime,
    parent.signal,
  );
  let observed: AbortSignal | undefined;
  const supervised = host.superviseCapability(
    { toolCallId: "variant-child-call" } as ToolExecutionRequest,
    { namespace: "skill", serverId: "", version: "2", schemaDigest: "skill-v2" },
    async (signal) => {
      observed = signal;
      await new Promise<void>((_resolve, reject) => {
        const onAbort = (): void => reject(signal?.reason ?? new Error("cancelled"));
        if (signal?.aborted) onAbort();
        else signal?.addEventListener("abort", onAbort, { once: true });
      });
      return "unreachable";
    },
  );

  parent.abort(new Error("parent skill timed out"));
  await assert.rejects(supervised, /parent skill timed out/i);
  assert.equal(observed?.aborted, true);
  assert.equal(host.isAborted(), true);
  assert.equal(gateway.controller.signal.aborted, false);
});

test("e02.default-path binds permission permit and capability execution to the caller session revision", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "zyra-e02-default-host-"));
  const input: RuntimeRunInput = {
    runId: "e02-default-run",
    taskId: "e02-default-task",
    nodeId: "e02-default-node",
    workerRequestId: "e02-default-worker",
    sessionId: "e02-default-session",
    messages: [],
    turns: [],
    tools: [],
    config: {
      permissionPolicy: { mode: "default", default_effect: "allow" },
      runtimeConstraints: {
        workspaceRoot: workspace,
        watchSkills: false,
        watchPlugins: false,
      },
    },
    metadata: {
      session_revision: 7,
      canonical_permission_owner: "typescript",
    },
  };
  const capabilities = await TypeScriptCapabilityRuntime.open(input);
  try {
    const runtimeInput: RuntimeRunInput = {
      ...input,
      tools: capabilities.mergeToolSpecs(input.tools),
    };
    const gateway = new CommitOnlyGateway();
    const host = new PermissionedCapabilityHost(gateway, runtimeInput, capabilities);
    const batch: ToolBatch = {
      batchId: "e02-default-batch",
      turnIndex: 0,
      executionMode: "concurrent_read_only",
      steps: [{ tool_name: "e02_health", arguments: {} }],
    };
    const [result] = await host.executeBatch(batch, [{
      toolCallId: "e02-default-call",
      toolName: "e02_health",
      arguments: {},
      turnIndex: 0,
      stepIndex: 0,
      batchId: batch.batchId,
      batchIndex: 0,
      batchSize: 1,
      executionMode: batch.executionMode,
      metadata: {},
    }]);

    assert.equal(result?.ok, true);
    assert.equal(result?.metadata.canonical_permission_owner, "typescript");
    assert.equal(result?.metadata.python_capability_fallback, "false");
    assert.equal(gateway.delegated.length, 1);
    assert.equal(gateway.delegated[0]?.permissionOnly, true);
    assert.equal(gateway.delegated[0]?.e02SessionRevision, 7);
    assert.equal(gateway.settlements, 1);

    const snapshot = capabilities.snapshot();
    const permits = (snapshot.executionLedger as unknown as { permits: Array<{ sessionRevision: number }> }).permits;
    assert.equal(permits.length, 1);
    assert.equal(permits[0]?.sessionRevision, 7);
    assert.equal((snapshot.runtime as unknown as JsonObject).sessionRevision, undefined);
  } finally {
    await capabilities.close();
    await rm(workspace, { recursive: true, force: true });
  }
});

test("e02.default-path honors canonical access_mode metadata when inferring permission operation", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "zyra-e02-access-mode-"));
  const input: RuntimeRunInput = {
    runId: "e02-access-mode-run",
    taskId: "e02-access-mode-task",
    nodeId: "e02-access-mode-node",
    workerRequestId: "e02-access-mode-worker",
    sessionId: "e02-access-mode-session",
    messages: [],
    turns: [],
    tools: [],
    config: {
      permissionPolicy: { mode: "default", default_effect: "allow" },
      runtimeConstraints: {
        workspaceRoot: workspace,
        watchSkills: false,
        watchPlugins: false,
      },
    },
  };
  const capabilities = await TypeScriptCapabilityRuntime.open(input);
  try {
    const runtimeInput = { ...input, tools: capabilities.mergeToolSpecs(input.tools) };
    const projectionSpec = runtimeInput.tools.find((tool) => tool.name === "e02_projection");
    assert.deepEqual(
      ((projectionSpec?.input_schema.properties as JsonObject).action as JsonObject).enum,
      ["capture", "read", "snapshot", "page", "diff", "list", "subscribe", "pending", "ack"],
    );
    const gateway = new CommitOnlyGateway();
    const host = new PermissionedCapabilityHost(gateway, runtimeInput, capabilities);
    const batch: ToolBatch = {
      batchId: "e02-access-mode-batch",
      turnIndex: 0,
      executionMode: "concurrent_read_only",
      steps: [{ tool_name: "e02_projection", arguments: { action: "read" } }],
    };
    const [result] = await host.executeBatch(batch, [{
      toolCallId: "e02-access-mode-call",
      toolName: "e02_projection",
      arguments: { action: "read" },
      turnIndex: 0,
      stepIndex: 0,
      batchId: batch.batchId,
      batchIndex: 0,
      batchSize: 1,
      executionMode: batch.executionMode,
      metadata: {},
    }]);

    assert.equal(result?.ok, true);
    const decision = gateway.delegated[0]?.permissionDecision as unknown as {
      requestBinding?: { operation?: string };
    };
    assert.equal(decision.requestBinding?.operation, "read");
  } finally {
    await capabilities.close();
    await rm(workspace, { recursive: true, force: true });
  }
});

test("e02.default-path treats artifact materialization as a bounded write", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "zyra-e02-artifact-write-"));
  const input: RuntimeRunInput = {
    runId: "e02-artifact-write-run",
    taskId: "e02-artifact-write-task",
    nodeId: "e02-artifact-write-node",
    workerRequestId: "e02-artifact-write-worker",
    sessionId: "e02-artifact-write-session",
    messages: [],
    turns: [],
    tools: [
      {
        name: "artifact_write",
        purpose: "persist evidence",
        source: "test",
        input_schema: { type: "object" },
        output_schema: {},
        metadata: { access_mode: "artifact_write", read_only: "false" },
      },
      {
        name: "checkpoint",
        purpose: "read or persist a checkpoint",
        source: "test",
        input_schema: { type: "object" },
        output_schema: {},
        metadata: { access_mode: "read_only", read_only: "true" },
      },
    ],
    config: {
      permissionPolicy: { mode: "auto", default_effect: "allow" },
      runtimeConstraints: {
        workspaceRoot: workspace,
        watchSkills: false,
        watchPlugins: false,
      },
    },
  };
  const capabilities = await TypeScriptCapabilityRuntime.open(input);
  try {
    const gateway = new CommitOnlyGateway();
    const host = new PermissionedCapabilityHost(gateway, input, capabilities);
    const batch: ToolBatch = {
      batchId: "e02-artifact-write-batch",
      turnIndex: 0,
      executionMode: "serial_non_read_only",
      steps: [],
    };
    const calls: ToolExecutionRequest[] = [
      {
        toolCallId: "e02-artifact-write-call",
        toolName: "artifact_write",
        arguments: { content: "bounded evidence" },
        turnIndex: 0,
        stepIndex: 0,
        batchId: batch.batchId,
        batchIndex: 0,
        batchSize: 2,
        executionMode: batch.executionMode,
        metadata: {},
      },
      {
        toolCallId: "e02-checkpoint-artifact-call",
        toolName: "checkpoint",
        arguments: { write_artifact: true },
        turnIndex: 0,
        stepIndex: 1,
        batchId: batch.batchId,
        batchIndex: 1,
        batchSize: 2,
        executionMode: batch.executionMode,
        metadata: {},
      },
    ];

    const results = await host.executeBatch(batch, calls);

    assert.deepEqual(results.map((result) => result.ok), [true, true]);
    assert.deepEqual(
      gateway.delegated.map((request) => {
        const decision = request.permissionDecision as unknown as {
          requestBinding?: { operation?: string };
        };
        return decision.requestBinding?.operation;
      }),
      ["write", "write"],
    );
  } finally {
    await capabilities.close();
    await rm(workspace, { recursive: true, force: true });
  }
});

test("e02.default-path sends the canonical permission tool alias to the physical host", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "zyra-e02-alias-host-"));
  const input: RuntimeRunInput = {
    runId: "e02-alias-run",
    taskId: "e02-alias-task",
    nodeId: "e02-alias-node",
    workerRequestId: "e02-alias-worker",
    sessionId: "e02-alias-session",
    messages: [],
    turns: [],
    tools: [],
    config: {
      permissionPolicy: { mode: "default", default_effect: "allow" },
      runtimeConstraints: {
        workspaceRoot: workspace,
        watchSkills: false,
        watchPlugins: false,
      },
    },
  };
  const capabilities = await TypeScriptCapabilityRuntime.open(input);
  try {
    const runtimeInput: RuntimeRunInput = {
      ...input,
      tools: capabilities.mergeToolSpecs(input.tools),
    };
    const gateway = new CommitOnlyGateway();
    const host = new PermissionedCapabilityHost(gateway, runtimeInput, capabilities);
    const batch: ToolBatch = {
      batchId: "e02-alias-batch",
      turnIndex: 0,
      executionMode: "serial_non_read_only",
      steps: [{ tool_name: "Task", arguments: { agent_type: "missing", prompt: "repair" } }],
    };
    await host.executeBatch(batch, [{
      toolCallId: "e02-alias-call",
      toolName: "Task",
      arguments: { agent_type: "missing", prompt: "repair" },
      turnIndex: 0,
      stepIndex: 0,
      batchId: batch.batchId,
      batchIndex: 0,
      batchSize: 1,
      executionMode: batch.executionMode,
      metadata: {},
    }]);

    assert.equal(gateway.delegated[0]?.toolName, "Agent");
    const decision = gateway.delegated[0]?.permissionDecision as unknown as {
      requestBinding?: { tool_name?: string };
    };
    assert.equal(decision.requestBinding?.tool_name, "Agent");
  } finally {
    await capabilities.close();
    await rm(workspace, { recursive: true, force: true });
  }
});

test("e02.disable.typescript-runtime fails closed before any Python fallback can open", async () => {
  const previous = process.env.ZYRA_DISABLE_E02_TYPESCRIPT_RUNTIME;
  process.env.ZYRA_DISABLE_E02_TYPESCRIPT_RUNTIME = "1";
  try {
    await assert.rejects(
      () => TypeScriptCapabilityRuntime.open({
        runId: "e02-disabled-run",
        taskId: "e02-disabled-task",
        workerRequestId: "e02-disabled-worker",
        sessionId: "e02-disabled-session",
        messages: [],
        turns: [],
        tools: [],
        config: { permissionPolicy: {}, runtimeConstraints: {} },
      }),
      /e02 capability runtime is disabled|no Python fallback/i,
    );
  } finally {
    if (previous === undefined) delete process.env.ZYRA_DISABLE_E02_TYPESCRIPT_RUNTIME;
    else process.env.ZYRA_DISABLE_E02_TYPESCRIPT_RUNTIME = previous;
  }
});

test("e02.restore keeps request-scoped authority on the original worker request", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "zyra-e02-request-scope-"));
  const input: RuntimeRunInput = {
    runId: "e02-request-scope-run",
    taskId: "e02-request-scope-task",
    nodeId: "e02-request-scope-node",
    workerRequestId: "e02-request-scope-worker-1",
    sessionId: "e02-request-scope-session",
    messages: [],
    turns: [],
    tools: [],
    config: {
      permissionPolicy: { mode: "default", default_effect: "allow" },
      runtimeConstraints: {
        workspaceRoot: workspace,
        watchSkills: false,
        watchPlugins: false,
      },
    },
  };
  const original = await TypeScriptCapabilityRuntime.open(input);
  const snapshot = original.snapshot();
  await original.close();
  try {
    const sameRequest = await TypeScriptCapabilityRuntime.open({
      ...input,
      restoredState: { e02: snapshot as unknown as JsonObject },
    });
    try {
      assert.equal(sameRequest.e02.runtime.workerRequestId, input.workerRequestId);
      assert.equal(sameRequest.e02.runtime.epoch, snapshot.runtime.epoch + 1);
    } finally {
      await sameRequest.close();
    }

    const laterRequest = await TypeScriptCapabilityRuntime.open({
      ...input,
      workerRequestId: "e02-request-scope-worker-2",
      restoredState: { e02: snapshot as unknown as JsonObject },
    });
    try {
      assert.equal(laterRequest.e02.runtime.workerRequestId, "e02-request-scope-worker-2");
      assert.equal(laterRequest.e02.runtime.epoch, 1);
      assert.equal(laterRequest.snapshot().executionSequence, 0);
    } finally {
      await laterRequest.close();
    }
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }
});
