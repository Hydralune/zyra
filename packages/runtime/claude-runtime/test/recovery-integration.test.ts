import assert from "node:assert/strict";
import test from "node:test";

import {
  DurableBackgroundTaskReceiptRuntime,
  McpReconnectBreakerRuntime,
  OmpRecoveryIntegrationRuntime,
  PartialStreamContinuationRuntime,
  ProviderCredentialRotationRuntime,
  WorktreeRecoveryRuntime,
  ompIntegrationRuntimeContract,
} from "../src/recovery/omp-integration-runtime.ts";


test("provider credential rotation is deterministic supplementary evidence", () => {
  const runtime = new ProviderCredentialRotationRuntime();
  const input = {
    runId: "run-provider",
    taskId: "task-provider",
    requestId: "request-provider",
    responseId: "",
    currentProviderId: "provider-a",
    currentModelId: "model-a",
    currentCredentialId: "credential-a",
    currentRouteId: "route-a",
    failureClass: "credential_revoked" as const,
    statusCode: 401,
    retryAfterMs: 0,
    attempt: 3,
    requiredScopes: ["models:invoke"],
    candidates: [
      {
        providerId: "provider-b",
        modelId: "model-b",
        credentialId: "credential-b-v1",
        routeId: "route-b-v1",
        credentialVersion: 1,
        available: true,
        cooldownUntil: "",
        scopes: ["models:invoke"],
      },
      {
        providerId: "provider-b",
        modelId: "model-b",
        credentialId: "credential-b-v2",
        routeId: "route-b-v2",
        credentialVersion: 2,
        available: true,
        cooldownUntil: "",
        scopes: ["models:invoke"],
      },
      {
        providerId: "provider-c",
        modelId: "model-c",
        credentialId: "credential-c",
        routeId: "route-c",
        credentialVersion: 9,
        available: false,
        cooldownUntil: "",
        scopes: ["models:invoke"],
      },
    ],
    idempotencyKey: "provider-rotation-once",
  };
  const receipt = runtime.observe(input);
  const replay = runtime.observe(input);
  assert.equal(receipt.selected_candidate?.credential_id, "credential-b-v2");
  assert.equal(receipt.route_mutation_applied, false);
  assert.equal(receipt.canonical_route_owner, "python.ProviderControlPlane");
  assert.equal(receipt.retry_eligible, false);
  assert.deepEqual(receipt.excluded_route_ids.sort(), ["route-a", "route-c"]);
  assert.equal(replay.receipt_id, receipt.receipt_id);
  assert.equal(replay.replayed, true);
});

test("partial stream receipt skips committed tool calls and rejects response replay", () => {
  const runtime = new PartialStreamContinuationRuntime();
  const input = {
    runId: "run-stream",
    taskId: "task-stream",
    sessionId: "session-stream",
    turnId: "turn-stream",
    requestId: "request-stream",
    responseId: "response-stream",
    toolCallIds: ["tool-committed", "tool-pending"],
    committedToolCallIds: ["tool-committed"],
    emittedContentDigest: "sha256:partial-stream",
    checkpointId: "checkpoint-stream",
    sideEffectFenceKeys: ["fence-tool-committed"],
    sequence: 12,
    idempotencyKey: "partial-stream-once",
  };
  const receipt = runtime.observe(input);
  const replay = runtime.observe(input);
  assert.deepEqual(receipt.skipped_tool_call_ids, ["tool-committed"]);
  assert.deepEqual(receipt.replayable_tool_call_ids, ["tool-pending"]);
  assert.equal(receipt.blind_retry_allowed, false);
  assert.equal(receipt.exact_resume_required, true);
  assert.equal(replay.replayed, true);
  assert.throws(
    () => runtime.observe({ ...input, idempotencyKey: "different-key" }),
    /already processed/,
  );
});

test("MCP reconnect breaker opens at budget and never selects canonical action", () => {
  const runtime = new McpReconnectBreakerRuntime();
  const base = {
    runId: "run-mcp",
    taskId: "task-mcp",
    sessionId: "session-mcp",
    serverId: "server-mcp",
    transportId: "transport-mcp",
    failureCode: "connection_reset",
    authRequired: false,
    retryable: true,
    maximumAttempts: 2,
    cooldownMs: 10_000,
  };
  const first = runtime.observe({
    ...base,
    requestId: "request-mcp-1",
    attempt: 1,
    idempotencyKey: "mcp-failure-1",
  });
  const second = runtime.observe({
    ...base,
    requestId: "request-mcp-2",
    attempt: 2,
    idempotencyKey: "mcp-failure-2",
  });
  assert.equal(first.reconnect_eligible, true);
  assert.equal(first.action_hint, "retry");
  assert.equal(second.state, "open");
  assert.equal(second.reconnect_eligible, false);
  assert.equal(second.action_hint, "replan");
  assert.equal(second.applied_action_selected_here, false);
  runtime.success("task-mcp", "server-mcp", "transport-mcp");
  assert.match(JSON.stringify(runtime.snapshot()), /closed/);
});

test("worktree conflict preserves WIP and forbids destructive cleanup", () => {
  const runtime = new WorktreeRecoveryRuntime();
  const receipt = runtime.observe({
    runId: "run-worktree",
    taskId: "task-worktree",
    subagentId: "subagent-worktree",
    worktreeId: "worktree-1",
    baseRevision: "commit-base",
    headRevision: "commit-head",
    targetRevision: "commit-target",
    changedPaths: ["src/runtime.ts", "tests/runtime.test.ts"],
    dirtyPaths: ["src/runtime.ts"],
    conflictPaths: ["src/runtime.ts"],
    untrackedPaths: ["notes/recovery.txt"],
    stashRef: "stash-recovery-1",
    idempotencyKey: "worktree-conflict-once",
  });
  assert.equal(receipt.state, "conflicted");
  assert.equal(receipt.merge_allowed, false);
  assert.equal(receipt.destructive_cleanup_allowed, false);
  assert.deepEqual(receipt.conflict_paths, ["src/runtime.ts"]);
  assert.throws(
    () => runtime.observe({
      runId: "run-worktree",
      taskId: "task-worktree",
      subagentId: "subagent-worktree",
      worktreeId: "worktree-escape",
      baseRevision: "commit-base",
      headRevision: "commit-head",
      targetRevision: "commit-target",
      changedPaths: ["../escape"],
      dirtyPaths: [],
      conflictPaths: [],
      untrackedPaths: [],
      stashRef: "",
      idempotencyKey: "worktree-escape",
    }),
    /(unsafe segment|missing or invalid)/,
  );
});

test("background restart workset preserves latest durable generation", () => {
  const runtime = new DurableBackgroundTaskReceiptRuntime();
  const base = {
    runId: "run-background",
    taskId: "task-background",
    jobId: "job-background",
    issueId: "issue-background",
    attemptId: "attempt-background-1",
    workerId: "worker-background",
    workerLeaseId: "lease-background",
    checkpointId: "checkpoint-background",
    generation: 1,
    state: "running" as const,
    resultRef: "",
    errorCode: "",
    heartbeatAt: "2026-07-22T00:00:00.000Z",
    idempotencyKey: "background-running-1",
  };
  const running = runtime.observe(base);
  assert.equal(running.disposition, "resume_checkpoint");
  const workset = runtime.restartWorkset("run-background", "task-background");
  const tasks = workset.tasks as unknown as Array<Record<string, unknown>>;
  assert.equal(tasks.length, 1);
  assert.equal(tasks[0]?.job_id, "job-background");
  assert.equal(workset.mutable_task_state_copied, false);
  assert.throws(
    () => runtime.observe({
      ...base,
      generation: 0,
      idempotencyKey: "background-generation-regressed",
    }),
    /generation regressed/,
  );
});

test("combined OMP evidence stays supplementary and names Python owners", () => {
  const runtime = new OmpRecoveryIntegrationRuntime();
  const evidence = runtime.evidence({
    background: {
      runId: "run-combined",
      taskId: "task-combined",
      jobId: "job-combined",
      issueId: "issue-combined",
      attemptId: "attempt-combined",
      workerId: "",
      workerLeaseId: "",
      checkpointId: "checkpoint-combined",
      generation: 2,
      state: "failed",
      resultRef: "",
      errorCode: "worker_lost",
      heartbeatAt: "2026-07-22T00:00:00.000Z",
      idempotencyKey: "combined-background",
    },
  });
  assert.equal(evidence.supplementary_only, true);
  assert.equal(evidence.canonical_policy_owner, "python.RecoveryDecisionRuntime");
  assert.equal(evidence.applied_action_selected_here, false);
  const contract = ompIntegrationRuntimeContract();
  assert.equal(contract.canonical_checkpoint_owner, "python.RecoveryPlanStore");
  assert.equal(contract.langgraph_runtime_dependency, false);
});
