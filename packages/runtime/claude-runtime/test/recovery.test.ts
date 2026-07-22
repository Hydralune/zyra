import assert from "node:assert/strict";
import test from "node:test";

import {
  DeterministicRetryBudgetRuntime,
  OmpRecoveryReceiptRuntime,
  ResponseReplayFence,
  ompRecoveryRuntimeContract,
} from "../src/recovery/omp-recovery-runtime.ts";
import {
  AppendOnlySessionContinuityRuntime,
  TaskTerminalReceiptRuntime,
  WorktreeMergeReceiptRuntime,
} from "../src/recovery/continuity-runtime.ts";
import type { OmpFaultInput, OmpRecoveryRefs } from "../src/recovery/omp-recovery-runtime.ts";


function refs(overrides: Partial<OmpRecoveryRefs> = {}): OmpRecoveryRefs {
  return {
    runId: "run-omp-1",
    taskId: "task-omp-1",
    sessionId: "session-omp-1",
    requestId: "request-omp-1",
    responseId: "",
    attemptId: "attempt-omp-1",
    workerId: "worker-omp-1",
    backendId: "backend-omp-1",
    providerId: "provider-omp-1",
    modelId: "model-omp-1",
    mcpServerId: "mcp-omp-1",
    toolCallId: "tool-call-omp-1",
    ...overrides,
  };
}

function fault(overrides: Partial<OmpFaultInput> = {}): OmpFaultInput {
  return {
    kind: "model_rate_limit",
    retryable: true,
    terminal: false,
    statusCode: 429,
    observedCode: "rate_limited",
    errorType: "ProviderError",
    refs: refs(),
    details: { retry_after_ms: 900 },
    observedAt: "2026-07-22T00:00:00.000Z",
    ...overrides,
  };
}

test("OMP receipt is deterministic supplementary evidence with bounded retry history", () => {
  const runtime = new OmpRecoveryReceiptRuntime({
    retry: { maxAttempts: 2, baseDelayMs: 100, maximumDelayMs: 2_000, jitterRatio: 0 },
  });
  const first = runtime.receipt(fault());
  const replay = runtime.receipt(fault());

  assert.equal(first.receipt_id, replay.receipt_id);
  assert.equal(first.signal_kind, "provider_rate_limit");
  assert.equal(first.retry.eligible, true);
  assert.equal(first.retry.delay_ms, 900);
  assert.equal(first.provenance.canonical_policy_owner, "python.RecoveryDecisionRuntime");
  assert.equal(first.provenance.applied_action_selected_here, false);
  assert.equal(first.routing.lease_owner, "ProviderControlPlane");

  const scheduled = runtime.schedule(first);
  assert.equal(scheduled.attempt, 1);
  assert.equal(scheduled.delayMs, 900);
  runtime.settle(first, scheduled.attempt, "failed");
  const secondReceipt = runtime.receipt(fault());
  assert.equal(secondReceipt.retry.attempt, 1);
  const second = runtime.schedule(secondReceipt);
  runtime.settle(secondReceipt, second.attempt, "failed");
  assert.equal(runtime.receipt(fault()).retry.eligible, false);
  const snapshot = runtime.snapshot();
  assert.equal(snapshot.supplementary_only, true);
  assert.equal((snapshot.continuity as Record<string, unknown>).supplementary_only, true);
});

test("response and side-effect fences make unsafe retries ineligible", () => {
  const fence = new ResponseReplayFence();
  const identity = refs({ responseId: "response-omp-1" });
  const responseKey = fence.responseKey(identity);
  const sideEffectKey = fence.sideEffectKey(identity, { side_effect_key: "effect-omp-1" });
  assert.equal(fence.markResponse(responseKey, "response-receipt-1"), true);
  assert.equal(fence.markResponse(responseKey, "response-receipt-1"), false);
  assert.equal(fence.markSideEffect(sideEffectKey, "effect-receipt-1"), true);
  assert.equal(fence.markSideEffect(sideEffectKey, "effect-receipt-1"), false);

  const runtime = new OmpRecoveryReceiptRuntime();
  runtime.replay.markSideEffect("effect-omp-1", "effect-receipt-1");
  const receipt = runtime.receipt(fault({
    refs: identity,
    details: {
      partial_output: true,
      observable_side_effect: true,
      side_effect_key: "effect-omp-1",
    },
  }));
  assert.equal(receipt.replay_fence.retry_safe, false);
  assert.equal(receipt.retry.eligible, false);
  assert.equal(receipt.action_candidates.find((item) => item.action === "retry")?.eligible, false);
});

test("retry budget and ownership contract fail closed", () => {
  const budget = new DeterministicRetryBudgetRuntime({ maxAttempts: 1, jitterRatio: 0 });
  const scheduled = budget.schedule("request-key", "tool_timeout", "provider-a");
  budget.settle("request-key", scheduled.attempt, "succeeded", "response-a");
  assert.throws(() => budget.schedule("request-key", "tool_timeout", "provider-a"), /exhausted/);

  const contract = ompRecoveryRuntimeContract();
  assert.equal((contract.source as Record<string, unknown>).role, "supplementary");
  assert.equal(contract.receipt_consumer, "python.RecoverySignalClassifier.from_omp_receipt");
});

test("append-only session continuity preserves lineage and bypasses processed events", () => {
  const sessions = new AppendOnlySessionContinuityRuntime();
  const parent = sessions.commit({
    runId: "run-continuity-1",
    taskId: "task-continuity-1",
    sessionId: "session-parent-1",
    parentSessionId: "",
    checkpointId: "checkpoint-parent-1",
    expectedRevision: 0,
    entries: [
      { eventId: "event-1", sequence: 1, kind: "assistant", payloadDigest: "sha256:event-1" },
      { eventId: "event-2", sequence: 2, kind: "tool", payloadDigest: "sha256:event-2" },
    ],
    idempotencyKey: "append-parent-1",
  });
  assert.equal(parent.revision, 1);
  const replay = sessions.commit({
    runId: "run-continuity-1",
    taskId: "task-continuity-1",
    sessionId: "session-parent-1",
    parentSessionId: "",
    checkpointId: "checkpoint-parent-1",
    expectedRevision: 0,
    entries: [
      { eventId: "event-1", sequence: 1, kind: "assistant", payloadDigest: "sha256:event-1" },
      { eventId: "event-2", sequence: 2, kind: "tool", payloadDigest: "sha256:event-2" },
    ],
    idempotencyKey: "append-parent-1",
  });
  assert.equal(replay.receipt_id, parent.receipt_id);
  assert.equal(replay.replayed, true);

  const resumed = sessions.resume("session-parent-1", ["event-1"]);
  assert.deepEqual(resumed.bypassed_event_ids, ["event-1"]);
  assert.equal((resumed.pending_entries as unknown[]).length, 1);
  assert.equal(resumed.processed_response_replay, false);

  const child = sessions.fork("session-parent-1", {
    runId: "run-continuity-1",
    taskId: "task-continuity-1",
    sessionId: "session-child-1",
    parentSessionId: "session-parent-1",
    checkpointId: "checkpoint-child-1",
    expectedRevision: 0,
    entries: [
      { eventId: "event-child-1", sequence: 1, kind: "assistant", payloadDigest: "sha256:event-child-1" },
    ],
    idempotencyKey: "append-child-1",
  });
  assert.deepEqual(child.lineage, ["session-parent-1", "session-child-1"]);
  assert.throws(() => sessions.commit({
    runId: "run-continuity-1",
    taskId: "task-continuity-1",
    sessionId: "session-parent-1",
    parentSessionId: "",
    checkpointId: "checkpoint-parent-1",
    expectedRevision: 0,
    entries: [
      { eventId: "event-3", sequence: 3, kind: "assistant", payloadDigest: "sha256:event-3" },
    ],
    idempotencyKey: "append-parent-stale",
  }), /revision changed/);
});

test("task terminal and worktree merge receipts are idempotent and fenced", () => {
  const tasks = new TaskTerminalReceiptRuntime();
  const terminalRequest = {
    runId: "run-terminal-1",
    taskId: "task-terminal-1",
    jobId: "job-terminal-1",
    attemptId: "attempt-terminal-1",
    generation: 2,
    state: "succeeded" as const,
    resultRef: "artifact-result-1",
    errorCode: "",
    idempotencyKey: "terminal-once",
  };
  const terminal = tasks.settle(terminalRequest);
  const terminalReplay = tasks.settle(terminalRequest);
  assert.equal(terminalReplay.receipt_id, terminal.receipt_id);
  assert.equal(terminalReplay.replayed, true);
  assert.throws(() => tasks.settle({ ...terminalRequest, resultRef: "artifact-different" }), /different content/);

  const worktrees = new WorktreeMergeReceiptRuntime();
  const mergeRequest = {
    runId: "run-merge-1",
    taskId: "task-merge-1",
    worktreeId: "worktree-merge-1",
    baseRevision: "revision-base",
    headRevision: "revision-head",
    targetRevision: "revision-target",
    changedPaths: ["src/runtime.ts", "src/runtime.ts"],
    conflictPaths: [] as string[],
    idempotencyKey: "merge-once",
  };
  const merged = worktrees.settle(mergeRequest);
  assert.equal(merged.state, "committed");
  assert.deepEqual(merged.changed_paths, ["src/runtime.ts"]);
  assert.equal(worktrees.settle(mergeRequest).replayed, true);
  const conflicted = worktrees.settle({
    ...mergeRequest,
    worktreeId: "worktree-merge-2",
    conflictPaths: ["src/conflict.ts"],
    idempotencyKey: "merge-conflict-once",
  });
  assert.equal(conflicted.state, "conflicted");
  assert.throws(() => worktrees.settle({
    ...mergeRequest,
    worktreeId: "worktree-unsafe",
    changedPaths: ["../outside.ts"],
    idempotencyKey: "merge-unsafe",
  }), /invalid|unsafe segment/);
});
