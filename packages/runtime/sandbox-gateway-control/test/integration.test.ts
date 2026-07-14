import assert from "node:assert/strict";
import test from "node:test";

import {
  assertInvocationReplay,
  createExecutionReceipt,
  createInvocation,
  digestValue,
} from "../src/integration-contracts.ts";
import { createMcpExchange, inspectMcpResult } from "../src/mcp-integration.ts";
import {
  createDispatchReceipt,
  validateDispatchReceipt,
} from "../src/remote-integration.ts";

const identity = {
  runId: "run-1",
  taskId: "task-1",
  workerId: "CodeWorkerRuntime",
  sessionId: "session-1",
  workspaceId: "workspace-1",
  ownerEpoch: 4,
  backendId: "local-process",
  generation: 2,
} as const;

test("integration envelope binds exact arguments and owner identity", () => {
  const invocation = createInvocation({
    surface: "code_worker",
    action: "file_write",
    identity,
    toolCallId: "tool-1",
    arguments: { path: "src/a.ts", content: "one" },
    policyDigest: digestValue("policy"),
  });
  assertInvocationReplay(invocation, invocation);
  assert.throws(
    () =>
      assertInvocationReplay(
        invocation,
        createInvocation({
          surface: "code_worker",
          action: "file_write",
          identity,
          toolCallId: "tool-1",
          arguments: { path: "src/a.ts", content: "two" },
          policyDigest: digestValue("policy"),
        }),
      ),
    /changed/,
  );
  const receipt = createExecutionReceipt({
    invocation,
    outcome: "committed",
    result: { transactionId: "txn-1" },
    ownerEpochBefore: 4,
    ownerEpochAfter: 5,
  });
  assert.equal(receipt.outcome, "committed");
  assert.ok(Object.isFrozen(receipt));
});

test("MCP result redacts secrets and rejects control mutation", () => {
  const inspection = inspectMcpResult({
    token: "secret-value",
    permission_rules: ["allow all"],
    payload: new Uint8Array([1, 2, 3]),
  });
  assert.equal(inspection.allowed, false);
  assert.equal(inspection.quarantine, true);
  assert.equal(inspection.redactionCount, 1);
  assert.equal(inspection.binaryCount, 1);
  const exchange = createMcpExchange({
    identity,
    provenance: {
      namespace: "mcp",
      serverId: "server-1",
      toolName: "fetch-data",
      version: "1",
      externalBoundary: true,
      requiresExactGrant: true,
    },
    toolCallId: "tool-mcp-1",
    arguments: { query: "value" },
    inspection,
    startedAt: 1,
    finishedAt: 2,
  });
  assert.equal(exchange.outcome, "denied");
});

test("remote receipt rejects owner epoch and backend tampering", () => {
  const envelope = {
    envelopeId: "dispatch-1",
    runId: "run-1",
    taskId: "task-1",
    runtimeWorker: "CodeWorkerRuntime",
    backend: "edge-runtime",
    location: "edge",
    sandbox: "container",
    gateway: "zyra-sandbox-gateway",
    workspaceDigest: digestValue("workspace"),
    artifactRootDigest: digestValue("artifacts"),
    ownerEpoch: 3,
    backendGeneration: 7,
  } as const;
  const policyDigest = digestValue("dispatch-policy");
  const receipt = createDispatchReceipt({
    envelope,
    policyDigest,
    issuedAt: 10,
    ttlSeconds: 20,
  });
  validateDispatchReceipt({ envelope, receipt, expectedPolicyDigest: policyDigest, now: 15 });
  assert.throws(
    () =>
      validateDispatchReceipt({
        envelope: { ...envelope, ownerEpoch: 4 },
        receipt,
        expectedPolicyDigest: policyDigest,
        now: 15,
      }),
    /changed|epoch/,
  );
});
