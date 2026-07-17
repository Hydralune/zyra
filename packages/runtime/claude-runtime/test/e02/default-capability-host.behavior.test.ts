import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "bun:test";

import {
  PermissionedCapabilityHost,
  TypeScriptCapabilityRuntime,
  type ArtifactReceipt,
  type ArtifactRequest,
  type JsonObject,
  type RuntimeEvent,
  type RuntimeHost,
  type RuntimeRunInput,
  type ToolBatch,
  type ToolExecutionRequest,
  type ToolExecutionResponse,
} from "../../src/index.ts";

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
