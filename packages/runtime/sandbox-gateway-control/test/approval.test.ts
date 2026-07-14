import assert from "node:assert/strict";
import test from "node:test";

import {
  ApprovalLedger,
  CallbackToolPermissionApprovalPort,
  Effects,
  GatewayProtocolError,
  InMemoryApprovalBindingStore,
  StructuredCommandPolicy,
  commandDigest,
  createCommandEnvelope,
  requestFingerprint,
  stableId,
  type ApprovalRequest,
} from "../src/index.ts";

function fixture() {
  let now = 1_000;
  let consumptionCalls = 0;
  const envelope = createCommandEnvelope({
    sessionId: "session-approval",
    runId: "run-approval",
    taskId: "task-approval",
    workerId: "CodeWorkerRuntime",
    toolUseId: "tool-approval",
    executable: "rg",
    argv: ["needle", "."],
  });
  const policy = new StructuredCommandPolicy().evaluate(envelope);
  const request: ApprovalRequest = Object.freeze({
    requestId: stableId("approval-request", {
      commandId: envelope.commandId,
      policyDigest: policy.policyDigest,
    }),
    requestFingerprint: requestFingerprint(envelope),
    envelope,
    policy,
    interactive: true,
    sealed: false,
    createdAt: now,
  });
  const port = new CallbackToolPermissionApprovalPort({
    evaluate: (value) => ({
      requestFingerprint: value.requestFingerprint,
      grantDigest: "sha256:test-grant",
      effect: Effects.allow,
      issuedAt: now,
      expiresAt: now + 60_000,
      permissionOwner: "ToolPermissionRuntime",
      metadata: {},
    }),
    validateAndConsume: (value, grant, replay) => {
      consumptionCalls += 1;
      return (
        value.requestFingerprint === grant.requestFingerprint &&
        commandDigest(value.envelope) === commandDigest(replay)
      );
    },
  });
  const ledger = new ApprovalLedger(
    new InMemoryApprovalBindingStore(),
    port,
    { clock: () => now },
  );
  return {
    envelope,
    request,
    ledger,
    advance: (milliseconds: number) => {
      now += milliseconds;
    },
    consumptionCalls: () => consumptionCalls,
  };
}

test("approval binds exact command and consumes once atomically", async () => {
  const value = fixture();
  const ticket = await value.ledger.issue(value.request);
  const consumed = await value.ledger.consume(ticket, value.envelope);

  assert.equal(consumed.accepted, true);
  assert.ok(consumed.binding.consumptionId);
  assert.equal(value.consumptionCalls(), 1);
  await assert.rejects(
    () => value.ledger.consume(ticket, value.envelope),
    (error: unknown) =>
      error instanceof GatewayProtocolError &&
      error.code === "approval_replay",
  );
  assert.equal(
    value.consumptionCalls(),
    1,
    "durable binding rejects replay before permission callback",
  );
});

test("approval rejects command mutation before permission callback", async () => {
  const value = fixture();
  const ticket = await value.ledger.issue(value.request);
  const mutated = Object.freeze({
    ...value.envelope,
    argv: Object.freeze(["different", "."]),
  });

  await assert.rejects(
    () => value.ledger.consume(ticket, mutated),
    (error: unknown) =>
      error instanceof GatewayProtocolError &&
      error.code === "command_mutated",
  );
  assert.equal(value.consumptionCalls(), 0);
});

test("approval expiration fails before permission callback", async () => {
  const value = fixture();
  const ticket = await value.ledger.issue(value.request);
  value.advance(60_001);

  await assert.rejects(
    () => value.ledger.consume(ticket, value.envelope),
    (error: unknown) =>
      error instanceof GatewayProtocolError &&
      error.code === "approval_expired",
  );
  assert.equal(value.consumptionCalls(), 0);
});
