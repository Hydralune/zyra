import assert from "node:assert/strict";
import { test } from "bun:test";

import type { JsonObject, ToolSpecContract } from "../../src/contracts.ts";
import {
  E02CheckpointBundleRuntime,
  E02ControlPlaneRuntime,
  E02ProjectionRuntime,
  E02RecoveryRuntime,
  E02RouteRuntime,
  type E02RuntimeIdentity,
} from "../../src/e02/index.ts";
import { digest } from "../../src/e02/canonical.ts";

function runtime(epoch = 1): E02RuntimeIdentity {
  return {
    runtimeId: "runtime-e02-test",
    runId: "run-e02-test",
    taskId: "task-e02-test",
    sessionId: "session-e02-test",
    workerRequestId: "worker-e02-test",
    epoch,
  };
}

function clock(start = Date.parse("2026-07-17T00:00:00.000Z")): {
  now: () => Date;
  advance: (milliseconds: number) => void;
} {
  let current = start;
  return {
    now: () => new Date(current),
    advance: (milliseconds: number) => { current += milliseconds; },
  };
}

function tool(name: string, operation = "read"): ToolSpecContract {
  return {
    name,
    purpose: `test capability ${name}`,
    source: "e02-test",
    input_schema: { type: "object", properties: {} },
    output_schema: { type: "object" },
    metadata: { operation, version: "1" },
  };
}

function checkpointPayloads(revision: number): Parameters<E02CheckpointBundleRuntime["stageAll"]>[1] {
  const value = (domain: string): { value: JsonObject; revision: number } => ({
    value: { domain, revision, state: `${domain}-${revision}` },
    revision,
  });
  return {
    permission: value("permission"),
    mcp: value("mcp"),
    skill: value("skill"),
    plugin: value("plugin"),
    command: value("command"),
    agent: value("agent"),
    route: value("route"),
    transition: value("transition"),
    event: value("event"),
    execution: value("execution"),
    "host-port": value("host-port"),
    custody: value("custody"),
  };
}

test("route custody selects one canonical TypeScript owner and consumes an exact lease", () => {
  const time = clock();
  const routes = new E02RouteRuntime({ runtime: runtime(), now: time.now });
  const revision = routes.reconcile([
    {
      spec: tool("shared_tool", "execute"),
      domain: "skill",
      owner: "typescript-skill",
      sourceId: "skills",
      priority: 600,
    },
    {
      spec: tool("shared_tool", "execute"),
      domain: "command",
      owner: "typescript-command",
      sourceId: "commands",
      priority: 800,
    },
    {
      spec: tool("health"),
      domain: "control",
      owner: "typescript-e02-control",
      sourceId: "control",
    },
  ]);
  assert.equal(revision?.revision, 1);
  assert.equal(routes.owner("shared_tool"), "typescript-command");
  assert.equal(routes.domain("shared_tool"), "command");
  assert.equal(routes.candidatesFor("shared_tool").length, 2);
  const lease = routes.issueLease({
    toolName: "shared_tool",
    toolCallId: "call-route-1",
    argumentsDigest: "sha256:arguments",
  });
  const consumed = routes.consumeLease(lease.leaseId, {
    toolName: "shared_tool",
    toolCallId: "call-route-1",
    argumentsDigest: "sha256:arguments",
  });
  assert.ok(consumed.consumedAt);
  assert.throws(
    () => routes.consumeLease(lease.leaseId, {
      toolName: "shared_tool",
      toolCallId: "call-route-1",
      argumentsDigest: "sha256:arguments",
    }),
    /consumed/,
  );
  assert.equal(routes.health().python_route_fallback, false);
});

test("route custody rejects equal-priority owners and stale lease bindings", () => {
  const time = clock();
  const routes = new E02RouteRuntime({ runtime: runtime(), now: time.now });
  assert.throws(
    () => routes.reconcile([
      { spec: tool("collision"), domain: "skill", owner: "typescript-a", priority: 10 },
      { spec: tool("collision"), domain: "plugin", owner: "typescript-b", priority: 10 },
    ]),
    /conflict/,
  );
  routes.reconcile([{ spec: tool("stable"), domain: "control", owner: "typescript-e02-control" }]);
  const lease = routes.issueLease({
    toolName: "stable",
    toolCallId: "call-stale",
    argumentsDigest: "sha256:one",
  });
  assert.throws(
    () => routes.consumeLease(lease.leaseId, {
      toolName: "stable",
      toolCallId: "call-stale",
      argumentsDigest: "sha256:two",
    }),
    /binding mismatch/,
  );
});

test("atomic checkpoint bundle stages every required domain before CAS commit", () => {
  const time = clock();
  const bundles = new E02CheckpointBundleRuntime({ runtime: runtime(), now: time.now });
  const open = bundles.begin("behavior-test", { suite: "e02" });
  bundles.stageAll(open.bundleId, checkpointPayloads(1));
  const sealed = bundles.seal(open.bundleId);
  assert.equal(sealed.missingDomains.length, 0);
  const committed = bundles.commit(open.bundleId);
  assert.equal(committed.phase, "committed");
  assert.equal(committed.sequence, 1);
  assert.equal(bundles.verifyBundle(committed.bundleId).ok, true);
  assert.equal(bundles.verifyChain().ok, true);
  bundles.recordDeliveryAttempt(committed.bundleId);
  const receipt = bundles.markDelivered(committed.bundleId, {
    providerReceiptId: "provider-checkpoint-1",
    deliveredSnapshotHash: "sha256:snapshot-1",
  });
  assert.equal(receipt.ok, true);
  assert.equal(bundles.bundle(committed.bundleId)?.phase, "delivered");
});

test("atomic checkpoint bundle fails closed on partial state and records indeterminate delivery", () => {
  const bundles = new E02CheckpointBundleRuntime({ runtime: runtime() });
  const partial = bundles.begin("partial-state");
  bundles.stage(partial.bundleId, "permission", { revision: 1 }, 1);
  assert.throws(() => bundles.seal(partial.bundleId), /missing required domains/);
  bundles.abort(partial.bundleId, "test cleanup");
  const complete = bundles.begin("delivery-failure");
  bundles.stageAll(complete.bundleId, checkpointPayloads(2));
  bundles.seal(complete.bundleId);
  bundles.commit(complete.bundleId);
  bundles.recordDeliveryAttempt(complete.bundleId);
  const receipt = bundles.markDeliveryFailure(complete.bundleId, new Error("store unavailable"));
  assert.equal(receipt.ok, false);
  assert.equal(bundles.bundle(complete.bundleId)?.phase, "delivery_indeterminate");
  const reconciled = bundles.reconcileDelivery(complete.bundleId, {
    outcome: "not_delivered",
    actor: "operator",
    reason: "provider confirmed absence",
  });
  assert.equal(reconciled.phase, "committed");
});

test("checkpoint history preserves prior runtime epochs and rejects future epoch injection", () => {
  const epochOne = new E02CheckpointBundleRuntime({ runtime: runtime(1) });
  const first = epochOne.begin("epoch-one");
  epochOne.stageAll(first.bundleId, checkpointPayloads(1));
  epochOne.seal(first.bundleId);
  epochOne.commit(first.bundleId);

  const epochTwo = new E02CheckpointBundleRuntime({
    runtime: runtime(2),
    snapshot: epochOne.snapshot(),
  });
  assert.equal(epochTwo.verifyBundle(first.bundleId).ok, true);
  const second = epochTwo.begin("epoch-two");
  epochTwo.stageAll(second.bundleId, checkpointPayloads(2));
  epochTwo.seal(second.bundleId);
  epochTwo.commit(second.bundleId);

  const epochTwoSnapshot = epochTwo.snapshot();
  const epochThree = new E02CheckpointBundleRuntime({
    runtime: runtime(3),
    snapshot: epochTwoSnapshot,
  });
  assert.equal(epochThree.verifyBundle(first.bundleId).ok, true);
  assert.equal(epochThree.verifyBundle(second.bundleId).ok, true);
  assert.equal(epochThree.verifyChain().ok, true);

  const tampered = structuredClone(epochTwoSnapshot);
  tampered.bundles[0]!.runtimeEpoch = 3;
  const { snapshotHash: _ignored, ...withoutHash } = tampered;
  tampered.snapshotHash = digest(withoutHash);
  assert.throws(
    () => new E02CheckpointBundleRuntime({ runtime: runtime(3), snapshot: tampered }),
    /binding mismatch/,
  );
});

test("control plane binds idempotency, CAS, effects, and durable results", async () => {
  const calls: JsonObject[] = [];
  const control = new E02ControlPlaneRuntime({
    runtime: runtime(),
    handler: async (request) => {
      calls.push(request.payload);
      return {
        result: { accepted: true, operation: request.operation },
        providerReceiptId: `receipt:${request.requestId}`,
      };
    },
  });
  const binding = {
    runId: runtime().runId,
    taskId: runtime().taskId,
    sessionId: runtime().sessionId,
    workerRequestId: runtime().workerRequestId,
    toolCallId: "control-call-1",
    actor: "tester",
    correlationId: "correlation-control-1",
  };
  const first = await control.execute({
    operation: "mcp.reload",
    binding,
    payload: { actor: "tester", reason: "reload test" },
    idempotencyKey: "mcp-reload-test",
  });
  assert.equal(first.phase, "committed");
  assert.equal(first.revisionAfter, 1);
  const replay = await control.execute({
    operation: "mcp.reload",
    binding,
    payload: { actor: "tester", reason: "reload test" },
    idempotencyKey: "mcp-reload-test",
  });
  assert.equal(replay.request.requestId, first.request.requestId);
  assert.equal(calls.length, 1);
  assert.equal(control.verifyRecord(first.request.requestId).ok, true);
  assert.equal(control.verifyChain().ok, true);
});

test("control plane rejects unknown fields, foreign bindings, stale CAS, and idempotency collisions", async () => {
  const control = new E02ControlPlaneRuntime({
    runtime: runtime(),
    handler: async () => ({ result: { ok: true } }),
  });
  const binding = {
    runId: runtime().runId,
    taskId: runtime().taskId,
    sessionId: runtime().sessionId,
    workerRequestId: runtime().workerRequestId,
    toolCallId: "control-call-2",
    actor: "tester",
    correlationId: "correlation-control-2",
  };
  assert.throws(
    () => control.prepare({
      operation: "mcp.reload",
      binding,
      payload: { actor: "tester", reason: "x", unknown: true },
    }),
    /unknown fields/,
  );
  assert.throws(
    () => control.prepare({
      operation: "mcp.reload",
      binding: { ...binding, runId: "foreign" },
      payload: { actor: "tester", reason: "x" },
    }),
    /binding differs/,
  );
  const stale = await control.execute({
    operation: "permission.mode.transition",
    binding,
    payload: { mode: "default", actor: "tester", reason: "cas" },
    expectedRevision: 3,
  });
  assert.equal(stale.phase, "failed");
  assert.equal(stale.error?.code, "control_compare_and_swap_failed");
});

test("projection runtime redacts secrets, payloads, history, and exposes stable cursor pages", () => {
  const projections = new E02ProjectionRuntime({ runtime: runtime() });
  const record = projections.capture({
    snapshotHash: "sha256:projection-source",
    snapshotSequence: 7,
    runtime: { status: "open", access_token: "secret-token" },
    permission: { mode: "default", history: [{ effect: "allow" }] },
    mcp: { clients: [{ id: "peer", authorization: "Bearer secret" }], payload: { private: true } },
  }, {
    domains: ["runtime", "permission", "mcp"],
    includeHistory: false,
    includePayloads: false,
    maximumArrayItems: 10,
  });
  assert.ok(record.redactionCount >= 3);
  const rendered = JSON.stringify(record.value);
  assert.doesNotMatch(rendered, /secret-token|Bearer secret|"private":true/);
  const first = projections.pageRecord(record.projectionId, 2);
  assert.equal(first.items.length, 2);
  assert.equal(first.exhausted, false);
  const second = projections.page(first.nextCursor!);
  assert.ok(second.offset >= 2);
  assert.equal(projections.verifyRecord(record.projectionId).ok, true);
});

test("projection runtime computes deterministic path-level diffs and subscription acknowledgements", () => {
  const projections = new E02ProjectionRuntime({ runtime: runtime() });
  const first = projections.capture({
    snapshotHash: "sha256:first",
    snapshotSequence: 1,
    runtime: { status: "open", count: 1 },
  }, { domains: ["runtime"], includeDetails: true });
  const second = projections.capture({
    snapshotHash: "sha256:second",
    snapshotSequence: 2,
    runtime: { status: "open", count: 2, added: true },
  }, { domains: ["runtime"], includeDetails: true });
  const difference = projections.diff(first.projectionId, second.projectionId);
  assert.ok(difference.changedPaths.some((path) => path.endsWith(".count")));
  assert.ok(difference.addedPaths.some((path) => path.endsWith(".added")));
  const subscription = projections.subscribe("web-console", ["runtime"]);
  assert.equal(projections.pendingForSubscription(subscription.subscriptionId).length, 2);
  const acknowledged = projections.acknowledge(subscription.subscriptionId, second.projectionId);
  assert.equal(acknowledged.lastAcknowledgedSequence, second.sequence);
  assert.equal(projections.pendingForSubscription(subscription.subscriptionId).length, 0);
});

test("recovery runtime diagnoses an indeterminate effect without ever scheduling effect replay", () => {
  const recovery = new E02RecoveryRuntime({ runtime: runtime() });
  const incident = recovery.observe({
    source: "execution",
    sourceId: "execution-indeterminate-1",
    sourcePhase: "effect_started",
    summary: "provider acknowledgement was lost",
    payload: { non_idempotent: true, effect_started: true, receipt: null },
    severity: "critical",
  });
  const diagnosis = recovery.diagnose(incident.incidentId);
  assert.equal(diagnosis.effectState, "indeterminate");
  assert.equal(diagnosis.replaySafe, false);
  const plan = recovery.buildPlan(incident.incidentId);
  assert.ok(plan.steps.some((step) => step.kind === "verify_provider_state"));
  assert.ok(plan.steps.every((step) => step.reexecutesExternalEffect === false));
  const recommendation = recovery.recommendNext(incident.incidentId);
  assert.equal(recommendation.automatic_effect_replay, false);
  assert.equal(recovery.verifyGraph().ok, true);
  assert.equal(recovery.health().scheduler_route_owner, false);
});

test("recovery runtime fences claims and requires ordered evidence-bearing steps", () => {
  const recovery = new E02RecoveryRuntime({ runtime: runtime() });
  const incident = recovery.observe({
    source: "checkpoint",
    sourceId: "checkpoint-indeterminate-1",
    sourcePhase: "delivery_indeterminate",
    summary: "checkpoint delivery acknowledgement missing",
    payload: { idempotent: true, delivery_attempts: 1 },
  });
  recovery.diagnose(incident.incidentId);
  const plan = recovery.buildPlan(incident.incidentId);
  const claim = recovery.claim(incident.incidentId, "operator-a");
  assert.throws(() => recovery.claim(incident.incidentId, "operator-b"), /claimed by/);
  const first = plan.steps[0]!;
  const started = recovery.startStep(incident.incidentId, first.stepId, claim.claimId);
  assert.equal(started.status, "running");
  const completed = recovery.completeStep(incident.incidentId, first.stepId, claim.claimId, {
    inspected: true,
  });
  assert.equal(completed.status, "committed");
  const next = recovery.plan(incident.incidentId)!.steps.find((step) => step.status === "pending");
  assert.ok(next);
  const timeline = recovery.timeline(incident.incidentId);
  assert.ok(timeline.some((entry) => entry.kind === "recovery_step_completed"));
  assert.equal(recovery.statistics().steps_reexecuting_external_effects, 0);
});
