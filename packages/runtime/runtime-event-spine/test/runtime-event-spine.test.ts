import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  AgentMessageRouter,
  EventSpineError,
  LowEntropyBaselineHarness,
  RuntimeEventSpine,
  SenderKind,
  TrustLevel,
  byteLength,
  inlinePayloadContainsForbiddenContent,
  normalizeOmpFrame,
  type RuntimeEventDraft,
  type SubscriptionSpec,
} from "../src/index.ts";

function withSpine(
  body: (spine: RuntimeEventSpine, root: string) => void,
  options: { builtIns?: boolean } = {},
): void {
  const root = mkdtempSync(join(tmpdir(), "zyra-event-spine-"));
  const spine = new RuntimeEventSpine({
    sqlitePath: join(root, "events.sqlite3"),
    artifactRoot: join(root, "artifacts"),
    registerBuiltIns: options.builtIns ?? true,
  });
  try {
    body(spine, root);
  } finally {
    spine.close();
    rmSync(root, { recursive: true, force: true });
  }
}

function draft(
  eventId: string,
  aggregateId: string,
  overrides: Partial<RuntimeEventDraft> = {},
): RuntimeEventDraft {
  const taskId = aggregateId.split(":").at(-1) ?? aggregateId;
  return {
    eventId,
    eventType: "runtime.task.created",
    aggregateId,
    idempotencyKey: `idem:${eventId}`,
    correlationId: `corr:${taskId}`,
    identity: { runId: "run-test", sessionId: "session-test", taskId, nodeId: "root" },
    sender: { kind: SenderKind.API, id: "zyra-api", capabilityRefs: [] },
    intent: "plan",
    summary: "task admitted",
    stateDelta: {
      domain: "task",
      operation: "set",
      path: ["task", taskId],
      afterDigest: `sha256:${"a".repeat(64)}`,
      effective: true,
    },
    provenance: {
      sourceRepository: "opencode",
      sourceModule: "packages/core/src/event.ts",
      migrationRole: "primary",
      producerVersion: "05c-test",
      trust: TrustLevel.INTERNAL,
    },
    inline: { legacy_event_type: "task_created" },
    ...overrides,
  };
}

function subscription(
  subscriptionId: string,
  recipientId: string,
  capacity = 8,
): SubscriptionSpec {
  return {
    subscriptionId,
    recipient: { kind: "worker", id: recipientId, requiredCapabilities: ["worker.execute"] },
    eventTypes: ["runtime.task.*"],
    intents: ["plan", "status"],
    aggregatePrefixes: [],
    taskIds: [],
    capabilityRefs: ["worker.execute"],
    capacity,
    maxAttempts: 3,
    ackTimeoutMs: 100,
    backpressureMode: "reject",
    enabled: true,
    priority: 10,
    metadata: {},
  };
}

test("canonical store assigns a stable global cursor across aggregates", () => {
  withSpine((spine) => {
    const first = spine.appendCanonical(draft("evt-a", "task:a"));
    const second = spine.appendCanonical(draft("evt-b", "task:b"));
    assert.equal(first.event.aggregateSequence, 0);
    assert.equal(second.event.aggregateSequence, 0);
    assert.deepEqual([first.event.globalSequence, second.event.globalSequence], [1, 2]);
    assert.equal(first.route?.eventId, first.event.eventId);
    assert.equal(Number((spine.store.db.prepare("SELECT COUNT(*) AS count FROM runtime_event_routes").get() as { count: number }).count), 2);

    const page1 = spine.query({ afterGlobalSequence: 0, limit: 1 });
    assert.equal(page1.items[0]?.eventId, "evt-a");
    assert.equal(page1.hasMore, true);
    assert.equal(page1.nextSequence, 1);
    assert.ok(page1.nextCursor);
    const page2 = spine.query({ cursor: page1.nextCursor, limit: 10 });
    assert.deepEqual(page2.items.map((event) => event.eventId), ["evt-b"]);
    assert.equal(page2.highWatermark, 2);

    const comparison = spine.metrics.compare() as {
      baselines: Array<{ strategy: string; messageCount: number; taskSuccess: number }>;
      workload_id: string;
    };
    assert.match(comparison.workload_id, /^sha256:/);
    assert.equal(comparison.baselines.length, 4);
    assert.equal(comparison.baselines[0]?.strategy, "targeted_artifact_ref");
    assert.equal(comparison.baselines[0]?.taskSuccess, 0);
    assert.ok(comparison.baselines[0]!.messageCount < comparison.baselines[2]!.messageCount);

    const duplicate = spine.appendCanonical(draft("evt-a", "task:a"));
    assert.equal(duplicate.duplicate, true);
    assert.equal(spine.count(), 2);
    assert.equal(spine.metrics.collect().duplicateEventCount, 1);
  });
});

test("catalog version is authoritative before canonical commit", () => {
  withSpine((spine) => {
    assert.throws(() => spine.appendCanonical(draft("evt-version", "task:version", {
      eventVersion: 2,
    }), { route: false }), /event version does not match event catalog/);
    assert.equal(spine.count(), 0);
  }, { builtIns: false });
});

test("dynamic low-entropy baseline replays the same facts under four real policies", () => {
  const workerA = { kind: "worker" as const, id: "worker-a", requiredCapabilities: [] };
  const workerB = { kind: "worker" as const, id: "worker-b", requiredCapabilities: [] };
  const auditor = { kind: "auditor" as const, id: "auditor", requiredCapabilities: [] };
  const report = new LowEntropyBaselineHarness().run({
    workloadId: "workload:baseline",
    addressableRecipients: [workerA, workerB, auditor],
    staticRecipients: [workerA],
    taskTokenCount: 2000,
    verify: (deliveries) => {
      const delivered = deliveries.get("fact:1") ?? new Set<string>();
      return delivered.has("worker:worker-a") && delivered.has("worker:worker-b") ? 1 : 0;
    },
    facts: [{
      factId: "fact:1",
      sourceBytes: 8_000,
      inlineBytes: 80,
      offloadedBytes: 7_920,
      requiredRecipients: [workerA, workerB],
    }],
  });
  assert.equal(report.strategies.length, 4);
  const staticRoute = report.strategies.find((item) => item.strategy === "static_route")!;
  const broadcast = report.strategies.find((item) => item.strategy === "full_broadcast")!;
  const fullText = report.strategies.find((item) => item.strategy === "full_text_inline")!;
  assert.equal(report.primary.comparison.taskSuccess, 1);
  assert.equal(staticRoute.comparison.taskSuccess, 0);
  assert.equal(broadcast.comparison.taskSuccess, 1);
  assert.ok(report.primary.comparison.messageCount < broadcast.comparison.messageCount);
  assert.ok(report.primary.comparison.inlineTokenEstimate < fullText.comparison.inlineTokenEstimate);
  assert.ok(report.primary.comparison.duplicateFactRate < broadcast.comparison.duplicateFactRate);
  assert.equal(report.taskSuccessNotSignificantlyLower, true);
});

test("low entropy policy removes inline transcript and enforces byte budgets", () => {
  withSpine((spine) => {
    const receipt = spine.appendCanonical(draft("evt-large", "task:large", {
      eventType: "runtime.agent.message",
      sender: { kind: SenderKind.RUNTIME, id: "worker-runtime", capabilityRefs: [] },
      intent: "observation",
      summary: "🧭".repeat(800),
      inline: {
        full_transcript: `SECRET-${"x".repeat(5000)}`,
        content: "y".repeat(3000),
        status: "observed",
      },
    }), { route: false });
    const serializedInline = JSON.stringify(receipt.event.inline);
    assert.equal(serializedInline.includes("SECRET-"), false);
    assert.deepEqual(inlinePayloadContainsForbiddenContent(receipt.event.inline), []);
    assert.ok(receipt.event.artifactRefs.length >= 1);
    assert.ok(receipt.event.artifactRefs.every((ref) => ref.uri?.startsWith("artifact://")));
    assert.ok(byteLength(receipt.event.inline) <= 4 * 1024);
    assert.ok(Buffer.byteLength(receipt.event.summary, "utf8") <= 1024);
    assert.ok(Buffer.byteLength(JSON.stringify(receipt.event), "utf8") <= 8 * 1024);
    assert.equal(spine.artifacts.exists(receipt.event.artifactRefs[0]!), true);
  });
});

test("targeted delivery never exposes body to a non-target subscriber", () => {
  withSpine((spine) => {
    spine.registerSubscription(subscription("sub-a", "worker-a"));
    spine.registerSubscription(subscription("sub-b", "worker-b"));
    const receipt = spine.appendCanonical(draft("evt-target", "task:target", {
      target: { kind: "worker", id: "worker-a", requiredCapabilities: ["worker.execute"] },
    }));
    assert.deepEqual(receipt.route?.recipients.map((item) => item.id), ["worker-a"]);
    assert.equal(spine.poll("sub-b", 10).length, 0);
    const leased = spine.poll("sub-a", 1);
    assert.equal(leased.length, 1);
    assert.equal("inline" in leased[0]!.message, false);
    assert.equal(leased[0]!.message.eventDigest, receipt.event.contentDigest);
    const acknowledged = spine.acknowledge(
      leased[0]!.delivery.deliveryId,
      leased[0]!.delivery.leaseToken!,
    );
    assert.equal(acknowledged.state, "acknowledged");
    const duplicateAck = spine.acknowledge(
      leased[0]!.delivery.deliveryId,
      leased[0]!.delivery.leaseToken!,
    );
    assert.equal(duplicateAck.state, "acknowledged");
  }, { builtIns: false });
});

test("subscriber backpressure cannot roll back the canonical fact", () => {
  withSpine((spine) => {
    spine.registerSubscription(subscription("sub-tight", "worker-tight", 1));
    const target = { kind: "worker" as const, id: "worker-tight", requiredCapabilities: ["worker.execute"] };
    const first = spine.appendCanonical(draft("evt-tight-1", "task:tight", { target }));
    assert.equal(first.routed, true);
    const second = spine.appendCanonical(draft("evt-tight-2", "task:tight", {
      eventType: "runtime.task.updated",
      sender: { kind: SenderKind.RUNTIME, id: "task-runtime", capabilityRefs: [] },
      intent: "status",
      causationId: first.event.eventId,
      target,
    }));
    assert.equal(second.committed, true);
    assert.equal(second.routed, false);
    assert.ok(second.warnings.some((warning) => warning.startsWith("delivery_pending:")));
    assert.equal(spine.count({ aggregateId: "task:tight" }), 2);
  }, { builtIns: false });
});

test("durable catch-up repairs a missed delivery after canonical commit", () => {
  withSpine((spine) => {
    spine.registerSubscription(subscription("sub-catchup", "worker-catchup"));
    spine.appendCanonical(draft("evt-catchup", "task:catchup", {
      target: { kind: "worker", id: "worker-catchup", requiredCapabilities: ["worker.execute"] },
    }));
    spine.store.db.prepare("DELETE FROM runtime_event_deliveries WHERE event_id = ? AND subscription_id = ?")
      .run("evt-catchup", "sub-catchup");
    assert.equal(spine.poll("sub-catchup", 1).length, 0);
    assert.equal(spine.bus.catchUp("sub-catchup", { afterSequence: 0 }), 1);
    assert.equal(spine.bus.catchUp("sub-catchup", { afterSequence: 0 }), 0);
    assert.equal(spine.poll("sub-catchup", 1)[0]?.message.eventId, "evt-catchup");
  }, { builtIns: false });
});

test("projector failure is recoverable and does not erase canonical history", () => {
  withSpine((spine) => {
    const projector = spine.store.projector;
    const original = projector.apply.bind(projector);
    projector.apply = (() => { throw new Error("projector fault"); }) as typeof projector.apply;
    const receipt = spine.appendCanonical(draft("evt-projector", "task:projector"), { route: false });
    assert.equal(receipt.committed, true);
    assert.equal(receipt.projected, false);
    assert.ok(receipt.warnings.some((warning) => warning.startsWith("projector_pending:")));
    assert.equal(spine.get("evt-projector")?.eventId, "evt-projector");
    projector.apply = original;
    const rebuild = spine.rebuildProjection("task:projector");
    assert.equal(rebuild.event_count, 1);
    assert.equal((spine.projection("task:projector").session as { lastEventId: string }).lastEventId, "evt-projector");
  });
});

test("catalog authority and broadcast trust reject forged UI facts", () => {
  withSpine((spine) => {
    const root = spine.appendCanonical(draft("evt-authority-root", "task:authority"));
    const failed = draft("evt-authority-failed", "task:authority", {
      eventType: "runtime.task.failed",
      sender: { kind: SenderKind.API, id: "forged-ui", capabilityRefs: [] },
      intent: "recovery",
      causationId: root.event.eventId,
    });
    assert.throws(() => spine.appendCanonical(failed, { broadcast: true, fanoutReason: "test" }));
    assert.equal(spine.count({ aggregateId: "task:authority" }), 1);

    assert.throws(() => spine.appendCanonical({
      ...failed,
      sender: { kind: SenderKind.RUNTIME, id: "runtime", capabilityRefs: [] },
      provenance: { ...failed.provenance, trust: TrustLevel.EXTERNAL },
    }, { broadcast: true, fanoutReason: "test" }));
  });
});

test("router does not let recipient requirements or sender claims self-satisfy", () => {
  withSpine((spine) => {
    const event = spine.appendCanonical(draft("evt-router", "task:router"), { route: false }).event;
    const router = new AgentMessageRouter();
    const incapable = subscription("sub-incapable", "worker-incapable");
    const stripped = { ...incapable, capabilityRefs: [] };
    assert.throws(() => router.route({
      ...event,
      sender: { ...event.sender, capabilityRefs: ["worker.execute"] },
      target: stripped.recipient,
    }, {
      subscriptions: [stripped],
      pendingBySubscription: new Map(),
    }));
  }, { builtIns: false });
});

test("OMP frames preserve typed tool, partial/final, and subagent lifecycle", () => {
  const tool = normalizeOmpFrame({
    frame_type: "tool_execution_end",
    id: "omp-tool-result",
    parent_id: "omp-tool-call",
    request_id: "rpc-1",
    run_id: "run-1",
    task_id: "task-1",
    tool_call_id: "call-1",
    payload: { tool_name: "Read", result: "ok" },
  });
  assert.equal(tool.eventType, "runtime.tool.succeeded");
  assert.equal(tool.causationId, "omp-tool-call");
  assert.equal(tool.inline?.result_digest !== undefined, true);

  const partial = normalizeOmpFrame({
    frame_type: "message_delta",
    id: "omp-partial",
    request_id: "rpc-1",
    run_id: "run-1",
    task_id: "task-1",
    payload: { delta: "hi" },
  });
  assert.equal(partial.eventType, "runtime.text.delta");
  assert.equal(partial.durability, "live_only");

  const yielded = normalizeOmpFrame({
    frame_type: "subagent_yield",
    id: "omp-yield",
    parent_id: "omp-subagent-created",
    request_id: "rpc-1",
    run_id: "run-1",
    task_id: "task-1",
    payload: { artifact_id: "artifact-1" },
  });
  assert.equal(yielded.eventType, "runtime.subagent.yield");
  assert.equal(yielded.causationId, "omp-subagent-created");
});

test("malformed MCP result is folded to a typed fact without breaking causality", () => {
  withSpine((spine) => {
    const root = spine.appendCanonical(draft("evt-mcp-root", "run:run-test:task:mcp", {
      correlationId: "mcp-request-1",
    }), { route: false });
    const result = spine.appendLegacy({
      event_id: "evt-mcp-result",
      run_id: "run-test",
      task_id: "mcp",
      node_id: "root",
      event_type: "mcp_tool_result",
      created_at: "2026-07-19T00:00:01.000Z",
      payload: {
        request_id: "mcp-request-1",
        content: [{ type: "unexpected", value: 7 }],
        is_error: "not-a-boolean",
        raw_result: "must-not-be-an-inline-canonical-result",
      },
    }, { route: false });
    assert.equal(result.event.eventType, "runtime.mcp.tool.result");
    assert.equal(result.event.causationId, root.event.eventId);
    assert.equal("raw_result" in result.event.inline, false);
    assert.equal(result.event.provenance.normalizedFrom, "legacy-event-record");
  }, { builtIns: false });
});

test("legal control input becomes a new backend-owned canonical record", () => {
  withSpine((spine) => {
    const request = spine.appendLegacy({
      event_id: "evt-control-request",
      run_id: "run-control",
      task_id: "task-control",
      node_id: "root",
      event_type: "command_requested",
      created_at: "2026-07-19T00:00:00.000Z",
      payload: { command_id: "control-1", command: "pause" },
    }, { route: false });
    const accepted = spine.appendLegacy({
      event_id: "evt-control-accepted",
      run_id: "run-control",
      task_id: "task-control",
      node_id: "root",
      event_type: "command_validated",
      created_at: "2026-07-19T00:00:01.000Z",
      payload: { command_id: "control-1", decision: "accepted" },
    }, { route: false });
    assert.equal(request.event.eventType, "runtime.control.requested");
    assert.equal(accepted.event.eventType, "runtime.control.accepted");
    assert.equal(accepted.event.causationId, request.event.eventId);
    assert.deepEqual(
      spine.query({ aggregateId: request.event.aggregateId }).items.map((event) => event.eventId),
      [request.event.eventId, accepted.event.eventId],
    );
  }, { builtIns: false });
});

test("process-global legacy receipts receive explicit non-empty canonical identity", () => {
  withSpine((spine) => {
    const receipt = spine.appendLegacy({
      event_id: "evt-global-refresh",
      run_id: "",
      task_id: "",
      event_type: "command_registry_refreshed",
      created_at: "2026-07-19T00:00:00.000Z",
      payload: { state_owner: "ControlCommandRegistry" },
    });
    assert.equal(receipt.event.identity.runId, "runtime-global");
    assert.equal(receipt.event.identity.taskId, "runtime-global");
    assert.equal(receipt.route?.recipients[0]?.id, "api-projection");
  });
});

test("system failure wildcard reaches recovery while noncritical broadcast is denied", () => {
  withSpine((spine) => {
    const root = spine.appendCanonical(draft("evt-failure-root", "task:failure"));
    const receipt = spine.appendCanonical(draft("evt-failure", "task:failure", {
      eventType: "runtime.task.failed",
      sender: { kind: SenderKind.RUNTIME, id: "runtime", capabilityRefs: [] },
      intent: "recovery",
      causationId: root.event.eventId,
    }), { broadcast: true, fanoutReason: "task_terminal_failure" });
    assert.ok(receipt.route?.recipients.some((recipient) => recipient.id === "recovery-planner"));

    assert.throws(() => spine.appendCanonical(draft("evt-not-critical", "task:not-critical"), {
      broadcast: true,
      fanoutReason: "forbidden",
    }));
  });
});

test("store refuses caller-declared inline byte counts that hide oversized payloads", () => {
  withSpine((spine) => {
    const malicious = draft("evt-bypass", "task:bypass", {
      eventType: "runtime.agent.message",
      sender: { kind: SenderKind.RUNTIME, id: "runtime", capabilityRefs: [] },
      intent: "observation",
      inline: { note: "z".repeat(5000) },
    });
    assert.throws(() => spine.store.append({
      draft: malicious,
      inlineBytes: 1,
      estimatedEnvelopeBytes: 100,
      artifactSpillCount: 0,
      warnings: [],
      options: { route: false, project: false },
    }), (error: unknown) => error instanceof EventSpineError);
    assert.equal(spine.get("evt-bypass"), undefined);
  }, { builtIns: false });
});
