import { describe, expect, test } from "bun:test";

import {
  E01_COORDINATOR_SNAPSHOT_VERSION,
  E01RuntimeCoordinator,
  type E01CoordinatorSnapshot,
} from "../../src/e01/coordinator.js";
import { digest, type TransitionReceipt } from "../../src/e01/kernel.js";

function runtime(
  runId = "run-1",
  sessionId = "session-1",
  taskId = "task-1",
  workerRequestId = "worker-request-1",
): E01RuntimeCoordinator {
  return new E01RuntimeCoordinator(
    runId,
    sessionId,
    taskId,
    workerRequestId,
  );
}

async function boot(
  runId = "run-1",
  sessionId = "session-1",
  taskId = "task-1",
  workerRequestId = "worker-request-1",
): Promise<{ coordinator: E01RuntimeCoordinator; receipt: TransitionReceipt }> {
  const coordinator = runtime(runId, sessionId, taskId, workerRequestId);
  const receipt = await coordinator.bootstrap();
  return { coordinator, receipt };
}

function committedOperations(coordinator: E01RuntimeCoordinator): string[] {
  return coordinator.journal.committed().map((receipt) => receipt.operation);
}

function committedIds(coordinator: E01RuntimeCoordinator): Set<string> {
  return new Set(
    coordinator.journal.committed().map(
      (receipt) => receipt.identity.transitionId,
    ),
  );
}

function recomputeCoordinatorChecksum(
  snapshot: E01CoordinatorSnapshot,
): E01CoordinatorSnapshot {
  const unsigned = { ...snapshot } as Partial<E01CoordinatorSnapshot>;
  delete unsigned.checksum;
  snapshot.checksum = digest(unsigned);
  return snapshot;
}

describe("E01 coordinator default TypeScript cutover", () => {
  test("constructs all canonical owners without bootstrapping hidden work", () => {
    const coordinator = runtime();
    const inventory = coordinator.inventory();

    expect(inventory.owner).toBe("typescript");
    expect(inventory.coordinator_version).toBe(E01_COORDINATOR_SNAPSHOT_VERSION);
    expect(inventory.journal_version).toBe("zyra.e01-journal/v3");
    expect(inventory.owner_count).toBe(32);
    expect(inventory.revision).toBe(0);
    expect(inventory.committed).toBe(0);
    expect(inventory.pending).toBe(0);
    expect(inventory.restored).toBe(false);
  });

  test("does not permit event recording before explicit bootstrap", () => {
    const coordinator = runtime();

    expect(() =>
      coordinator.recordRuntimeEvent("provider_request", {
        provider: "anthropic",
      }),
    ).toThrow("e01_runtime_not_bootstrapped");
    expect(coordinator.journal.revision).toBe(0);
    expect(coordinator.journal.committed()).toEqual([]);
  });

  test("bootstraps a fresh TypeScript-owned journal transition", async () => {
    const { coordinator, receipt } = await boot();

    expect(receipt.owner).toBe("typescript");
    expect(receipt.domain).toBe("session");
    expect(receipt.operation).toBe("new_bootstrap");
    expect(receipt.status).toBe("acked");
    expect(receipt.revisionBefore).toBe(0);
    expect(receipt.revisionAfter).toBe(1);
    expect(receipt.readSet).toEqual(["session", "session"]);
    expect(receipt.writeSet).toEqual(["session"]);
    expect(coordinator.journal.state.session).toBeDefined();
  });

  test("activates the durable session and query lifecycle on bootstrap", async () => {
    const { coordinator } = await boot();
    const session = coordinator.session.project();
    const snapshot = coordinator.snapshot();

    expect(session.status).toBe("active");
    expect(snapshot.session.identity.sessionId).toBe("session-1");
    expect(snapshot.session.identity.runId).toBe("run-1");
    expect(coordinator.query.project().status).toBe("queued");
    expect(snapshot.query.identity.sessionId).toBe("session-1");
    expect(snapshot.query.identity.runId).toBe("run-1");
  });

  test("creates the initial execution plan and system context section", async () => {
    const { coordinator } = await boot();
    const snapshot = coordinator.snapshot();

    expect(snapshot.executionPlan.plans).toHaveLength(1);
    expect(snapshot.executionPlan.plans[0]?.planId).toBe(
      "session-1:execution-plan",
    );
    expect(snapshot.context.sections).toHaveLength(1);
    expect(snapshot.context.sections[0]?.kind).toBe("system");
    expect(snapshot.context.sections[0]?.metadata.canonical_owner).toBe(
      "typescript",
    );
    expect(snapshot.input.inputs).toHaveLength(1);
  });

  test("installs default hook stop routing rate and command policy", async () => {
    const { coordinator } = await boot();
    const inventory = coordinator.inventory();

    expect(inventory.query_hooks).toBe(1);
    expect(Number(inventory.query_stop_rules)).toBeGreaterThanOrEqual(2);
    expect(inventory.provider_routes).toBe(1);
    expect(inventory.provider_rate_limits).toBe(1);
    expect(inventory.runtime_commands).toBe(2);
    expect(coordinator.commands.snapshot().descriptors.map((item) => item.commandName).sort()).toEqual([
      "runtime.inventory",
      "session.cancel",
    ]);
  });

  test("rejects a second bootstrap without adding another transition", async () => {
    const { coordinator } = await boot();
    const revision = coordinator.journal.revision;

    await expect(coordinator.bootstrap()).rejects.toThrow(
      "e01_runtime_already_bootstrapped",
    );
    expect(coordinator.journal.revision).toBe(revision);
    expect(committedOperations(coordinator)).toEqual(["new_bootstrap"]);
  });

  test("accepts a normal nonempty query turn", async () => {
    const { coordinator } = await boot();
    const decision = coordinator.decideQuery("query_turn", {
      turnIndex: 0,
      turnLimit: 20,
      empty: false,
      allowEmpty: false,
      aborted: false,
    });

    expect(decision.accepted).toBe(true);
    expect(decision.reason).toBe("accepted");
    expect(decision.domain).toBe("query");
    expect(decision.receipt.status).toBe("acked");
    expect(coordinator.journal.revision).toBe(2);
  });

  test("rejects an empty turn unless empty input is explicitly allowed", async () => {
    const deniedRuntime = (await boot("run-empty-denied")).coordinator;
    const denied = deniedRuntime.decideQuery("query_empty", {
      turnIndex: 1,
      turnLimit: 20,
      empty: true,
      allowEmpty: false,
      aborted: false,
    });
    const allowedRuntime = (await boot("run-empty-allowed")).coordinator;
    const allowed = allowedRuntime.decideQuery("query_empty", {
      turnIndex: 1,
      turnLimit: 20,
      empty: true,
      allowEmpty: true,
      aborted: false,
    });

    expect(denied.accepted).toBe(false);
    expect(denied.reason).toBe("empty_query_turn");
    expect(allowed.accepted).toBe(true);
    expect(allowed.reason).toBe("accepted");
  });

  test("enforces the query turn limit in canonical state", async () => {
    const { coordinator } = await boot();
    const decision = coordinator.decideQuery("query_limit", {
      turnIndex: 8,
      turnLimit: 8,
      empty: false,
      allowEmpty: false,
      aborted: false,
    });

    expect(decision.accepted).toBe(false);
    expect(decision.reason).toBe("max_turns_exceeded");
    expect(decision.receipt.revisionAfter).toBe(2);
    expect(coordinator.stop.snapshot().decisions.at(-1)?.action).toBe("stop");
  });

  test("cancels query lifecycle when the caller aborts", async () => {
    const { coordinator } = await boot();
    const decision = coordinator.decideQuery("query_abort", {
      turnIndex: 2,
      turnLimit: 10,
      empty: false,
      allowEmpty: false,
      aborted: true,
    });

    expect(decision.accepted).toBe(false);
    expect(decision.reason).toBe("user_cancelled");
    expect(coordinator.query.project().status).toBe("cancelled");
    expect(coordinator.stop.snapshot().decisions.at(-1)?.signal).toBe(
      "explicit_cancel",
    );
  });

  test("default query hook blocks an aborted payload", async () => {
    const { coordinator } = await boot();
    const result = await coordinator.runHooks("query.accept", {
      aborted: true,
      correlation_id: "correlation-abort",
    });

    expect(result.decision).toBe("block");
    expect(result.reason).toBe("query_aborted");
    expect(result.appliedHooks).toEqual(["zyra.query-abort-guard"]);
    expect(result.auditIds).toHaveLength(1);
  });

  test("default query hook permits a live payload", async () => {
    const { coordinator } = await boot();
    const result = await coordinator.runHooks("query.accept", {
      aborted: false,
      correlation_id: "correlation-live",
    });

    expect(result.decision).toBe("continue");
    expect(result.reason).toBe("all_hooks_continued");
    expect(result.appliedHooks).toEqual(["zyra.query-abort-guard"]);
    expect(result.auditIds).toHaveLength(1);
  });

  test("keeps context unchanged while within the configured budget", async () => {
    const { coordinator } = await boot();
    const decision = coordinator.decideContext(10_000, 100_000, false, 0);

    expect(decision.accepted).toBe(false);
    expect(decision.reason).toBe("within_context_budget");
    expect(decision.domain).toBe("context");
    expect(decision.operation).toBe("compact_decision");
  });

  test("requests compaction when explicitly forced", async () => {
    const { coordinator } = await boot();
    const decision = coordinator.decideContext(10_000, 100_000, true, 0);

    expect(decision.accepted).toBe(true);
    expect(decision.reason).toBe("forced_compact");
    expect((coordinator.query.project().control as Record<string, unknown>).pending_compact).toBe(
      true,
    );
  });

  test("requests compaction when characters exceed the hard threshold", async () => {
    const { coordinator } = await boot();
    const decision = coordinator.decideContext(150_000, 100_000, false, 0);

    expect(decision.accepted).toBe(true);
    expect(decision.reason).toBe("context_threshold_exceeded");
    expect(decision.receipt.domain).toBe("context");
  });

  test("permits a later compaction after a previous compact boundary", async () => {
    const { coordinator } = await boot();
    const decision = coordinator.decideContext(150_000, 100_000, true, 1);

    expect(decision.accepted).toBe(true);
    expect(decision.reason).toBe("forced_compact");
    expect((coordinator.query.project().control as Record<string, unknown>).pending_compact).toBe(
      true,
    );
  });

  test("routes provider phases to the provider journal domain", async () => {
    const { coordinator } = await boot();
    const receipt = coordinator.recordRuntimeEvent("provider_request_started", {
      provider: "anthropic",
      model: "claude-sonnet",
      request_id: "provider-request-1",
    });

    expect(receipt.domain).toBe("provider");
    expect(receipt.operation).toBe("provider_request_started");
    expect(receipt.status).toBe("acked");
    expect(coordinator.inventory().correlation_nodes).toBeGreaterThan(0);
  });

  test("routes compact budget phases to the context domain", async () => {
    const { coordinator } = await boot();
    const receipt = coordinator.recordRuntimeEvent("context_budget_warning", {
      estimated_tokens: 900_000,
      maximum_tokens: 1_000_000,
    });

    expect(receipt.domain).toBe("context");
    expect(receipt.operation).toBe("context_budget_warning");
    expect(coordinator.journal.state.context).toBeDefined();
  });

  test("routes tool permission phases to the tool domain", async () => {
    const { coordinator } = await boot();
    const receipt = coordinator.recordRuntimeEvent("tool_permission_checked", {
      tool_name: "read_file",
      decision: "allow",
      tool_call_id: "tool-call-1",
    });

    expect(receipt.domain).toBe("tool");
    expect(receipt.operation).toBe("tool_permission_checked");
    expect(coordinator.journal.state.tool).toBeDefined();
  });

  test("routes ACK and correlation phases to the protocol domain", async () => {
    const { coordinator } = await boot();
    const receipt = coordinator.recordRuntimeEvent("transition_ack_received", {
      transition_id: "transition-external-1",
      ack_sequence: 4,
    });

    expect(receipt.domain).toBe("protocol");
    expect(receipt.operation).toBe("transition_ack_received");
    expect(coordinator.journal.state.protocol).toBeDefined();
  });

  test("uses query as the fallback domain for an unclassified phase", async () => {
    const { coordinator } = await boot();
    const receipt = coordinator.recordRuntimeEvent("reasoning_revised", {
      revision_reason: "tool observation",
    });

    expect(receipt.domain).toBe("query");
    expect(receipt.operation).toBe("reasoning_revised");
    expect(coordinator.journal.state.query).toBeDefined();
  });

  test("advances exactly one journal revision per recorded phase", async () => {
    const { coordinator } = await boot();
    const phases = [
      "reasoning_started",
      "provider_request_started",
      "context_budget_warning",
      "tool_permission_checked",
      "transition_ack_received",
      "session_checkpointed",
    ];
    const revisions = phases.map((phase, index) =>
      coordinator.recordRuntimeEvent(phase, {
        index,
        tool_name: phase.includes("tool") ? "read_file" : "",
      }).revisionAfter,
    );

    expect(revisions).toEqual([2, 3, 4, 5, 6, 7]);
    expect(coordinator.journal.revision).toBe(7);
    expect(coordinator.journal.pending()).toEqual([]);
    expect(coordinator.journal.committed()).toHaveLength(7);
  });

  test("records effectful tool completion through prepare effect commit ACK", async () => {
    const { coordinator } = await boot();
    const receipt = coordinator.recordRuntimeEvent("tool_call_completed", {
      tool_name: "read_file",
      tool_call_id: "tool-call-1",
      ok: true,
      bytes: 512,
    });

    expect(receipt.effect?.terminal).toBe(true);
    expect(receipt.effect?.ok).toBe(true);
    expect(receipt.status).toBe("acked");
    expect(receipt.outboxIds).toHaveLength(1);
    expect(coordinator.journal.undelivered()).toEqual([]);
    expect(coordinator.snapshot().journal.effects).toHaveLength(1);
  });

  test("creates history correlation protocol and framing evidence per transition", async () => {
    const { coordinator } = await boot();
    const before = coordinator.inventory();
    coordinator.recordRuntimeEvent("reasoning_revised", {
      turn_id: "turn-1",
      correlation_id: "correlation-1",
      reason: "tool observation",
    });
    const after = coordinator.inventory();

    expect(Number(after.history_nodes)).toBeGreaterThan(Number(before.history_nodes));
    expect(Number(after.correlation_nodes)).toBeGreaterThan(
      Number(before.correlation_nodes),
    );
    expect(Number(after.protocol_frames)).toBeGreaterThan(
      Number(before.protocol_frames),
    );
    expect(coordinator.protocol.snapshot().transactions.length).toBeGreaterThan(0);
  });

  test("creates a cited compact summary on the real context_compacted phase", async () => {
    const { coordinator } = await boot();
    coordinator.decideContext(200_000, 100_000, true, 0);
    const receipt = coordinator.recordRuntimeEvent("context_compacted", {
      before_tokens: 80_000,
      after_tokens: 20_000,
      compact_id: "compact-1",
      compact_summary: [
        "## Active objective",
        "Complete the governed release.",
        "## Verified tool observations",
        "138 tests passed.",
        "## Open work",
        "Start the stack and run the simulation.",
      ].join("\n"),
    });
    const summary = coordinator.compactSummary.snapshot().summaries.at(-1);

    expect(receipt.domain).toBe("context");
    expect(receipt.effect?.ok).toBe(true);
    expect(summary).toBeDefined();
    expect(summary?.items[0]?.subject).toBe("governed task");
    expect(summary?.items.some((item) => item.kind === "open_loop")).toBe(true);
    expect(summary?.rendered).toContain("138 tests passed");
    expect(summary?.items[0]?.citations).toHaveLength(1);
    expect(summary?.items[0]?.metadata.canonical_owner).toBe("typescript");
    expect((coordinator.query.project().control as Record<string, unknown>).pending_compact).toBe(
      false,
    );
  });

  test("turns provider failure events into recovery observations", async () => {
    const { coordinator } = await boot();
    const before = coordinator.recovery.snapshot().contexts.length;
    coordinator.recordProvider("provider_request_failed", {
      provider: "anthropic",
      model: "claude-sonnet",
      request_id: "request-failed-1",
      error: {
        code: "rate_limit",
        message: "request quota exhausted",
        retryable: true,
      },
    });
    const snapshot = coordinator.recovery.snapshot();

    expect(snapshot.contexts).toHaveLength(before + 1);
    expect(snapshot.contexts.at(-1)?.provider).toBe("anthropic");
    expect(snapshot.contexts.at(-1)?.requestId).toBe("request-failed-1");
  });

  test("finishes durable session state on session_completed", async () => {
    const { coordinator } = await boot();
    const receipt = coordinator.recordRuntimeEvent("session_completed", {
      result_id: "result-1",
      terminal: true,
    });

    expect(receipt.domain).toBe("session");
    expect(receipt.effect?.ok).toBe(true);
    expect(coordinator.session.project().status).toBe("completed");
    expect(coordinator.journal.state.session).toBeDefined();
  });

  test("finishes durable session state on session_failed", async () => {
    const { coordinator } = await boot("run-failed");
    const receipt = coordinator.recordRuntimeEvent("session_failed", {
      error_code: "provider_unavailable",
      terminal: true,
    });

    expect(receipt.domain).toBe("session");
    expect(receipt.effect?.ok).toBe(true);
    expect(coordinator.session.project().status).toBe("failed");
  });

  test("inventory reports changing owner state from live operations", async () => {
    const { coordinator } = await boot();
    coordinator.recordRuntimeEvent("provider_request_started", {
      provider: "anthropic",
      model: "claude-sonnet",
      request_id: "request-1",
    });
    coordinator.recordRuntimeEvent("reasoning_revised", {
      reason: "observation",
    });
    const inventory = coordinator.inventory();

    expect(inventory.revision).toBe(3);
    expect(inventory.committed).toBe(3);
    expect(Number(inventory.history_nodes)).toBeGreaterThanOrEqual(3);
    expect(Number(inventory.correlation_nodes)).toBeGreaterThanOrEqual(3);
    expect(Number(inventory.protocol_frames)).toBeGreaterThanOrEqual(3);
    expect(Number(inventory.telemetry_events)).toBeGreaterThan(0);
  });

  test("captures every owner in a checksummed v6 composite snapshot", async () => {
    const { coordinator } = await boot();
    coordinator.recordRuntimeEvent("reasoning_revised", { reason: "snapshot" });
    const snapshot = coordinator.snapshot();

    expect(snapshot.version).toBe("zyra.e01-runtime/v6");
    expect(snapshot.runId).toBe("run-1");
    expect(snapshot.sessionId).toBe("session-1");
    expect(snapshot.taskId).toBe("task-1");
    expect(snapshot.bootstrapped).toBe(true);
    expect(snapshot.journal.revision).toBe(2);
    expect(snapshot.checksum).toMatch(/^sha256:/);
    expect(snapshot.commands.descriptors).toHaveLength(2);
    expect(snapshot.framing.sessionId).toBe("session-1");
  });

  test("rejects composite checksum tampering before restoring owners", async () => {
    const { coordinator } = await boot();
    const snapshot = coordinator.snapshot();
    snapshot.taskId = "task-tampered";
    const target = runtime("run-2", "session-1", "task-1");

    expect(() => target.restore(snapshot)).toThrow("e01_snapshot_checksum_mismatch");
    expect(target.journal.revision).toBe(0);
    expect(target.inventory().restored).toBe(false);
  });

  test("rejects a valid snapshot for a different session", async () => {
    const { coordinator } = await boot();
    const snapshot = coordinator.snapshot();
    const target = runtime("run-2", "session-other", "task-1");

    expect(() => target.restore(snapshot)).toThrow("e01_snapshot_identity_mismatch");
    expect(target.journal.revision).toBe(0);
  });

  test("rejects a valid snapshot for a different task", async () => {
    const { coordinator } = await boot();
    const snapshot = coordinator.snapshot();
    const target = runtime("run-2", "session-1", "task-other");

    expect(() => target.restore(snapshot)).toThrow("e01_snapshot_identity_mismatch");
    expect(target.journal.revision).toBe(0);
  });

  test("requires restore before bootstrap", async () => {
    const source = (await boot("run-source")).coordinator;
    const target = (await boot("run-target")).coordinator;

    expect(() => target.restore(source.snapshot())).toThrow(
      "e01_restore_must_precede_bootstrap",
    );
    expect(target.journal.restartEpoch).toBe(0);
  });

  test("restores a journal-only snapshot before resume bootstrap", async () => {
    const source = (await boot("run-source")).coordinator;
    source.recordRuntimeEvent("reasoning_revised", { phase: "before-restart" });
    const previousRevision = source.journal.revision;
    const target = runtime("run-target");
    target.restore(source.journal.snapshot());
    const resumed = await target.bootstrap();

    expect(resumed.operation).toBe("resume_bootstrap");
    expect(resumed.revisionBefore).toBe(previousRevision);
    expect(resumed.revisionAfter).toBe(previousRevision + 1);
    expect(target.journal.restartEpoch).toBe(1);
    expect(target.inventory().restored).toBe(true);
  });

  test("restores all composite owners for the same session in a new run", async () => {
    const source = (await boot("run-before")).coordinator;
    source.recordRuntimeEvent("reasoning_revised", {
      phase: "before-restart",
      correlation_id: "correlation-resume",
    });
    source.recordRuntimeEvent("context_budget_warning", {
      estimated_tokens: 900_000,
    });
    const snapshot = source.snapshot();
    const target = runtime("run-after");
    target.restore(snapshot);

    expect(target.journal.restartEpoch).toBe(1);
    expect(target.journal.revision).toBe(snapshot.journal.revision);
    expect(target.history.snapshot().nodes).toHaveLength(snapshot.history.nodes.length);
    expect(target.correlation.snapshot().nodes).toHaveLength(
      snapshot.correlation.nodes.length,
    );
    expect(target.executionPlan.snapshot().plans).toHaveLength(1);
    expect(target.inventory().restored).toBe(true);
  });

  test("resume bootstrap preserves prior owner state and appends one revision", async () => {
    const source = (await boot("run-before")).coordinator;
    source.recordRuntimeEvent("reasoning_revised", { marker: "preserve-me" });
    const snapshot = source.snapshot();
    const target = runtime("run-after");
    target.restore(snapshot);
    const receipt = await target.bootstrap();

    expect(receipt.operation).toBe("resume_bootstrap");
    expect(receipt.revisionBefore).toBe(snapshot.journal.revision);
    expect(receipt.revisionAfter).toBe(snapshot.journal.revision + 1);
    expect(target.journal.state.query).toEqual(source.journal.state.query);
    expect(target.executionPlan.snapshot().plans).toHaveLength(1);
    expect(target.snapshot().bootstrapped).toBe(true);
  });

  test("creates zero overlapping transition IDs after full composite resume", async () => {
    const source = (await boot("run-before")).coordinator;
    for (const phase of [
      "reasoning_started",
      "provider_request_started",
      "tool_permission_checked",
    ]) {
      source.recordRuntimeEvent(phase, {
        tool_name: phase.includes("tool") ? "read_file" : "",
      });
    }
    const oldIds = committedIds(source);
    const target = runtime("run-after");
    target.restore(source.snapshot());
    await target.bootstrap();
    for (const phase of [
      "reasoning_revised",
      "context_budget_warning",
      "transition_ack_received",
    ]) {
      target.recordRuntimeEvent(phase, {});
    }
    const newIds = [...committedIds(target)].filter((value) => !oldIds.has(value));
    const overlap = newIds.filter((value) => oldIds.has(value));

    expect(newIds).toHaveLength(4);
    expect(overlap).toEqual([]);
    expect(target.journal.restartEpoch).toBe(1);
    expect(target.journal.revision).toBe(source.journal.revision + 4);
  });

  test("does not replay committed effects while resuming a lost bootstrap ACK", async () => {
    const source = (await boot("run-before")).coordinator;
    const completed = source.recordRuntimeEvent("tool_call_completed", {
      tool_name: "read_file",
      tool_call_id: "tool-call-before",
      bytes: 64,
    });
    expect(completed.effect?.ok).toBe(true);
    const priorEffects = source.snapshot().journal.effects.length;
    const priorOutbox = source.snapshot().journal.outbox.length;
    const target = runtime("run-after");
    target.restore(source.snapshot());
    await target.bootstrap();

    expect(target.snapshot().journal.effects).toHaveLength(priorEffects);
    expect(target.snapshot().journal.outbox).toHaveLength(priorOutbox + 1);
    expect(target.journal.state.tool).toEqual(source.journal.state.tool);
    expect(target.journal.undelivered()).toEqual([]);
  });

  test("disconnecting the abort hook changes observable admission behavior", async () => {
    const guarded = (await boot("run-guarded")).coordinator;
    const guardedResult = await guarded.runHooks("query.accept", {
      aborted: true,
    });
    const disconnected = (await boot("run-disconnected")).coordinator;
    disconnected.hooks.unregister("zyra.query-abort-guard");
    const disconnectedResult = await disconnected.runHooks("query.accept", {
      aborted: true,
    });

    expect(guardedResult.decision).toBe("block");
    expect(disconnectedResult.decision).toBe("continue");
    expect(disconnectedResult.appliedHooks).toEqual([]);
  });

  test("disabling the default route changes provider selection behavior", async () => {
    const { coordinator } = await boot();
    const before = coordinator.providerRouting.decide({
      requestId: "route-before",
      sessionId: "session-1",
      requiredCapabilities: ["text", "tools"],
      privacyClass: "internal",
      estimatedInputTokens: 10_000,
      requestedOutputTokens: 2_000,
      maximumCost: null,
      maximumLatencyMilliseconds: null,
      preferredProviderIds: [],
      excludedRouteIds: [],
      stickyRouteId: null,
      allowDegraded: false,
      metadata: {},
    });
    coordinator.providerRouting.forceStatus("anthropic-default", "disabled");

    expect(before.routeId).toBe("anthropic-default");
    expect(() =>
      coordinator.providerRouting.decide({
        requestId: "route-after",
        sessionId: "session-1",
        requiredCapabilities: ["text", "tools"],
        privacyClass: "internal",
        estimatedInputTokens: 10_000,
        requestedOutputTokens: 2_000,
        maximumCost: null,
        maximumLatencyMilliseconds: null,
        preferredProviderIds: [],
        excludedRouteIds: [],
        stickyRouteId: null,
        allowDegraded: false,
        metadata: {},
      }),
    ).toThrow("no_eligible_provider_route");
  });

  test("rejects a recomputed snapshot whose nested journal belongs to another session", async () => {
    const { coordinator } = await boot();
    const snapshot = coordinator.snapshot();
    snapshot.journal.sessionId = "session-forged";
    const journalUnsigned = { ...snapshot.journal } as Partial<typeof snapshot.journal>;
    delete journalUnsigned.checksum;
    snapshot.journal.checksum = digest(journalUnsigned);
    recomputeCoordinatorChecksum(snapshot);
    const target = runtime("run-after");

    expect(() => target.restore(snapshot)).toThrow("session_mismatch");
    expect(target.journal.revision).toBe(0);
  });
});
