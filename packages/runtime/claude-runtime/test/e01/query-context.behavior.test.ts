import { describe, expect, test } from "bun:test";

import { CompactSummaryRuntime } from "../../src/compact/summary-runtime.ts";
import { ContextCacheRuntime } from "../../src/context/cache-runtime.ts";
import {
  DeterministicIdFactory,
  ManualClock,
  type JsonRecord,
  type JsonValue,
} from "../../src/core/runtime-primitives.ts";
import { QueryExecutionPlanRuntime } from "../../src/query/execution-plan-runtime.ts";
import { QueryHookRuntime } from "../../src/query/hook-runtime.ts";
import { QueryStopRuntime } from "../../src/query/stop-runtime.ts";

function ids(seed: string, sequence = 0): DeterministicIdFactory {
  return new DeterministicIdFactory(seed, sequence);
}

function executionBudget(overrides: Partial<{
  maximumTurns: number;
  maximumToolCalls: number;
  maximumReasoningTokens: number;
  maximumOutputTokens: number;
  maximumWallMilliseconds: number;
  maximumConsecutiveToolFailures: number;
}> = {}) {
  return {
    maximumTurns: 8,
    maximumToolCalls: 8,
    maximumReasoningTokens: 8_000,
    maximumOutputTokens: 4_000,
    maximumWallMilliseconds: 60_000,
    maximumConsecutiveToolFailures: 3,
    ...overrides,
  };
}

function createPlan(
  runtime: QueryExecutionPlanRuntime,
  overrides: Partial<{
    planId: string;
    objective: string;
    contextDigest: string;
    budget: ReturnType<typeof executionBudget>;
  }> = {},
) {
  return runtime.create({
    planId: overrides.planId ?? "plan-1",
    sessionId: "session-1",
    runId: "run-1",
    queryId: "query-1",
    objective: overrides.objective ?? "Complete the requested repository change.",
    contextDigest: overrides.contextDigest ?? "context-digest-1",
    budget: overrides.budget ?? executionBudget(),
    metadata: { fixture: "query-context" },
  });
}

function cacheKey(overrides: Partial<{
  sessionId: string;
  branchId: string;
  modelId: string;
  policyDigest: string;
  systemPromptDigest: string;
  toolSetDigest: string;
  messageBoundaryId: string;
  messageSequence: number;
  compactGeneration: number;
  disclosureClass: string;
}> = {}) {
  return {
    sessionId: overrides.sessionId ?? "session-1",
    branchId: overrides.branchId ?? "main",
    modelId: overrides.modelId ?? "claude-test",
    policyDigest: overrides.policyDigest ?? "policy-1",
    systemPromptDigest: overrides.systemPromptDigest ?? "system-1",
    toolSetDigest: overrides.toolSetDigest ?? "tools-1",
    messageBoundaryId: overrides.messageBoundaryId ?? "message-10",
    messageSequence: overrides.messageSequence ?? 10,
    compactGeneration: overrides.compactGeneration ?? 0,
    disclosureClass: overrides.disclosureClass ?? "internal",
  };
}

function cacheValue(overrides: Partial<{
  estimatedInputTokens: number;
  selectedMessageIds: string[];
  omittedMessageIds: string[];
  compactSummaryId: string | null;
  provenanceDigest: string;
}> = {}) {
  return {
    system: { text: "system" },
    messages: [{ role: "user", content: "inspect runtime" }],
    tools: [{ name: "read" }],
    estimatedInputTokens: overrides.estimatedInputTokens ?? 32,
    selectedMessageIds: overrides.selectedMessageIds ?? ["message-10"],
    omittedMessageIds: overrides.omittedMessageIds ?? [],
    compactSummaryId: overrides.compactSummaryId ?? null,
    provenanceDigest: overrides.provenanceDigest ?? "provenance-1",
    metadata: { fixture: "cache" },
  };
}

function citation(index: number) {
  return {
    eventId: `event-${index}`,
    transitionId: `transition-${index}`,
    messageId: `message-${index}`,
    artifactId: null,
    occurredAt: index * 100,
    digest: `citation-digest-${index}`,
  };
}

function stopRule(overrides: Partial<{
  ruleId: string;
  priority: number;
  signals: Array<
    | "explicit_cancel"
    | "maximum_turns"
    | "token_budget"
    | "model_stop"
    | "context_overflow"
  >;
  conditions: Array<{
    path: string;
    operator:
      | "equals"
      | "not_equals"
      | "greater_than"
      | "greater_or_equal"
      | "less_than"
      | "less_or_equal"
      | "includes"
      | "present"
      | "absent";
    value?: JsonValue;
  }>;
  action: "continue" | "stop" | "retry" | "compact" | "suspend";
  reason: string;
  terminal: boolean;
  retryAfterMilliseconds: number | null;
  requireTerminalAnswer: boolean;
}> = {}) {
  return {
    ruleId: overrides.ruleId ?? "rule-1",
    owner: "test",
    priority: overrides.priority ?? 10,
    enabled: true,
    signals: overrides.signals ?? ["explicit_cancel" as const],
    match: "all" as const,
    conditions: overrides.conditions ?? [],
    action: overrides.action ?? "stop",
    reason: overrides.reason ?? "test_stop",
    terminal: overrides.terminal ?? true,
    retryAfterMilliseconds: overrides.retryAfterMilliseconds ?? null,
    requireTerminalAnswer: overrides.requireTerminalAnswer ?? false,
    revision: 1,
    metadata: { fixture: "stop" },
  };
}

function evaluateStop(
  runtime: QueryStopRuntime,
  signal:
    | "explicit_cancel"
    | "maximum_turns"
    | "token_budget"
    | "model_stop"
    | "context_overflow",
  evidence: JsonRecord,
  terminalAnswer: string | null = null,
  revision = 1,
) {
  return runtime.evaluate({
    sessionId: "session-1",
    runId: "run-1",
    queryId: "query-1",
    turnId: "turn-1",
    signal,
    correlationId: `correlation-${signal}-${revision}`,
    evidence,
    terminalAnswer,
    currentRevision: revision,
  });
}

describe("query hook runtime", () => {
  test("orders hooks by dependency before priority and preserves audit order", async () => {
    const clock = new ManualClock(1_000);
    const runtime = new QueryHookRuntime({
      clock,
      ids: ids("hook-order"),
    });
    const invoked: string[] = [];
    runtime.register({
      descriptor: {
        hookId: "third",
        phase: "query.accept",
        priority: -100,
        timeoutMilliseconds: 1_000,
        failureMode: "fail_closed",
        enabled: true,
        before: [],
        after: ["second"],
        owner: "test",
        revision: 1,
        metadata: {},
      },
      handler: () => {
        invoked.push("third");
        return { kind: "continue", reason: "third" };
      },
    });
    runtime.register({
      descriptor: {
        hookId: "first",
        phase: "query.accept",
        priority: 100,
        timeoutMilliseconds: 1_000,
        failureMode: "fail_closed",
        enabled: true,
        before: ["second"],
        after: [],
        owner: "test",
        revision: 1,
        metadata: {},
      },
      handler: () => {
        invoked.push("first");
        return { kind: "continue", reason: "first" };
      },
    });
    runtime.register({
      descriptor: {
        hookId: "second",
        phase: "query.accept",
        priority: 0,
        timeoutMilliseconds: 1_000,
        failureMode: "fail_closed",
        enabled: true,
        before: [],
        after: [],
        owner: "test",
        revision: 1,
        metadata: {},
      },
      handler: () => {
        invoked.push("second");
        return { kind: "continue", reason: "second" };
      },
    });
    const result = await runtime.run({
      sessionId: "session-1",
      runId: "run-1",
      queryId: "query-1",
      turnId: null,
      phase: "query.accept",
      correlationId: "correlation-1",
      payload: { accepted: true },
    });
    expect(runtime.orderedHookIds("query.accept")).toEqual([
      "first",
      "second",
      "third",
    ]);
    expect(invoked).toEqual(["first", "second", "third"]);
    expect(result.appliedHooks).toEqual(invoked);
    expect(result.decision).toBe("continue");
    expect(runtime.queryAudits({ invocationId: result.invocationId })).toHaveLength(3);
  });

  test("applies add replace test and remove patches atomically", async () => {
    const runtime = new QueryHookRuntime({ ids: ids("hook-patches") });
    runtime.register({
      descriptor: {
        hookId: "patcher",
        phase: "context.after_build",
        priority: 0,
        timeoutMilliseconds: 1_000,
        failureMode: "fail_closed",
        enabled: true,
        before: [],
        after: [],
        owner: "test",
        revision: 1,
        metadata: {},
      },
      handler: () => ({
        kind: "continue",
        reason: "normalized",
        patches: [
          { operation: "test", path: "/budget", value: 100 },
          { operation: "replace", path: "/budget", value: 80 },
          { operation: "add", path: "/labels/-", value: "verified" },
          { operation: "remove", path: "/temporary" },
        ],
      }),
    });
    const result = await runtime.run({
      sessionId: "session-1",
      runId: "run-1",
      queryId: "query-1",
      turnId: "turn-1",
      phase: "context.after_build",
      correlationId: "correlation-patch",
      payload: {
        budget: 100,
        labels: ["initial"],
        temporary: true,
      },
    });
    expect(result.value).toEqual({
      budget: 80,
      labels: ["initial", "verified"],
    });
    expect(result.auditIds).toHaveLength(1);
    expect(runtime.getAudit(result.auditIds[0]!).patchCount).toBe(4);
  });

  test("fail-open hook records error and permits the next handler", async () => {
    const runtime = new QueryHookRuntime({ ids: ids("hook-open") });
    runtime.register({
      descriptor: {
        hookId: "optional-observer",
        phase: "model.before_request",
        priority: 0,
        timeoutMilliseconds: 1_000,
        failureMode: "fail_open",
        enabled: true,
        before: [],
        after: [],
        owner: "test",
        revision: 1,
        metadata: {},
      },
      handler: () => {
        throw new Error("observer unavailable");
      },
    });
    runtime.register({
      descriptor: {
        hookId: "mandatory-normalizer",
        phase: "model.before_request",
        priority: 1,
        timeoutMilliseconds: 1_000,
        failureMode: "fail_closed",
        enabled: true,
        before: [],
        after: [],
        owner: "test",
        revision: 1,
        metadata: {},
      },
      handler: () => ({
        kind: "continue",
        reason: "normalized",
        patches: [{ operation: "add", path: "/normalized", value: true }],
      }),
    });
    const result = await runtime.run({
      sessionId: "session-1",
      runId: "run-1",
      queryId: "query-1",
      turnId: "turn-1",
      phase: "model.before_request",
      correlationId: "correlation-open",
      payload: { model: "claude-test" },
    });
    expect(result.decision).toBe("continue");
    expect(result.value.normalized).toBe(true);
    expect(runtime.queryAudits({ decision: "error" })).toHaveLength(1);
    expect(result.appliedHooks).toEqual(["mandatory-normalizer"]);
  });

  test("fail-closed hook blocks and prevents lower-priority execution", async () => {
    const runtime = new QueryHookRuntime({ ids: ids("hook-closed") });
    let reached = false;
    runtime.register({
      descriptor: {
        hookId: "policy",
        phase: "tool.before_execute",
        priority: 0,
        timeoutMilliseconds: 1_000,
        failureMode: "fail_closed",
        enabled: true,
        before: [],
        after: [],
        owner: "test",
        revision: 1,
        metadata: {},
      },
      handler: () => {
        throw new Error("policy store unavailable");
      },
    });
    runtime.register({
      descriptor: {
        hookId: "executor-observer",
        phase: "tool.before_execute",
        priority: 1,
        timeoutMilliseconds: 1_000,
        failureMode: "fail_open",
        enabled: true,
        before: [],
        after: [],
        owner: "test",
        revision: 1,
        metadata: {},
      },
      handler: () => {
        reached = true;
        return { kind: "continue", reason: "observed" };
      },
    });
    const result = await runtime.run({
      sessionId: "session-1",
      runId: "run-1",
      queryId: "query-1",
      turnId: "turn-1",
      phase: "tool.before_execute",
      correlationId: "correlation-closed",
      payload: { tool: "write" },
    });
    expect(result.decision).toBe("block");
    expect(result.reason).toContain("hook_failure:policy");
    expect(reached).toBe(false);
  });

  test("quarantines a failing hook and persists quarantine across restore", async () => {
    const runtime = new QueryHookRuntime({ ids: ids("hook-quarantine") });
    runtime.register({
      descriptor: {
        hookId: "classifier",
        phase: "tool.before_permission",
        priority: 0,
        timeoutMilliseconds: 1_000,
        failureMode: "quarantine",
        enabled: true,
        before: [],
        after: [],
        owner: "test",
        revision: 1,
        metadata: {},
      },
      handler: () => {
        throw new Error("bad classifier");
      },
    });
    const first = await runtime.run({
      sessionId: "session-1",
      runId: "run-1",
      queryId: "query-1",
      turnId: "turn-1",
      phase: "tool.before_permission",
      correlationId: "correlation-quarantine-1",
      payload: { tool: "shell" },
    });
    expect(first.decision).toBe("continue");
    const snapshot = runtime.snapshot();
    expect(snapshot.quarantine).toHaveLength(1);
    const restored = new QueryHookRuntime({ ids: ids("hook-quarantine-restored") });
    restored.register({
      descriptor: snapshot.descriptors[0]!,
      handler: () => ({ kind: "continue", reason: "should remain quarantined" }),
    });
    restored.restore(snapshot);
    const second = await restored.run({
      sessionId: "session-1",
      runId: "run-2",
      queryId: "query-1",
      turnId: "turn-2",
      phase: "tool.before_permission",
      correlationId: "correlation-quarantine-2",
      payload: { tool: "shell" },
    });
    expect(second.appliedHooks).toEqual([]);
    expect(second.skippedHooks).toEqual(["classifier"]);
    expect(restored.queryAudits({ decision: "skipped" })).toHaveLength(1);
  });

  test("rejects dependency cycles during configuration", () => {
    const runtime = new QueryHookRuntime({ ids: ids("hook-cycle") });
    for (const hookId of ["alpha", "beta"]) {
      runtime.register({
        descriptor: {
          hookId,
          phase: "compact.before",
          priority: 0,
          timeoutMilliseconds: 1_000,
          failureMode: "fail_closed",
          enabled: true,
          before: [],
          after: [],
          owner: "test",
          revision: 1,
          metadata: {},
        },
        handler: () => ({ kind: "continue", reason: hookId }),
      });
    }
    runtime.configure("alpha", { before: ["beta"] });
    expect(() => runtime.configure("beta", { before: ["alpha"] })).toThrow(
      "hook_dependency_cycle",
    );
    expect(runtime.orderedHookIds("compact.before")).toEqual(["alpha", "beta"]);
  });

  test("enforces retry delay bounds supplied by a hook", async () => {
    const runtime = new QueryHookRuntime({
      ids: ids("hook-retry"),
      maximumRetryDelayMilliseconds: 100,
    });
    runtime.register({
      descriptor: {
        hookId: "retry-policy",
        phase: "model.after_response",
        priority: 0,
        timeoutMilliseconds: 1_000,
        failureMode: "fail_closed",
        enabled: true,
        before: [],
        after: [],
        owner: "test",
        revision: 1,
        metadata: {},
      },
      handler: () => ({
        kind: "retry",
        reason: "provider overloaded",
        retryAfterMilliseconds: 101,
      }),
    });
    const result = await runtime.run({
      sessionId: "session-1",
      runId: "run-1",
      queryId: "query-1",
      turnId: "turn-1",
      phase: "model.after_response",
      correlationId: "correlation-retry",
      payload: { status: 529 },
    });
    expect(result.decision).toBe("block");
    expect(result.reason).toContain("hook_failure:retry-policy");
    expect(runtime.queryAudits({ decision: "error" })[0]?.errorCode).toBe(
      "hook_retry_delay_exceeded",
    );
  });

  test("rejects tampered hook snapshots before replacing live descriptors", () => {
    const runtime = new QueryHookRuntime({ ids: ids("hook-snapshot") });
    runtime.register({
      descriptor: {
        hookId: "stable",
        phase: "query.after_stop",
        priority: 0,
        timeoutMilliseconds: 1_000,
        failureMode: "fail_closed",
        enabled: true,
        before: [],
        after: [],
        owner: "test",
        revision: 1,
        metadata: {},
      },
      handler: () => ({ kind: "continue", reason: "stable" }),
    });
    const snapshot = runtime.snapshot();
    snapshot.descriptors[0]!.enabled = false;
    expect(() => runtime.restore(snapshot)).toThrow(
      "hook_snapshot_checksum_mismatch",
    );
    expect(runtime.list("query.after_stop")[0]?.enabled).toBe(true);
  });
});

describe("query execution plan", () => {
  test("executes a complete reason tool observe revise loop", () => {
    const clock = new ManualClock(10_000);
    const runtime = new QueryExecutionPlanRuntime({
      clock,
      ids: ids("plan-loop"),
    });
    createPlan(runtime);
    const reasoning = runtime.beginReason(
      "plan-1",
      "prompt-digest",
      "correlation-reason",
    );
    clock.advance(20);
    const completedReason = runtime.completeReason("plan-1", {
      reasoningId: reasoning.reasoningId,
      conclusion: "Inspect the repository before editing.",
      reasoningTokens: 120,
      tool: {
        toolName: "read_file",
        input: { path: "src/main.ts" },
        idempotencyKey: "read-main-once",
      },
      correlationId: "correlation-reason-complete",
    });
    const toolCallId = completedReason.proposedToolCallId!;
    runtime.decidePermission({
      planId: "plan-1",
      toolCallId,
      permissionDecisionId: "permission-1",
      allowed: true,
      reason: "read-only tool",
      correlationId: "correlation-permission",
    });
    runtime.beginTool({
      planId: "plan-1",
      toolCallId,
      effectId: "effect-read-1",
      correlationId: "correlation-tool-start",
    });
    clock.advance(30);
    const observation = runtime.completeTool({
      planId: "plan-1",
      toolCallId,
      output: { content: "export const value = 1;" },
      startedAt: 10_020,
      metadata: { bytes: 23 },
      correlationId: "correlation-tool-complete",
    });
    runtime.recordObservation({
      planId: "plan-1",
      toolCallId,
      correlationId: "correlation-observed",
    });
    const revision = runtime.beginRevision({
      planId: "plan-1",
      observationDigest: observation.outputDigest,
      correlationId: "correlation-revise",
    });
    runtime.completeRevision({
      planId: "plan-1",
      revisionId: revision.revisionId,
      assessment: "The target file is ready for the requested edit.",
      nextAction: "complete",
      terminalAnswer: "Repository inspection completed.",
      correlationId: "correlation-revise-complete",
    });
    const project = runtime.get("plan-1");
    expect(project.status).toBe("completed");
    expect(project.usage).toEqual({
      turns: 1,
      toolCalls: 1,
      reasoningTokens: 120,
      outputTokens: 0,
      consecutiveToolFailures: 0,
    });
    expect(project.terminalAnswer).toBe("Repository inspection completed.");
    expect(runtime.listTransitions("plan-1").map((item) => item.kind)).toEqual([
      "plan.created",
      "reason.started",
      "reason.completed",
      "permission.allowed",
      "tool.started",
      "tool.completed",
      "observation.recorded",
      "revision.started",
      "revision.completed",
    ]);
  });

  test("permission denial returns the plan to revision without an effect", () => {
    const runtime = new QueryExecutionPlanRuntime({ ids: ids("plan-deny") });
    createPlan(runtime);
    const reasoning = runtime.beginReason(
      "plan-1",
      "prompt-digest",
      "correlation-reason",
    );
    const complete = runtime.completeReason("plan-1", {
      reasoningId: reasoning.reasoningId,
      conclusion: "Delete the protected artifact.",
      reasoningTokens: 40,
      tool: {
        toolName: "delete_file",
        input: { path: "protected.txt" },
        idempotencyKey: "delete-protected",
      },
      correlationId: "correlation-proposal",
    });
    runtime.decidePermission({
      planId: "plan-1",
      toolCallId: complete.proposedToolCallId!,
      permissionDecisionId: "permission-denied",
      allowed: false,
      reason: "path is protected",
      correlationId: "correlation-denial",
    });
    const project = runtime.get("plan-1");
    expect(project.status).toBe("revising");
    expect(project.activeToolCallId).toBeNull();
    expect(project.usage.toolCalls).toBe(0);
    expect(runtime.snapshot().completedEffects).toEqual([]);
  });

  test("rejects reasoning tokens that exceed the durable plan budget", () => {
    const runtime = new QueryExecutionPlanRuntime({ ids: ids("plan-tokens") });
    createPlan(runtime, {
      budget: executionBudget({ maximumReasoningTokens: 10 }),
    });
    const reasoning = runtime.beginReason(
      "plan-1",
      "prompt-digest",
      "correlation-reason",
    );
    expect(() =>
      runtime.completeReason("plan-1", {
        reasoningId: reasoning.reasoningId,
        conclusion: "This conclusion is too expensive.",
        reasoningTokens: 11,
        correlationId: "correlation-complete",
      }),
    ).toThrow("reasoning_token_budget_exceeded");
    expect(runtime.get("plan-1").status).toBe("reasoning");
    expect(runtime.get("plan-1").usage.reasoningTokens).toBe(0);
  });

  test("enforces wall deadline before starting another reasoning turn", () => {
    const clock = new ManualClock(500);
    const runtime = new QueryExecutionPlanRuntime({
      clock,
      ids: ids("plan-deadline"),
    });
    createPlan(runtime, {
      budget: executionBudget({ maximumWallMilliseconds: 25 }),
    });
    clock.advance(25);
    expect(() =>
      runtime.beginReason(
        "plan-1",
        "prompt-digest",
        "correlation-after-deadline",
      ),
    ).toThrow("execution_wall_budget_exceeded");
    expect(runtime.get("plan-1").status).toBe("created");
  });

  test("prevents a completed idempotent effect from running twice", () => {
    const runtime = new QueryExecutionPlanRuntime({ ids: ids("plan-effect") });
    createPlan(runtime);
    const reasoning = runtime.beginReason(
      "plan-1",
      "prompt-digest",
      "correlation-reason",
    );
    const complete = runtime.completeReason("plan-1", {
      reasoningId: reasoning.reasoningId,
      conclusion: "Read the file.",
      reasoningTokens: 10,
      tool: {
        toolName: "read_file",
        input: { path: "file.txt" },
        idempotencyKey: "same-effect",
      },
      correlationId: "correlation-proposal",
    });
    const toolCallId = complete.proposedToolCallId!;
    runtime.decidePermission({
      planId: "plan-1",
      toolCallId,
      permissionDecisionId: "allow-1",
      allowed: true,
      reason: "allowed",
      correlationId: "correlation-allow",
    });
    runtime.beginTool({
      planId: "plan-1",
      toolCallId,
      effectId: "effect-1",
      correlationId: "correlation-start",
    });
    runtime.completeTool({
      planId: "plan-1",
      toolCallId,
      output: "value",
      startedAt: 0,
      correlationId: "correlation-complete",
    });
    expect(runtime.snapshot().completedEffects).toEqual([
      { idempotencyKey: "same-effect", effectId: "effect-1" },
    ]);
    expect(runtime.completeTool({
      planId: "plan-1",
      toolCallId,
      output: "value",
      startedAt: 0,
      correlationId: "correlation-repeated-complete",
    }).outputDigest).toBeDefined();
  });

  test("restores a suspended plan and advances restart epoch on resume", () => {
    const runtime = new QueryExecutionPlanRuntime({ ids: ids("plan-restore") });
    createPlan(runtime);
    runtime.suspend("plan-1", "process_restart", "correlation-suspend");
    const snapshot = runtime.snapshot();
    const restored = new QueryExecutionPlanRuntime({
      ids: ids("plan-restore-after", 100),
    });
    restored.restore(snapshot);
    expect(restored.get("plan-1").restartEpoch).toBe(1);
    restored.resume("plan-1", "created", "correlation-resume");
    expect(restored.get("plan-1").restartEpoch).toBe(2);
    expect(restored.get("plan-1").status).toBe("created");
    const transitionIds = restored
      .listTransitions("plan-1")
      .map((transition) => transition.transitionId);
    expect(new Set(transitionIds).size).toBe(transitionIds.length);
  });

  test("tracks consecutive tool failures and rejects the next tool start", () => {
    const runtime = new QueryExecutionPlanRuntime({ ids: ids("plan-failures") });
    createPlan(runtime, {
      budget: executionBudget({ maximumConsecutiveToolFailures: 1 }),
    });
    const reasoning = runtime.beginReason(
      "plan-1",
      "prompt-digest",
      "correlation-reason",
    );
    const complete = runtime.completeReason("plan-1", {
      reasoningId: reasoning.reasoningId,
      conclusion: "Call the unstable tool.",
      reasoningTokens: 10,
      tool: {
        toolName: "unstable",
        input: {},
        idempotencyKey: "unstable-1",
      },
      correlationId: "correlation-proposal",
    });
    const toolCallId = complete.proposedToolCallId!;
    runtime.decidePermission({
      planId: "plan-1",
      toolCallId,
      permissionDecisionId: "allow-1",
      allowed: true,
      reason: "allowed",
      correlationId: "correlation-allow",
    });
    runtime.beginTool({
      planId: "plan-1",
      toolCallId,
      effectId: "effect-1",
      correlationId: "correlation-start",
    });
    runtime.failTool({
      planId: "plan-1",
      toolCallId,
      output: { error: "unavailable" },
      startedAt: 0,
      errorCode: "unavailable",
      correlationId: "correlation-fail",
    });
    runtime.recordObservation({
      planId: "plan-1",
      toolCallId,
      correlationId: "correlation-observe",
    });
    const revision = runtime.beginRevision({
      planId: "plan-1",
      observationDigest: "failed-observation",
      correlationId: "correlation-revision",
    });
    runtime.completeRevision({
      planId: "plan-1",
      revisionId: revision.revisionId,
      assessment: "Retry with a different approach.",
      nextAction: "reason",
      correlationId: "correlation-revision-complete",
    });
    expect(() =>
      runtime.beginReason(
        "plan-1",
        "next-prompt",
        "correlation-next-reason",
      ),
    ).toThrow("execution_tool_failure_budget_exceeded");
  });

  test("rejects cancellation after the plan has completed", () => {
    const runtime = new QueryExecutionPlanRuntime({ ids: ids("plan-terminal") });
    createPlan(runtime);
    const reasoning = runtime.beginReason(
      "plan-1",
      "prompt-digest",
      "correlation-reason",
    );
    runtime.completeReason("plan-1", {
      reasoningId: reasoning.reasoningId,
      conclusion: "No tool is required.",
      reasoningTokens: 5,
      terminalAnswer: "Complete.",
      correlationId: "correlation-complete",
    });
    expect(runtime.get("plan-1").status).toBe("completed");
    expect(() =>
      runtime.cancel("plan-1", "too late", "correlation-cancel"),
    ).toThrow("execution_plan_terminal");
  });
});

describe("query stop policy", () => {
  test("selects the highest-priority matching terminal rule", () => {
    const runtime = new QueryStopRuntime({ ids: ids("stop-priority") });
    runtime.register(
      stopRule({
        ruleId: "generic-cancel",
        priority: 100,
        reason: "generic",
      }),
    );
    runtime.register(
      stopRule({
        ruleId: "security-cancel",
        priority: -100,
        conditions: [{ path: "/source", operator: "equals", value: "security" }],
        reason: "security_interrupt",
      }),
    );
    const decision = evaluateStop(runtime, "explicit_cancel", {
      source: "security",
    });
    expect(decision.action).toBe("stop");
    expect(decision.matchedRuleId).toBe("security-cancel");
    expect(decision.reason).toBe("security_interrupt");
    expect(decision.matches.filter((match) => match.matched)).toHaveLength(2);
  });

  test("requires a terminal answer when a rule declares that obligation", () => {
    const runtime = new QueryStopRuntime({ ids: ids("stop-answer") });
    runtime.register(
      stopRule({
        ruleId: "model-terminal",
        signals: ["model_stop"],
        requireTerminalAnswer: true,
        reason: "model_finished",
      }),
    );
    const missing = evaluateStop(runtime, "model_stop", { stop: true });
    expect(missing.action).toBe("continue");
    expect(missing.reason).toBe("terminal_answer_missing:model-terminal");
    const present = evaluateStop(
      runtime,
      "model_stop",
      { stop: true },
      "Final answer",
      2,
    );
    expect(present.action).toBe("stop");
    expect(present.terminalAnswerDigest).not.toBeNull();
  });

  test("evaluates numeric and collection conditions from JSON pointers", () => {
    const runtime = new QueryStopRuntime({ ids: ids("stop-conditions") });
    runtime.register(
      stopRule({
        ruleId: "budget-exhausted",
        signals: ["token_budget"],
        conditions: [
          { path: "/usage/tokens", operator: "greater_or_equal", value: 100 },
          { path: "/flags", operator: "includes", value: "enforced" },
          { path: "/override", operator: "absent" },
        ],
        reason: "token_limit_reached",
      }),
    );
    const decision = evaluateStop(runtime, "token_budget", {
      usage: { tokens: 100 },
      flags: ["measured", "enforced"],
    });
    expect(decision.action).toBe("stop");
    expect(decision.matches[0]?.conditionResults).toEqual([true, true, true]);
  });

  test("does not apply a decision to a newer query revision", () => {
    const runtime = new QueryStopRuntime({ ids: ids("stop-stale") });
    runtime.register(stopRule({ ruleId: "cancel" }));
    const decision = evaluateStop(runtime, "explicit_cancel", {}, null, 5);
    expect(() =>
      runtime.assertApplicable(
        decision.decisionId,
        "session-1",
        "query-1",
        6,
      ),
    ).toThrow("stale_stop_decision");
    expect(
      runtime.assertApplicable(
        decision.decisionId,
        "session-1",
        "query-1",
        5,
      ).decisionId,
    ).toBe(decision.decisionId);
  });

  test("persists decisions and rejects a modified decision digest", () => {
    const runtime = new QueryStopRuntime({ ids: ids("stop-snapshot") });
    runtime.register(stopRule({ ruleId: "cancel" }));
    evaluateStop(runtime, "explicit_cancel", { actor: "user" });
    const snapshot = runtime.snapshot();
    const restored = new QueryStopRuntime({ ids: ids("stop-snapshot-after") });
    restored.restore(snapshot);
    expect(restored.listRules()).toHaveLength(1);
    expect(restored.listDecisions()).toHaveLength(1);
    snapshot.decisions[0]!.reason = "tampered";
    snapshot.checksum = runtime.snapshot().checksum;
    expect(() => restored.restore(snapshot)).toThrow(
      "stop_snapshot_checksum_mismatch",
    );
  });
});

describe("context cache runtime", () => {
  test("moves from miss through a build lease to a verified cache hit", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ContextCacheRuntime({
      clock,
      ids: ids("cache-hit"),
      ttlMilliseconds: 1_000,
    });
    const key = cacheKey();
    expect(runtime.lookup(key).status).toBe("miss");
    const lease = runtime.acquireBuildLease(key, "worker-1");
    const entry = runtime.commit(
      lease.leaseId,
      key,
      cacheValue(),
      ["messages:session-1", "policy:policy-1"],
    );
    expect(entry.hits).toBe(0);
    const lookup = runtime.lookup(key);
    expect(lookup.status).toBe("hit");
    expect(lookup.entry?.hits).toBe(1);
    expect(lookup.entry?.value.provenanceDigest).toBe("provenance-1");
    expect(runtime.currentMetrics()).toMatchObject({
      hits: 1,
      misses: 1,
      builds: 1,
      currentEntries: 1,
    });
  });

  test("prevents two workers from building the same context key", () => {
    const runtime = new ContextCacheRuntime({ ids: ids("cache-stampede") });
    const key = cacheKey();
    const first = runtime.acquireBuildLease(key, "worker-1");
    expect(runtime.acquireBuildLease(key, "worker-1").leaseId).toBe(
      first.leaseId,
    );
    expect(() => runtime.acquireBuildLease(key, "worker-2")).toThrow(
      "context_cache_build_in_progress",
    );
    expect(runtime.lookup(key).status).toBe("building");
    runtime.abort(first.leaseId);
    expect(runtime.lookup(key).status).toBe("miss");
  });

  test("rejects a build whose dependency changed while the worker built", () => {
    const runtime = new ContextCacheRuntime({ ids: ids("cache-race") });
    const key = cacheKey();
    const lease = runtime.acquireBuildLease(key, "worker-1");
    runtime.invalidateDependency("messages:session-1");
    expect(() =>
      runtime.commit(
        lease.leaseId,
        key,
        cacheValue(),
        ["messages:session-1"],
      ),
    ).toThrow("context_cache_build_invalidated");
    const replacement = runtime.acquireBuildLease(key, "worker-2");
    expect(() =>
      runtime.commit(
        replacement.leaseId,
        key,
        cacheValue(),
        ["messages:session-1"],
      ),
    ).not.toThrow();
  });

  test("expires entries at the configured TTL and records a stale lookup", () => {
    const clock = new ManualClock(5_000);
    const runtime = new ContextCacheRuntime({
      clock,
      ids: ids("cache-ttl"),
      ttlMilliseconds: 100,
    });
    const key = cacheKey();
    const lease = runtime.acquireBuildLease(key, "worker-1");
    runtime.commit(lease.leaseId, key, cacheValue(), ["messages"]);
    clock.advance(99);
    expect(runtime.lookup(key).status).toBe("hit");
    clock.advance(1);
    expect(runtime.lookup(key).status).toBe("stale");
    expect(runtime.currentMetrics().stale).toBe(1);
    expect(runtime.currentMetrics().currentEntries).toBe(0);
  });

  test("evicts the least recently used entry when entry capacity is reached", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ContextCacheRuntime({
      clock,
      ids: ids("cache-lru"),
      maximumEntries: 2,
      maximumBytes: 10_000_000,
    });
    const first = cacheKey({ messageBoundaryId: "first", messageSequence: 1 });
    const second = cacheKey({ messageBoundaryId: "second", messageSequence: 2 });
    const third = cacheKey({ messageBoundaryId: "third", messageSequence: 3 });
    for (const key of [first, second]) {
      const lease = runtime.acquireBuildLease(key, `worker-${key.messageSequence}`);
      runtime.commit(lease.leaseId, key, cacheValue(), [`message:${key.messageSequence}`]);
      clock.advance(10);
    }
    runtime.lookup(first);
    clock.advance(10);
    const thirdLease = runtime.acquireBuildLease(third, "worker-3");
    runtime.commit(thirdLease.leaseId, third, cacheValue(), ["message:3"]);
    expect(runtime.lookup(first).status).toBe("hit");
    expect(runtime.lookup(second).status).toBe("miss");
    expect(runtime.lookup(third).status).toBe("hit");
    expect(runtime.currentMetrics().evictions).toBe(1);
  });

  test("invalidates only entries owned by the selected branch", () => {
    const runtime = new ContextCacheRuntime({ ids: ids("cache-branch") });
    const main = cacheKey({ branchId: "main", messageBoundaryId: "main-1" });
    const feature = cacheKey({
      branchId: "feature",
      messageBoundaryId: "feature-1",
    });
    for (const key of [main, feature]) {
      const lease = runtime.acquireBuildLease(key, `worker-${key.branchId}`);
      runtime.commit(lease.leaseId, key, cacheValue(), [key.branchId]);
    }
    const removed = runtime.invalidateBranch("session-1", "feature");
    expect(removed).toHaveLength(1);
    expect(runtime.lookup(main).status).toBe("hit");
    expect(runtime.lookup(feature).status).toBe("miss");
  });

  test("detects a selected and omitted message conflict before commit", () => {
    const runtime = new ContextCacheRuntime({ ids: ids("cache-conflict") });
    const key = cacheKey();
    const lease = runtime.acquireBuildLease(key, "worker-1");
    expect(() =>
      runtime.commit(
        lease.leaseId,
        key,
        cacheValue({
          selectedMessageIds: ["message-10"],
          omittedMessageIds: ["message-10"],
        }),
        [],
      ),
    ).toThrow("context_message_selected_and_omitted");
    expect(runtime.currentMetrics().currentEntries).toBe(0);
  });

  test("round-trips entries and rejects a modified value digest", () => {
    const runtime = new ContextCacheRuntime({ ids: ids("cache-snapshot") });
    const key = cacheKey();
    const lease = runtime.acquireBuildLease(key, "worker-1");
    runtime.commit(lease.leaseId, key, cacheValue(), ["messages"]);
    const snapshot = runtime.snapshot();
    const restored = new ContextCacheRuntime({ ids: ids("cache-after") });
    restored.restore(snapshot);
    expect(restored.lookup(key).status).toBe("hit");
    snapshot.entries[0]!.value.estimatedInputTokens += 1;
    expect(() => restored.restore(snapshot)).toThrow(
      "context_cache_snapshot_checksum_mismatch",
    );
  });
});

describe("compact summary runtime", () => {
  test("renders cited objectives decisions effects recovery and open loops", () => {
    const clock = new ManualClock(10_000);
    const runtime = new CompactSummaryRuntime({
      clock,
      ids: ids("summary-sections"),
    });
    const summary = runtime.build({
      sessionId: "session-1",
      runId: "run-1",
      coveredSequenceStart: 1,
      coveredSequenceEnd: 10,
      sourceDigest: "source-1",
      maximumCharacters: 20_000,
      candidates: [
        {
          kind: "objective",
          subject: "runtime",
          predicate: "must complete",
          value: "E01 v3",
          confidence: "observed",
          importance: 1,
          citations: [citation(1)],
        },
        {
          kind: "decision",
          subject: "toolchain",
          predicate: "uses",
          value: "Bun 1.2.15",
          confidence: "observed",
          importance: 0.9,
          citations: [citation(2)],
        },
        {
          kind: "artifact",
          subject: "bundle",
          predicate: "created",
          value: "dist/code-worker/main.js",
          confidence: "observed",
          importance: 0.7,
          citations: [citation(3)],
        },
        {
          kind: "recovery",
          subject: "session",
          predicate: "restored",
          value: "restart epoch 1",
          confidence: "observed",
          importance: 0.8,
          citations: [citation(4)],
        },
        {
          kind: "open_loop",
          subject: "review",
          predicate: "awaits",
          value: "independent PASS",
          confidence: "reported",
          importance: 1,
          citations: [citation(5)],
        },
      ],
    });
    expect(summary.items).toHaveLength(5);
    expect(summary.rendered).toContain("## Active objectives");
    expect(summary.rendered).toContain("## Decisions and durable facts");
    expect(summary.rendered).toContain("## Artifacts and tool effects");
    expect(summary.rendered).toContain("## Failures and recovery");
    expect(summary.rendered).toContain("## Open loops");
    expect(runtime.coverage(summary.summaryId)).toEqual({
      citedEvents: 5,
      citedTransitions: 5,
      citedMessages: 5,
      citedArtifacts: 0,
      uncitedItems: [],
    });
  });

  test("merges the same normalized fact and upgrades confidence", () => {
    const clock = new ManualClock(1_000);
    const runtime = new CompactSummaryRuntime({
      clock,
      ids: ids("summary-merge"),
    });
    const first = runtime.build({
      sessionId: "session-1",
      runId: "run-1",
      coveredSequenceStart: 1,
      coveredSequenceEnd: 2,
      sourceDigest: "source-1",
      maximumCharacters: 10_000,
      candidates: [{
        kind: "fact",
        subject: " Runtime ",
        predicate: "HAS   OWNER",
        value: "candidate",
        confidence: "reported",
        importance: 0.4,
        citations: [citation(1)],
      }],
    });
    clock.advance(100);
    const second = runtime.build({
      sessionId: "session-1",
      runId: "run-2",
      coveredSequenceStart: 3,
      coveredSequenceEnd: 4,
      sourceDigest: "source-2",
      previousSummaryId: first.summaryId,
      maximumCharacters: 10_000,
      candidates: [{
        kind: "fact",
        subject: "runtime",
        predicate: "has owner",
        value: "TypeScript",
        confidence: "observed",
        importance: 0.9,
        citations: [citation(2)],
      }],
    });
    expect(second.items).toHaveLength(1);
    expect(second.items[0]?.value).toBe("TypeScript");
    expect(second.items[0]?.confidence).toBe("observed");
    expect(second.items[0]?.citations).toHaveLength(2);
    expect(runtime.diff(first.summaryId, second.summaryId).changedItemIds).toHaveLength(1);
  });

  test("omits lower-importance facts when the character budget is exhausted", () => {
    const runtime = new CompactSummaryRuntime({ ids: ids("summary-budget") });
    const summary = runtime.build({
      sessionId: "session-1",
      runId: "run-1",
      coveredSequenceStart: 1,
      coveredSequenceEnd: 10,
      sourceDigest: "source-budget",
      maximumCharacters: 120,
      candidates: [
        {
          kind: "objective",
          subject: "critical",
          predicate: "must remain",
          value: "preserved objective",
          confidence: "observed",
          importance: 1,
          citations: [citation(1)],
        },
        {
          kind: "fact",
          subject: "optional-a",
          predicate: "contains",
          value: "a".repeat(80),
          confidence: "observed",
          importance: 0.2,
          citations: [citation(2)],
        },
        {
          kind: "fact",
          subject: "optional-b",
          predicate: "contains",
          value: "b".repeat(80),
          confidence: "observed",
          importance: 0.1,
          citations: [citation(3)],
        },
      ],
    });
    expect(summary.items.some((item) => item.kind === "objective")).toBe(true);
    expect(summary.omittedItemIds.length).toBeGreaterThan(0);
    expect(summary.renderedCharacters).toBeLessThanOrEqual(120);
  });

  test("resolves and retracts items with new evidence", () => {
    const runtime = new CompactSummaryRuntime({ ids: ids("summary-status") });
    const summary = runtime.build({
      sessionId: "session-1",
      runId: "run-1",
      coveredSequenceStart: 1,
      coveredSequenceEnd: 3,
      sourceDigest: "source-status",
      maximumCharacters: 10_000,
      candidates: [
        {
          kind: "open_loop",
          subject: "typecheck",
          predicate: "is pending",
          value: "not run",
          confidence: "reported",
          importance: 1,
          citations: [citation(1)],
        },
        {
          kind: "fact",
          subject: "baseline",
          predicate: "equals",
          value: "wrong",
          confidence: "reported",
          importance: 0.8,
          citations: [citation(2)],
        },
      ],
    });
    const openLoop = summary.items.find((item) => item.kind === "open_loop")!;
    const fact = summary.items.find((item) => item.kind === "fact")!;
    expect(runtime.resolveItem(openLoop.itemId, citation(3)).status).toBe(
      "resolved",
    );
    const retracted = runtime.retractItem(
      fact.itemId,
      "baseline was re-read",
      citation(4),
    );
    expect(retracted.status).toBe("retracted");
    expect(retracted.metadata.retractionReason).toBe("baseline was re-read");
  });

  test("drops expired inherited facts from the next generation", () => {
    const clock = new ManualClock(1_000);
    const runtime = new CompactSummaryRuntime({
      clock,
      ids: ids("summary-expiry"),
    });
    const first = runtime.build({
      sessionId: "session-1",
      runId: "run-1",
      coveredSequenceStart: 1,
      coveredSequenceEnd: 2,
      sourceDigest: "source-expiring",
      maximumCharacters: 10_000,
      candidates: [{
        kind: "fact",
        subject: "lease",
        predicate: "is valid",
        value: "until 1100",
        confidence: "observed",
        importance: 0.5,
        expiresAt: 1_100,
        citations: [citation(1)],
      }],
    });
    expect(first.items).toHaveLength(1);
    clock.advance(100);
    const second = runtime.build({
      sessionId: "session-1",
      runId: "run-2",
      coveredSequenceStart: 3,
      coveredSequenceEnd: 4,
      sourceDigest: "source-after-expiry",
      previousSummaryId: first.summaryId,
      maximumCharacters: 10_000,
      candidates: [],
    });
    expect(second.items).toEqual([]);
    expect(runtime.diff(first.summaryId, second.summaryId).removedItemIds).toHaveLength(1);
  });

  test("rejects an observed item that has no source citation", () => {
    const runtime = new CompactSummaryRuntime({ ids: ids("summary-citation") });
    expect(() =>
      runtime.build({
        sessionId: "session-1",
        runId: "run-1",
        coveredSequenceStart: 1,
        coveredSequenceEnd: 2,
        sourceDigest: "source-no-citation",
        maximumCharacters: 10_000,
        candidates: [{
          kind: "fact",
          subject: "claim",
          predicate: "is",
          value: "unproven",
          confidence: "observed",
          importance: 0.5,
          citations: [],
        }],
      }),
    ).toThrow("observed_summary_item_requires_citation");
  });

  test("round-trips summary generations and catches checksum tampering", () => {
    const runtime = new CompactSummaryRuntime({ ids: ids("summary-snapshot") });
    const summary = runtime.build({
      sessionId: "session-1",
      runId: "run-1",
      coveredSequenceStart: 1,
      coveredSequenceEnd: 2,
      sourceDigest: "source-snapshot",
      maximumCharacters: 10_000,
      candidates: [{
        kind: "decision",
        subject: "runtime",
        predicate: "uses",
        value: "TypeScript",
        confidence: "observed",
        importance: 1,
        citations: [citation(1)],
      }],
    });
    const snapshot = runtime.snapshot();
    const restored = new CompactSummaryRuntime({ ids: ids("summary-after") });
    restored.restore(snapshot);
    expect(restored.latest("session-1")?.summaryId).toBe(summary.summaryId);
    expect(restored.get(summary.summaryId).checksum).toBe(summary.checksum);
    snapshot.summaries[0]!.rendered = "tampered";
    expect(() => restored.restore(snapshot)).toThrow(
      "summary_snapshot_checksum_mismatch",
    );
  });
});
