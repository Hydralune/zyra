import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  ConsumerRole,
  DeliveryMode,
  RuntimeEventIntegration,
  RuntimeEventSpine,
  SourceDomain,
  SourceRecordKind,
  type RuntimeIngressContext,
} from "../src/index.ts";

async function withIntegration(
  body: (
    spine: RuntimeEventSpine,
    integration: RuntimeEventIntegration,
    root: string,
  ) => void | Promise<void>,
  options: { enableMessageBus?: boolean; enableProjector?: boolean } = {},
): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), "zyra-event-integration-"));
  const spine = new RuntimeEventSpine({
    sqlitePath: join(root, "events.sqlite3"),
    artifactRoot: join(root, "artifacts"),
    registerBuiltIns: true,
    enableMessageBus: options.enableMessageBus,
    enableProjector: options.enableProjector,
  });
  const integration = new RuntimeEventIntegration(spine);
  try {
    await body(spine, integration, root);
  } finally {
    spine.close();
    rmSync(root, { recursive: true, force: true });
  }
}

function context(
  producerSequence: number,
  overrides: Partial<RuntimeIngressContext> = {},
): RuntimeIngressContext {
  return {
    runId: "run-integration",
    sessionId: "session-integration",
    taskId: "task-integration",
    nodeId: "node-integration",
    workerId: "codeworker",
    correlationId: "request-integration",
    producerSequence,
    ownerId: "CodeWorkerRuntime",
    occurredAt: `2026-07-20T00:00:${String(producerSequence).padStart(2, "0")}.000Z`,
    ...overrides,
  };
}

test("source admission drives canonical store, durable projector, and worker consumer", async () => {
  await withIntegration(async (spine, integration) => {
    const admitted = integration.appendRuntimeRecord(
      SourceRecordKind.QUERY_ADMITTED,
      SourceDomain.CODEWORKER,
      context(1),
      { query_id: "query-1", delivery: "immediate", status: "admitted" },
      { summary: "query admitted to the CodeWorker runtime" },
    );

    assert.equal(admitted.committed, true);
    assert.equal(admitted.eventType, "runtime.query.admitted");
    assert.equal(admitted.projected, true);
    assert.equal(spine.count(), 1);
    assert.equal(spine.projectionDelivery?.globalCursor().globalSequence, 1);

    const view = spine.projectionDelivery!.taskView("run:run-integration:task:task-integration");
    assert.equal(view?.aggregateId, "run:run-integration:task:task-integration");
    assert.equal(view?.lastEventId, admitted.eventId);
    assert.equal(view?.phase, "query");

    const reports = await integration.pumpConsumers();
    const worker = reports.find((report) => report.role === ConsumerRole.WORKER)!;
    assert.equal(worker.acknowledged, 1);
    assert.equal(worker.checkpoint.lastEventId, admitted.eventId);
    const mutation = integration.consumers.readMutation(
      "integration-worker",
      "consumer/worker/session",
      "run:run-integration:task:task-integration",
    );
    assert.equal(mutation?.source_event_id, admitted.eventId);
    assert.equal(integration.domainConsumers.audit().passed, true);
  });
});

test("duplicate source retry repairs a missing delivery without duplicating the fact", async () => {
  await withIntegration((spine, integration) => {
    const sourceContext = context(1);
    const first = integration.appendRuntimeRecord(
      SourceRecordKind.QUERY_ADMITTED,
      SourceDomain.CODEWORKER,
      sourceContext,
      { query_id: "query-repair", delivery: "immediate" },
    );
    const delivery = spine.store.deliveriesForEvent(first.eventId!)
      .find((item) => item.subscriptionId === "sub:integration-worker");
    assert.ok(delivery);
    spine.store.db.prepare("DELETE FROM runtime_event_deliveries WHERE delivery_id = ?").run(delivery.deliveryId);
    assert.equal(
      spine.store.deliveriesForEvent(first.eventId!)
        .some((item) => item.subscriptionId === "sub:integration-worker"),
      false,
    );

    const retry = integration.appendRuntimeRecord(
      SourceRecordKind.QUERY_ADMITTED,
      SourceDomain.CODEWORKER,
      sourceContext,
      { query_id: "query-repair", delivery: "immediate" },
    );
    assert.equal(retry.duplicate, true);
    assert.equal(spine.count(), 1);
    assert.ok(retry.warnings.includes("duplicate_delivery_gap_repaired"));
    assert.equal(
      spine.store.deliveriesForEvent(first.eventId!)
        .some((item) => item.subscriptionId === "sub:integration-worker"),
      true,
    );
  });
});

test("tool source payload is externalized and artifact reads verify canonical pointer", async () => {
  await withIntegration((spine, integration) => {
    const called = integration.appendRuntimeRecord(
      SourceRecordKind.TOOL_CALLED,
      SourceDomain.CODEWORKER,
      context(1),
      {
        tool_name: "shell",
        tool_call_id: "tool-1",
        arguments: { command: "build" },
        started: true,
      },
    );
    const output = `build-output:${"x".repeat(16_000)}`;
    const succeeded = integration.appendRuntimeRecord(
      SourceRecordKind.TOOL_SUCCEEDED,
      SourceDomain.CODEWORKER,
      context(2, { causationEventId: called.eventId }),
      {
        tool_name: "shell",
        tool_call_id: "tool-1",
        stdout: output,
        result_digest: `sha256:${"b".repeat(64)}`,
      },
    );
    const event = spine.get(succeeded.eventId!)!;
    assert.equal(event.eventType, "runtime.tool.succeeded");
    assert.ok(event.artifactRefs.length >= 1);
    assert.equal(Object.hasOwn(event.inline, "stdout"), false);

    const pointer = event.artifactRefs.find((item) => item.mediaType.startsWith("text/"))
      ?? event.artifactRefs[0]!;
    const read = integration.artifacts.read({
      artifactId: pointer.artifactId,
      expectedDigest: pointer.digest,
      encoding: "utf8",
    });
    assert.equal(read.verified, true);
    assert.ok(read.totalBytes >= Buffer.byteLength(output, "utf8"));
    assert.match(read.data, /build-output:/);
    const audit = integration.auditArtifact(pointer.artifactId);
    assert.equal(audit.readableAfterRestart, true);
    assert.deepEqual(audit.findings, []);
  });
});

test("projector rebuild is equivalent to online bus delivery and preserves API history", async () => {
  await withIntegration((spine, integration) => {
    const started = integration.appendRuntimeRecord(
      SourceRecordKind.TURN_STARTED,
      SourceDomain.CODEWORKER,
      context(1),
      { turn_id: "turn-1", phase: "reason", status: "started" },
    );
    integration.appendRuntimeRecord(
      SourceRecordKind.TURN_COMPLETED,
      SourceDomain.CODEWORKER,
      context(2, { causationEventId: started.eventId }),
      { turn_id: "turn-1", phase: "observe", status: "completed" },
    );
    const aggregateId = "run:run-integration:task:task-integration";
    const before = spine.projectionDelivery!.history(aggregateId, { limit: 50 });
    assert.equal(before.items.length, 2);

    const rebuild = spine.projectionDelivery!.rebuild(aggregateId);
    assert.equal(rebuild.equivalent, true);
    assert.equal(rebuild.event_count, 2);
    const after = spine.projectionDelivery!.history(aggregateId, { limit: 50 });
    assert.deepEqual(
      after.items.map((item) => item.eventId),
      before.items.map((item) => item.eventId),
    );

    const reconciliation = integration.reconcile({
      aggregateId,
      repairDeliveries: true,
      repairProjection: true,
      rebuildProjection: true,
      verifyArtifacts: true,
    });
    assert.equal(reconciliation.equivalentAfterRepair, true);
    assert.equal(
      reconciliation.findings.filter((item) => item.severity === "error").length,
      0,
    );

    // A missing derivative view must not be hidden by an already-advanced
    // global cursor. Reconciliation repairs from canonical history only.
    spine.store.db.prepare(
      "DELETE FROM runtime_projected_task_views WHERE aggregate_id = ?",
    ).run(aggregateId);
    assert.equal(spine.projectionDelivery!.coverage(aggregateId).complete, false);
    const repaired = integration.reconcile({ aggregateId, repairProjection: true });
    assert.equal(repaired.equivalentAfterRepair, true);
    assert.equal(
      repaired.findings.some((item) => item.code === "projection_coverage_rebuilt" && item.repaired),
      true,
    );
    assert.equal(spine.projectionDelivery!.coverage(aggregateId).complete, true);
    assert.equal(spine.projectionDelivery!.taskView(aggregateId)?.lastEventId, after.items.at(-1)?.eventId);
  });
});

test("OMP RPC host tool frames preserve durable call, progress, and final correlation", async () => {
  await withIntegration((spine, integration) => {
    const ompContext = (producerSequence: number) => ({
      identity: {
        runId: "run-integration",
        sessionId: "session-integration",
        taskId: "task-integration",
        nodeId: "node-integration",
        workerId: "codeworker",
      },
      requestId: "omp-request-1",
      producerSequence,
      receivedAt: `2026-07-20T00:01:${String(producerSequence).padStart(2, "0")}.000Z`,
    });
    const called = integration.appendOmpRpcFrame({
      type: "host_tool_call",
      payload: {
        id: "rpc-1",
        tool_call_id: "tool-omp-1",
        tool_name: "browser",
        arguments: { url: "https://example.test" },
      },
    }, ompContext(1));
    const progress = integration.appendOmpRpcFrame({
      type: "host_tool_update",
      payload: { id: "rpc-1", partial_result: { phase: "navigating" } },
    }, ompContext(2));
    const final = integration.appendOmpRpcFrame({
      type: "host_tool_result",
      payload: { id: "rpc-1", result: { title: "Example" } },
    }, ompContext(3));

    assert.deepEqual(
      [called.results[0]?.eventType, progress.results[0]?.eventType, final.results[0]?.eventType],
      ["runtime.tool.called", "runtime.tool.progress", "runtime.tool.succeeded"],
    );
    const finalEvent = spine.get(final.results[0]!.eventId!)!;
    assert.equal(finalEvent.causationId, called.results[0]!.eventId);
    assert.equal(spine.projection(finalEvent.aggregateId).tool_pair_audit instanceof Object, true);

    const deferred = integration.appendOmpRpcFrame({
      type: "host_tool_cancel",
      payload: { id: "rpc-1", started: true, phase: "in_flight" },
    }, ompContext(4));
    assert.equal(deferred.results[0]?.eventType, "runtime.recovery.requested");
    assert.equal(spine.get(deferred.results[0]!.eventId!)?.inline.strategy, "await_real_tool_result");
  });
});

test("bus and projector disable observations are explicit and have no fallback", async () => {
  await withIntegration((spine, integration) => {
    const matrix = integration.evaluateDisableMatrix("run:run-integration:task:task-integration", [
      {
        component: "event_store",
        canonicalWrite: false,
        liveDelivery: false,
        replay: false,
        queryView: false,
        typedFrameAdmission: false,
        observedFailure: "store unavailable",
      },
      {
        component: "message_bus",
        canonicalWrite: true,
        liveDelivery: false,
        replay: true,
        queryView: false,
        typedFrameAdmission: true,
        observedFailure: "delivery stalled",
      },
      {
        component: "projector",
        canonicalWrite: true,
        liveDelivery: true,
        replay: true,
        queryView: false,
        typedFrameAdmission: true,
        observedFailure: "view unavailable",
      },
      {
        component: "omp_mapper",
        canonicalWrite: true,
        liveDelivery: true,
        replay: true,
        queryView: true,
        typedFrameAdmission: false,
        observedFailure: "typed frame rejected",
      },
    ]);
    assert.equal(matrix.passed, true);
    assert.equal(integration.contract().api_reads_projector_only, true);
    assert.equal(integration.contract().raw_rpc_forwarded, false);
    assert.equal(spine.count(), 0);
  });

  await withIntegration((spine, integration) => {
    const admitted = integration.appendRuntimeRecord(
      SourceRecordKind.QUERY_ADMITTED,
      SourceDomain.CODEWORKER,
      context(1),
      { query_id: "store-only", delivery: "immediate" },
      { deliveryMode: DeliveryMode.STORE_ONLY },
    );
    assert.equal(admitted.committed, true);
    assert.equal(admitted.deliveryIds.length, 0);
    assert.equal(spine.count(), 1);
    assert.equal(spine.projectionDelivery, undefined);
    assert.equal(
      spine.projection("run:run-integration:task:task-integration").task_view,
      null,
    );
  }, { enableMessageBus: false, enableProjector: true });
});
