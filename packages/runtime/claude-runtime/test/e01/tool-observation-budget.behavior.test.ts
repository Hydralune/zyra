import assert from "node:assert/strict";
import { test } from "bun:test";

import { ModelIterationRuntime, ToolObservationBudgetRuntime } from "../../src/index.ts";

test("e01.mutation.observation-budget-enforces-cross-result-limit", () => {
  const runtime = new ToolObservationBudgetRuntime({
    maxRoundChars: 700,
    maxObservationChars: 420,
    replacementPreviewChars: 48,
    keepRecentObservations: 1,
    minimumReplacementSavings: 32,
    errorReserveChars: 0,
  });
  runtime.register({ callId: "call-oldest", roundId: "round-7", toolName: "search", content: "alpha ".repeat(46) });
  runtime.register({
    callId: "call-middle",
    roundId: "round-7",
    toolName: "read",
    content: "beta ".repeat(50),
    artifactRefs: ["artifact://result/2"],
  });
  runtime.register({ callId: "call-newest", roundId: "round-7", toolName: "verify", content: "gamma ".repeat(25) });

  const plan = runtime.enforceRound("round-7");
  assert.ok(plan.originalChars > plan.limitChars);
  assert.ok(plan.renderedChars <= plan.limitChars);
  assert.ok(plan.replacementCount >= 1);
  assert.equal(runtime.decisionFor("call-newest")?.kind, "preserve");
  assert.match(String(runtime.contentFor("call-middle")), /tool_observation_omitted/);
  assert.deepEqual(runtime.enforceRound("round-7"), plan);
  assert.equal(runtime.project().lastPlanDigest, plan.digest);
});

test("e01.mutation.observation-budget-restore-rejects-tampering", () => {
  const runtime = new ToolObservationBudgetRuntime({
    maxRoundChars: 600,
    maxObservationChars: 300,
    replacementPreviewChars: 40,
  });
  runtime.register({
    callId: "call-image",
    roundId: "round-restore",
    toolName: "browser",
    content: [
      { type: "text", text: "visual result" },
      { type: "image", source: { media_type: "image/png", data: "abc" } },
    ],
    artifactRefs: ["artifact://image/1"],
  });
  runtime.enforceRound("round-restore");
  const snapshot = runtime.snapshot();
  const restored = new ToolObservationBudgetRuntime();
  restored.restore(snapshot);
  assert.deepEqual(restored.snapshot(), snapshot);
  assert.equal(restored.candidate("call-image")?.hasImage, true);
  const tampered = {
    ...snapshot,
    records: snapshot.records.map((record) =>
      record.callId === "call-image" ? { ...record, content: "changed" } : record,
    ),
  };
  assert.throws(() => restored.restore(tampered), /snapshot checksum mismatch/);
});

test("e01.integration.model-iteration-applies-observation-budget", () => {
  const iteration = new ModelIterationRuntime({
    sessionId: "budget-session",
    runId: "budget-run-1",
    taskId: "budget-task",
    workerRequestId: "budget-worker-request",
  });
  iteration.start([{ role: "user", content: "Read the large result and continue." }]);
  const round = iteration.beginProviderRound({
    messages: iteration.currentMessages(),
    model: "test-model",
    requestKey: "budget-provider-round",
  });
  iteration.acceptProviderResult({
    roundId: round.roundId,
    providerRequestId: "provider-budget-response",
    model: "test-model",
    stopReason: "tool_use",
    finalText: "",
    steps: [
      {
        step_id: "large-tool-call",
        tool_name: "read",
        arguments: { path: "large.txt" },
        metadata: {},
      },
    ],
  });
  iteration.recordToolObservation({
    callId: "large-tool-call",
    turnId: "turn-budget",
    ok: true,
    summary: "large read completed",
    output: { text: "provider-visible-result ".repeat(1_000) },
    error: null,
  });

  const revised = iteration.buildRevisionMessages(round.roundId);
  const observation = revised.at(-1) as {
    content: Array<{ content: string }>;
    metadata: { observation_budget_plan_digest: string };
  };
  assert.match(observation.content[0]?.content ?? "", /tool_observation_omitted/);
  assert.match(observation.metadata.observation_budget_plan_digest, /^[0-9a-f]{64}$/);
  const snapshot = iteration.snapshot();
  assert.equal(snapshot.toolObservationBudget.plans.length, 1);
  assert.equal(snapshot.toolObservationBudget.records[0]?.callId, "large-tool-call");

  const restored = new ModelIterationRuntime({
    sessionId: "budget-session",
    runId: "budget-run-2",
    taskId: "budget-task",
    workerRequestId: "budget-worker-request",
  });
  restored.restore(snapshot, true);
  assert.equal(restored.snapshot().toolObservationBudget.plans[0]?.digest, snapshot.toolObservationBudget.plans[0]?.digest);
});
