import { describe, expect, test } from "bun:test";

import type { JsonValue } from "../../src/contracts.js";

import {
  SessionCorrelationRuntime,
  type CorrelationNodeKind,
} from "../../src/session/correlation-runtime.js";
import { SessionHistoryRuntime } from "../../src/session/history-runtime.js";
import {
  CompactRestoreRuntime,
  type RestorePolicy,
} from "../../src/compact/restore-runtime.js";
import {
  Journal,
  command,
  digest,
  identity,
  type O,
  type TransitionCommand,
} from "../../src/e01/kernel.js";

class ManualClock {
  constructor(private value = 1_000) {}

  now(): number {
    return this.value;
  }

  advance(milliseconds: number): void {
    this.value += milliseconds;
  }
}

class SequenceIds {
  private sequence = 0;

  constructor(private readonly prefix: string) {}

  next(namespace: string): string {
    this.sequence += 1;
    return `${this.prefix}-${namespace}-${this.sequence}`;
  }
}

function correlationRuntime(prefix = "correlation"): {
  runtime: SessionCorrelationRuntime;
  clock: ManualClock;
} {
  const clock = new ManualClock();
  return {
    clock,
    runtime: new SessionCorrelationRuntime({
      clock,
      ids: new SequenceIds(prefix),
    }),
  };
}

function openCorrelation(
  runtime: SessionCorrelationRuntime,
  externalId: string,
  options: {
    kind?: CorrelationNodeKind;
    sessionId?: string;
    runId?: string;
    correlationId?: string;
    parentNodeId?: string | null;
    sequenceStart?: number | null;
    payload?: unknown;
    metadata?: Record<string, null | boolean | number | string>;
  } = {},
) {
  return runtime.open({
    kind: options.kind ?? "event",
    externalId,
    sessionId: options.sessionId ?? "session-1",
    runId: options.runId ?? "run-1",
    correlationId: options.correlationId ?? "correlation-1",
    parentNodeId: options.parentNodeId,
    sequenceStart: options.sequenceStart,
    payload: options.payload ?? { externalId },
    metadata: options.metadata,
  });
}

function appendHistory(
  history: SessionHistoryRuntime,
  role: "system" | "user" | "assistant" | "tool",
  content: JsonValue,
  turnId: string | null,
  toolCallId: string | null = null,
) {
  return history.append({
    role,
    content,
    turnId,
    toolCallId,
    metadata: { source: "e01-behavior" },
    createdAt: "2026-07-15T00:00:00.000Z",
  });
}

const WORKSPACE = "G:\\agent-zoo\\zyra";

function restorePolicy(
  overrides: Partial<RestorePolicy> = {},
): Partial<RestorePolicy> {
  return {
    workspaceRoot: WORKSPACE,
    allowedRoots: [WORKSPACE],
    deniedPatterns: ["(?:^|/)\\.env(?:$|/)", "(?:^|/)secrets?(?:$|/)"],
    maximumTotalTokens: 1_000,
    maximumFileTokens: 300,
    maximumSkillTokens: 300,
    maximumMemoryTokens: 300,
    maximumAgentTokens: 300,
    maximumFiles: 8,
    maximumSkills: 4,
    allowSymlinksOutsideWorkspace: false,
    ...overrides,
  };
}

function inlineCandidate(
  runtime: CompactRestoreRuntime,
  candidateId: string,
  content: string,
  options: {
    kind?: "file" | "plan" | "skill" | "agent" | "memory";
    priority?: number;
    required?: boolean;
    name?: string;
  } = {},
) {
  return runtime.discover({
    candidateId,
    kind: options.kind ?? "file",
    name: options.name ?? candidateId,
    sourceId: `source:${candidateId}`,
    priority: options.priority,
    required: options.required,
    content,
    metadata: { test_case: candidateId },
  });
}

function transition(
  journal: Journal,
  payload: O = { value: 1 },
  options: {
    domain?: string;
    operation?: string;
    readSet?: string[];
    writeSet?: string[];
    requiresEffect?: boolean;
    identity?: ReturnType<typeof identity>;
  } = {},
): TransitionCommand {
  return command(
    journal,
    options.domain ?? "session",
    options.operation ?? "update",
    payload,
    {
      identity: options.identity,
      readSet: options.readSet,
      writeSet: options.writeSet,
      requiresEffect: options.requiresEffect,
    },
  );
}

describe("session correlation custody", () => {
  test("opens a scoped root with a stable payload digest", () => {
    const { runtime } = correlationRuntime("root");
    const node = openCorrelation(runtime, "query-1", {
      kind: "query",
      sequenceStart: 7,
      payload: { prompt: "repair the runtime" },
      metadata: { lane: "interactive" },
    });

    expect(node.nodeId).toBe("root-correlation-node-1");
    expect(node.status).toBe("open");
    expect(node.restartEpoch).toBe(0);
    expect(node.sequenceStart).toBe(7);
    expect(node.payloadDigest).toMatch(/^[a-f0-9]{64}$/);
    expect(node.metadata.lane).toBe("interactive");
    expect(runtime.findByExternalId("query", "query-1")).toEqual(node);
  });

  test("deduplicates an identical external key without creating a second node", () => {
    const { runtime } = correlationRuntime("open-idempotent");
    const first = openCorrelation(runtime, "turn-1", {
      kind: "turn",
      payload: { query: "same" },
    });
    const repeated = openCorrelation(runtime, "turn-1", {
      kind: "turn",
      payload: { query: "same" },
    });

    expect(repeated).toEqual(first);
    expect(runtime.trace("correlation-1").nodes).toHaveLength(1);
  });

  test("rejects external key reuse with a different payload", () => {
    const { runtime } = correlationRuntime("open-conflict");
    openCorrelation(runtime, "tool-1", {
      kind: "tool_call",
      payload: { path: "a.ts" },
    });

    expect(() =>
      openCorrelation(runtime, "tool-1", {
        kind: "tool_call",
        payload: { path: "b.ts" },
      }),
    ).toThrow("correlation_external_id_conflict");
  });

  test("creates an explicit contains edge for a child node", () => {
    const { runtime } = correlationRuntime("contains");
    const query = openCorrelation(runtime, "query-1", { kind: "query" });
    const turn = openCorrelation(runtime, "turn-1", {
      kind: "turn",
      parentNodeId: query.nodeId,
    });
    const trace = runtime.trace("correlation-1");

    expect(turn.parentNodeId).toBe(query.nodeId);
    expect(trace.edges).toHaveLength(1);
    expect(trace.edges[0]?.kind).toBe("contains");
    expect(trace.edges[0]?.sourceNodeId).toBe(query.nodeId);
    expect(trace.edges[0]?.targetNodeId).toBe(turn.nodeId);
  });

  test("rejects a parent from another correlation scope", () => {
    const { runtime } = correlationRuntime("parent-scope");
    const parent = openCorrelation(runtime, "query-other", {
      kind: "query",
      correlationId: "correlation-other",
    });

    expect(() =>
      openCorrelation(runtime, "turn-1", {
        kind: "turn",
        parentNodeId: parent.nodeId,
      }),
    ).toThrow("correlation_parent_scope_mismatch");
  });

  test("annotates only at the expected node revision", () => {
    const { runtime } = correlationRuntime("annotate");
    const node = openCorrelation(runtime, "event-1");
    const updated = runtime.annotate(node.nodeId, 1, {
      worker_id: "worker-7",
      route: "local",
    });

    expect(updated.revision).toBe(2);
    expect(updated.metadata.worker_id).toBe("worker-7");
    expect(() => runtime.annotate(node.nodeId, 1, { stale: true })).toThrow(
      "correlation_revision_conflict",
    );
    expect(runtime.getNode(node.nodeId).metadata.stale).toBeUndefined();
  });

  test("closes a node idempotently with monotonic sequence bounds", () => {
    const { runtime, clock } = correlationRuntime("close");
    const node = openCorrelation(runtime, "provider-1", {
      kind: "provider_request",
      sequenceStart: 12,
    });
    clock.advance(25);
    const closed = runtime.close({
      nodeId: node.nodeId,
      status: "completed",
      sequenceEnd: 18,
      result: { model: "claude", stop: "end_turn" },
      metadata: { latency_ms: 25 },
    });
    const repeated = runtime.close({
      nodeId: node.nodeId,
      status: "completed",
      sequenceEnd: 18,
      result: { model: "claude", stop: "end_turn" },
    });

    expect(closed.status).toBe("completed");
    expect(closed.closedAt).toBe(1_025);
    expect(closed.sequenceEnd).toBe(18);
    expect(repeated.resultDigest).toBe(closed.resultDigest);
    expect(repeated.revision).toBe(closed.revision);
  });

  test("rejects a terminal close conflict and sequence regression", () => {
    const { runtime } = correlationRuntime("close-conflict");
    const node = openCorrelation(runtime, "reasoning-1", {
      kind: "reasoning",
      sequenceStart: 5,
    });

    expect(() =>
      runtime.close({
        nodeId: node.nodeId,
        status: "completed",
        sequenceEnd: 4,
        result: {},
      }),
    ).toThrow("correlation_sequence_regression");

    runtime.close({
      nodeId: node.nodeId,
      status: "failed",
      sequenceEnd: 6,
      result: { error: "provider_timeout" },
    });
    expect(() =>
      runtime.close({
        nodeId: node.nodeId,
        status: "completed",
        sequenceEnd: 6,
        result: { error: "provider_timeout" },
      }),
    ).toThrow("correlation_close_conflict");
  });

  test("links causal nodes once and returns the existing edge on retry", () => {
    const { runtime } = correlationRuntime("link-idempotent");
    const source = openCorrelation(runtime, "permission-1", {
      kind: "permission",
    });
    const target = openCorrelation(runtime, "tool-1", { kind: "tool_call" });
    const first = runtime.link({
      kind: "causes",
      sourceNodeId: source.nodeId,
      targetNodeId: target.nodeId,
      metadata: { decision: "allow" },
    });
    const repeated = runtime.link({
      kind: "causes",
      sourceNodeId: source.nodeId,
      targetNodeId: target.nodeId,
      metadata: { decision: "allow" },
    });

    expect(repeated.edgeId).toBe(first.edgeId);
    expect(runtime.trace("correlation-1").edges).toHaveLength(1);
  });

  test("rejects causal cycles before they enter the graph", () => {
    const { runtime } = correlationRuntime("cycle");
    const first = openCorrelation(runtime, "first");
    const second = openCorrelation(runtime, "second");
    const third = openCorrelation(runtime, "third");
    runtime.link({ kind: "causes", sourceNodeId: first.nodeId, targetNodeId: second.nodeId });
    runtime.link({ kind: "causes", sourceNodeId: second.nodeId, targetNodeId: third.nodeId });

    expect(() =>
      runtime.link({
        kind: "retries",
        sourceNodeId: third.nodeId,
        targetNodeId: first.nodeId,
      }),
    ).toThrow("correlation_causation_cycle");
    expect(runtime.audit().valid).toBe(true);
  });

  test("rejects self edges and cross-session edges", () => {
    const { runtime } = correlationRuntime("edge-scope");
    const local = openCorrelation(runtime, "local");
    const foreign = openCorrelation(runtime, "foreign", {
      sessionId: "session-2",
      correlationId: "correlation-2",
    });

    expect(() =>
      runtime.link({
        kind: "observes",
        sourceNodeId: local.nodeId,
        targetNodeId: local.nodeId,
      }),
    ).toThrow("correlation_self_edge");
    expect(() =>
      runtime.link({
        kind: "observes",
        sourceNodeId: local.nodeId,
        targetNodeId: foreign.nodeId,
      }),
    ).toThrow("correlation_cross_session_edge");
  });

  test("walks ancestors descendants and a shortest causal path", () => {
    const { runtime } = correlationRuntime("walk");
    const query = openCorrelation(runtime, "query", { kind: "query" });
    const turn = openCorrelation(runtime, "turn", {
      kind: "turn",
      parentNodeId: query.nodeId,
    });
    const tool = openCorrelation(runtime, "tool", {
      kind: "tool_call",
      parentNodeId: turn.nodeId,
    });
    const effect = openCorrelation(runtime, "effect", {
      kind: "tool_effect",
      parentNodeId: tool.nodeId,
    });

    expect(runtime.descendants(query.nodeId).map((node) => node.externalId)).toEqual([
      "turn",
      "tool",
      "effect",
    ]);
    expect(runtime.ancestors(effect.nodeId).map((node) => node.externalId)).toEqual([
      "query",
      "turn",
      "tool",
    ]);
    expect(runtime.shortestPath(query.nodeId, effect.nodeId).map((node) => node.externalId)).toEqual([
      "query",
      "turn",
      "tool",
      "effect",
    ]);
    expect(runtime.shortestPath(effect.nodeId, query.nodeId)).toEqual([]);
  });

  test("projects an auditable trace with open and terminal nodes", () => {
    const { runtime } = correlationRuntime("trace");
    const query = openCorrelation(runtime, "query", {
      kind: "query",
      sequenceStart: 2,
    });
    const event = openCorrelation(runtime, "event", {
      parentNodeId: query.nodeId,
      sequenceStart: 3,
    });
    runtime.close({
      nodeId: event.nodeId,
      status: "completed",
      sequenceEnd: 9,
      result: { persisted: true },
    });
    const trace = runtime.trace("correlation-1");

    expect(trace.roots.map((node) => node.nodeId)).toEqual([query.nodeId]);
    expect(trace.openNodeIds).toEqual([query.nodeId]);
    expect(trace.sequenceStart).toBe(2);
    expect(trace.sequenceEnd).toBe(9);
    expect(trace.digest).toMatch(/^[a-f0-9]{64}$/);
    expect(runtime.audit()).toEqual({
      valid: true,
      orphanNodeIds: [],
      missingEdgeEndpointIds: [],
      cyclicCausationEdgeIds: [],
      invalidSequenceNodeIds: [],
      duplicateExternalKeys: [],
      openTerminalNodeIds: [],
    });
  });

  test("restores a graph before opening nodes in a new restart epoch", () => {
    const { runtime } = correlationRuntime("before-restart");
    const root = openCorrelation(runtime, "query", { kind: "query" });
    const child = openCorrelation(runtime, "turn", {
      kind: "turn",
      parentNodeId: root.nodeId,
    });
    const snapshot = runtime.snapshot();
    const restored = correlationRuntime("after-restart").runtime;
    restored.restore(snapshot);
    const recovery = openCorrelation(restored, "recovery", {
      kind: "recovery",
      parentNodeId: child.nodeId,
      payload: { reason: "process_restart" },
    });

    expect(restored.getNode(root.nodeId).restartEpoch).toBe(0);
    expect(recovery.restartEpoch).toBe(1);
    expect(restored.trace("correlation-1").nodes).toHaveLength(3);
    expect(restored.audit().valid).toBe(true);
  });

  test("rejects a checksum-tampered graph snapshot", () => {
    const { runtime } = correlationRuntime("snapshot-tamper");
    openCorrelation(runtime, "query", { kind: "query" });
    const snapshot = runtime.snapshot();
    snapshot.nodes[0]!.externalId = "rewritten-after-checksum";

    expect(() => correlationRuntime("restore-target").runtime.restore(snapshot)).toThrow(
      "correlation_snapshot_checksum_mismatch",
    );
  });

  test("enforces configured graph capacity", () => {
    const runtime = new SessionCorrelationRuntime({
      ids: new SequenceIds("capacity"),
      maximumNodes: 1,
      maximumEdges: 0,
    });
    const first = openCorrelation(runtime, "first");

    expect(() => openCorrelation(runtime, "second")).toThrow(
      "correlation_node_limit_exceeded",
    );
    expect(() => {
      const otherRuntime = new SessionCorrelationRuntime({
        ids: new SequenceIds("edge-capacity"),
        maximumNodes: 2,
        maximumEdges: 0,
      });
      const source = openCorrelation(otherRuntime, "source");
      const target = openCorrelation(otherRuntime, "target");
      otherRuntime.link({
        kind: "observes",
        sourceNodeId: source.nodeId,
        targetNodeId: target.nodeId,
      });
    }).toThrow("correlation_edge_limit_exceeded");
    expect(runtime.getNode(first.nodeId).externalId).toBe("first");
  });
});

describe("session history branch and compact custody", () => {
  test("appends a parent-linked main transcript with monotonic sequence", () => {
    const history = new SessionHistoryRuntime();
    const user = appendHistory(history, "user", "inspect runtime", "turn-1");
    const assistant = appendHistory(history, "assistant", "calling tool", "turn-1");
    const tool = appendHistory(history, "tool", { ok: true }, "turn-1", "tool-1");

    expect(user.parentNodeId).toBeNull();
    expect(assistant.parentNodeId).toBe(user.nodeId);
    expect(tool.parentNodeId).toBe(assistant.nodeId);
    expect(history.transcript().map((node) => node.sequence)).toEqual([1, 2, 3]);
    expect(history.project().head_node_id).toBe(tool.nodeId);
  });

  test("edits a middle node and rewires its visible child", () => {
    const history = new SessionHistoryRuntime();
    const first = appendHistory(history, "user", "old request", "turn-1");
    const child = appendHistory(history, "assistant", "old answer", "turn-1");
    const replacement = history.edit(first.nodeId, "corrected request", {
      reason: "user_edit",
    });
    const transcript = history.transcript();
    const snapshot = history.snapshot();

    expect(replacement.supersedesNodeId).toBe(first.nodeId);
    expect(replacement.content).toBe("corrected request");
    expect(transcript.map((node) => node.nodeId)).toEqual([
      replacement.nodeId,
      child.nodeId,
    ]);
    expect(transcript[1]?.parentNodeId).toBe(replacement.nodeId);
    expect(snapshot.nodes.find((node) => node.nodeId === first.nodeId)?.state).toBe(
      "superseded",
    );
  });

  test("updates branch head when editing the last visible node", () => {
    const history = new SessionHistoryRuntime();
    const original = appendHistory(history, "assistant", "draft", "turn-1");
    const replacement = history.edit(original.nodeId, "final", {
      reviewed: true,
    });

    expect(history.project().head_node_id).toBe(replacement.nodeId);
    expect(history.transcript()).toHaveLength(1);
    expect(history.transcript()[0]?.metadata.reviewed).toBe(true);
  });

  test("cannot edit a node owned by an inactive branch", () => {
    const history = new SessionHistoryRuntime();
    const main = appendHistory(history, "user", "main", "turn-main");
    const branch = history.fork("experiment", main.nodeId);
    const experimental = appendHistory(history, "assistant", "branch answer", "turn-branch");
    history.switchBranch("main");

    expect(() => history.edit(experimental.nodeId, "illegal edit")).toThrow(
      "history edit requires active branch ownership",
    );
    expect(history.project().active_branch_id).toBe("main");
    expect(history.snapshot().branches.find((item) => item.branchId === branch.branchId)).toBeDefined();
  });

  test("forks from the active head and diverges without mutating main", () => {
    const history = new SessionHistoryRuntime();
    const root = appendHistory(history, "user", "shared root", "turn-1");
    const mainAnswer = appendHistory(history, "assistant", "main answer", "turn-1");
    const branch = history.fork("alternate", root.nodeId);
    const alternate = appendHistory(history, "assistant", "alternate answer", "turn-2");

    expect(branch.parentBranchId).toBe("main");
    expect(branch.forkNodeId).toBe(root.nodeId);
    expect(history.transcript().map((node) => node.nodeId)).toEqual([
      root.nodeId,
      alternate.nodeId,
    ]);
    history.switchBranch("main");
    expect(history.transcript().map((node) => node.nodeId)).toEqual([
      root.nodeId,
      mainAnswer.nodeId,
    ]);
  });

  test("forks a child branch from an ancestor lineage node", () => {
    const history = new SessionHistoryRuntime();
    const root = appendHistory(history, "system", "policy", null);
    history.fork("first", root.nodeId);
    const first = appendHistory(history, "user", "first branch", "turn-1");
    const secondBranch = history.fork("nested", root.nodeId);

    expect(secondBranch.forkNodeId).toBe(root.nodeId);
    expect(history.transcript().map((node) => node.nodeId)).toEqual([root.nodeId]);
    expect(first.branchId).not.toBe(secondBranch.branchId);
  });

  test("rejects a fork point outside the active branch lineage", () => {
    const history = new SessionHistoryRuntime();
    const root = appendHistory(history, "user", "root", "turn-1");
    history.fork("left", root.nodeId);
    const leftOnly = appendHistory(history, "assistant", "left", "turn-left");
    history.switchBranch("main");
    history.fork("right", root.nodeId);

    expect(() => history.fork("illegal", leftOnly.nodeId)).toThrow(
      "fork node is outside active branch lineage",
    );
  });

  test("compacts a contiguous prefix and preserves following visible nodes", () => {
    const history = new SessionHistoryRuntime();
    const first = appendHistory(history, "user", "large prompt", "turn-1");
    const second = appendHistory(history, "assistant", "analysis", "turn-1");
    const following = appendHistory(history, "tool", "tool output", "turn-1", "tool-1");
    const boundary = history.compact(
      [first.nodeId, second.nodeId],
      "The user requested an inspection and the assistant started analysis.",
    );
    const transcript = history.transcript();

    expect(boundary.compactedNodeIds).toEqual([first.nodeId, second.nodeId]);
    expect(transcript).toHaveLength(2);
    expect(transcript[0]?.nodeId).toBe(boundary.summaryNodeId);
    expect(transcript[1]?.nodeId).toBe(following.nodeId);
    expect(transcript[1]?.parentNodeId).toBe(boundary.summaryNodeId);
  });

  test("rejects an empty or noncontiguous compact selection", () => {
    const history = new SessionHistoryRuntime();
    const first = appendHistory(history, "user", "one", "turn-1");
    appendHistory(history, "assistant", "two", "turn-1");
    const third = appendHistory(history, "tool", "three", "turn-1", "tool-1");

    expect(() => history.compact([], "nothing")).toThrow(
      "history compaction selection is empty",
    );
    expect(() => history.compact([first.nodeId, third.nodeId], "gap")).toThrow(
      "history compaction selection must be contiguous",
    );
    expect(history.project().boundary_count).toBe(0);
  });

  test("rejects compaction that crosses branch ownership", () => {
    const history = new SessionHistoryRuntime();
    const main = appendHistory(history, "user", "main", "turn-main");
    history.fork("branch", main.nodeId);
    const child = appendHistory(history, "assistant", "child", "turn-child");

    expect(() => history.compact([main.nodeId, child.nodeId], "cross branch")).toThrow(
      "history compaction cannot span branches",
    );
  });

  test("chains compact boundaries in creation order", () => {
    const history = new SessionHistoryRuntime();
    const one = appendHistory(history, "user", "one", "turn-1");
    const two = appendHistory(history, "assistant", "two", "turn-1");
    const firstBoundary = history.compact([one.nodeId, two.nodeId], "first summary");
    const three = appendHistory(history, "user", "three", "turn-2");
    const four = appendHistory(history, "assistant", "four", "turn-2");
    const secondBoundary = history.compact([three.nodeId, four.nodeId], "second summary");

    expect(firstBoundary.previousBoundaryId).toBeNull();
    expect(secondBoundary.previousBoundaryId).toBe(firstBoundary.boundaryId);
    expect(history.project().boundary_count).toBe(2);
  });

  test("searches only visible transcript content and metadata", () => {
    const history = new SessionHistoryRuntime();
    const first = appendHistory(history, "user", "repair provider timeout", "turn-1");
    appendHistory(history, "assistant", "inspect retry policy", "turn-1");
    history.edit(first.nodeId, "repair credential refresh", {
      label: "authentication",
    });

    expect(history.search("timeout")).toEqual([]);
    expect(history.search("credential")).toHaveLength(1);
    expect(history.search("authentication", 1)[0]?.metadata.label).toBe(
      "authentication",
    );
    expect(history.search("repair", 0)).toEqual([]);
  });

  test("round-trips branch history and resumes the sequence", () => {
    const history = new SessionHistoryRuntime();
    const root = appendHistory(history, "system", "system policy", null);
    const user = appendHistory(history, "user", "request", "turn-1");
    const branch = history.fork("resume", user.nodeId);
    appendHistory(history, "assistant", "before restart", "turn-1");
    const snapshot = history.snapshot();
    const restored = new SessionHistoryRuntime();
    restored.restore(snapshot);
    const after = appendHistory(restored, "assistant", "after restart", "turn-2");

    expect(restored.project().active_branch_id).toBe(branch.branchId);
    expect(restored.transcript()[0]?.nodeId).toBe(root.nodeId);
    expect(after.sequence).toBe(snapshot.sequence + 1);
    expect(restored.project().revision).toBe(snapshot.revision + 1);
  });

  test("rejects a checksum-tampered history snapshot", () => {
    const history = new SessionHistoryRuntime();
    appendHistory(history, "user", "immutable", "turn-1");
    const snapshot = history.snapshot();
    snapshot.nodes[0]!.content = "tampered";

    expect(() => new SessionHistoryRuntime().restore(snapshot)).toThrow(
      "session history snapshot checksum mismatch",
    );
  });

  test("routes module actions into real history state", () => {
    const history = new SessionHistoryRuntime();
    const appended = history.history_module({
      action: "append",
      role: "user",
      content: "module request",
      turn_id: "turn-module",
      metadata: { entry: "command" },
    });
    const searched = history.history_module({
      action: "search",
      query: "module",
      maximum: 5,
    });

    expect(appended.role).toBe("user");
    expect(Array.isArray(searched.nodes)).toBe(true);
    expect(history.project().visible_node_count).toBe(1);
  });
});

describe("compact restore selection and path custody", () => {
  test("allows a non-file candidate without weakening file roots", () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    const memory = inlineCandidate(runtime, "memory-inline", "durable preference", {
      kind: "memory",
      priority: 50,
    });

    expect(memory.path).toBeNull();
    expect(memory.state).toBe("allowed");
    expect(memory.reason).toBe("non_file_candidate");
    expect(runtime.select()[0]?.attachmentKind).toBe("memory");
  });

  test("allows a normalized path inside the workspace root", () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    const candidate = runtime.discover({
      candidateId: "inside-file",
      kind: "file",
      name: "runtime plan",
      path: "docs/plans/runtime.md",
      sourceId: "workspace",
      content: "restore this plan",
    });

    expect(candidate.state).toBe("allowed");
    expect(candidate.reason).toBe("allowed_root");
    expect(runtime.select()).toHaveLength(1);
  });

  test("denies traversal outside every allowed root", () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    const candidate = runtime.discover({
      candidateId: "outside-file",
      kind: "file",
      name: "outside",
      path: "..\\claude-code-best\\package.json",
      sourceId: "workspace",
      content: "must not restore",
    });

    expect(candidate.state).toBe("denied");
    expect(candidate.reason).toContain("outside_allowed_roots");
    expect(runtime.select()).toEqual([]);
  });

  test("denies configured secret path patterns", () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    const env = runtime.discover({
      candidateId: "env-file",
      kind: "file",
      name: ".env",
      path: ".env",
      sourceId: "workspace",
      content: "TOKEN=secret",
    });
    const secret = runtime.discover({
      candidateId: "secret-file",
      kind: "file",
      name: "credential",
      path: "secrets/provider.txt",
      sourceId: "workspace",
      content: "credential",
    });

    expect(env.state).toBe("denied");
    expect(secret.state).toBe("denied");
    expect(runtime.toAttachmentCandidates()).toEqual([]);
  });

  test("deduplicates discovery by candidate identity", () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    const first = inlineCandidate(runtime, "stable-candidate", "first content");
    const repeated = inlineCandidate(runtime, "stable-candidate", "changed content");

    expect(repeated).toEqual(first);
    expect(repeated.content).toBe("first content");
    expect(runtime.snapshot().candidates).toHaveLength(1);
    expect(runtime.snapshot().receipts).toHaveLength(1);
  });

  test("provides normalized content and records its digest", () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    runtime.discover({
      candidateId: "deferred-content",
      kind: "plan",
      name: "plan",
      sourceId: "artifact:plan-1",
    });
    const provided = runtime.provideContent(
      "deferred-content",
      "line one\r\nline two\r\n",
    );

    expect(provided.state).toBe("read");
    expect(provided.content).toBe("line one\nline two\n");
    expect(provided.sourceDigest).toMatch(/^sha256:/);
    expect(provided.estimatedTokens).toBeGreaterThan(0);
    expect(runtime.select()[0]?.content).toBe("line one\nline two\n");
  });

  test("cannot provide content after path policy denied a candidate", () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    runtime.discover({
      candidateId: "denied-content",
      kind: "file",
      name: "denied",
      path: "..\\outside.txt",
      sourceId: "workspace",
    });

    expect(() => runtime.provideContent("denied-content", "bypass")).toThrow(
      "restore candidate is denied",
    );
  });

  test("reads a file through the injected reader and stores canonical metadata", async () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    runtime.discover({
      candidateId: "reader-file",
      kind: "file",
      name: "reader",
      path: "docs/runtime.md",
      sourceId: "workspace",
    });
    const read = await runtime.readCandidate("reader-file", {
      async read(path) {
        expect(path.replaceAll("\\", "/")).toEndWith("/docs/runtime.md");
        return {
          content: "canonical runtime contract",
          canonicalPath: `${WORKSPACE}\\docs\\runtime.md`,
          sizeBytes: 26,
        };
      },
    });

    expect(read.state).toBe("read");
    expect(read.metadata.size_bytes).toBe(26);
    expect(read.path?.replaceAll("\\", "/")).toEndWith("/docs/runtime.md");
  });

  test("rejects a reader that resolves a symlink outside the workspace", async () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    runtime.discover({
      candidateId: "escaped-reader",
      kind: "file",
      name: "link",
      path: "docs/link.md",
      sourceId: "workspace",
    });

    await expect(
      runtime.readCandidate("escaped-reader", {
        async read() {
          return {
            content: "external",
            canonicalPath: "G:\\agent-zoo\\claude-code-best\\README.md",
            sizeBytes: 8,
          };
        },
      }),
    ).rejects.toThrow("outside_allowed_roots");
    expect(runtime.snapshot().candidates[0]?.state).toBe("denied");
  });

  test("persists a reader failure without inventing content", async () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    runtime.discover({
      candidateId: "missing-reader",
      kind: "file",
      name: "missing",
      path: "docs/missing.md",
      sourceId: "workspace",
    });

    await expect(
      runtime.readCandidate("missing-reader", {
        async read() {
          throw new Error("ENOENT: missing runtime file");
        },
      }),
    ).rejects.toThrow("ENOENT");
    const failed = runtime.snapshot().candidates[0];
    expect(failed?.state).toBe("failed");
    expect(failed?.content).toBeNull();
    expect(runtime.select()).toEqual([]);
  });

  test("selects higher priority and required candidates first", () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    inlineCandidate(runtime, "low", "low priority", { priority: 1 });
    inlineCandidate(runtime, "high", "high priority", { priority: 100 });
    inlineCandidate(runtime, "required", "required context", {
      priority: -100,
      required: true,
    });
    const selected = runtime.select();

    expect(selected.map((item) => item.name)).toEqual([
      "required",
      "high",
      "low",
    ]);
  });

  test("drops duplicate source content after selecting the first candidate", () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    inlineCandidate(runtime, "first-copy", "identical content", { priority: 20 });
    inlineCandidate(runtime, "second-copy", "identical content", { priority: 10 });
    const selected = runtime.select();
    const snapshot = runtime.snapshot();

    expect(selected).toHaveLength(1);
    expect(selected[0]?.name).toBe("first-copy");
    expect(snapshot.candidates.find((item) => item.candidateId === "second-copy")?.state).toBe(
      "dropped",
    );
    expect(snapshot.candidates.find((item) => item.candidateId === "second-copy")?.reason).toBe(
      "duplicate_source_digest",
    );
  });

  test("enforces file and skill count limits independently", () => {
    const runtime = new CompactRestoreRuntime(
      restorePolicy({ maximumFiles: 1, maximumSkills: 1 }),
    );
    inlineCandidate(runtime, "file-one", "file one", { kind: "file", priority: 4 });
    inlineCandidate(runtime, "file-two", "file two", { kind: "file", priority: 3 });
    inlineCandidate(runtime, "skill-one", "skill one", { kind: "skill", priority: 2 });
    inlineCandidate(runtime, "skill-two", "skill two", { kind: "skill", priority: 1 });
    const selected = runtime.select();
    const dropped = runtime.snapshot().candidates.filter((item) => item.state === "dropped");

    expect(selected.map((item) => item.name)).toEqual(["file-one", "skill-one"]);
    expect(dropped).toHaveLength(2);
    expect(dropped.every((item) => item.reason === "kind_count_limit")).toBe(true);
  });

  test("truncates an oversized attachment at its kind token budget", () => {
    const runtime = new CompactRestoreRuntime(
      restorePolicy({ maximumMemoryTokens: 12, maximumTotalTokens: 100 }),
    );
    inlineCandidate(runtime, "large-memory", "memory ".repeat(200), {
      kind: "memory",
    });
    const attachment = runtime.select()[0];

    expect(attachment?.truncated).toBe(true);
    expect(attachment?.content.length).toBeLessThan("memory ".repeat(200).length);
    expect(runtime.snapshot().candidates[0]?.reason).toBe("selected_truncated");
    expect(runtime.snapshot().receipts.at(-1)?.truncated).toBe(true);
  });

  test("drops optional candidates when the total token budget is consumed", () => {
    const runtime = new CompactRestoreRuntime(
      restorePolicy({
        maximumTotalTokens: 1_000,
        maximumFileTokens: 1_000,
        maximumMemoryTokens: 1_000,
      }),
    );
    inlineCandidate(runtime, "required-budget", "required context ".repeat(3_000), {
      kind: "memory",
      required: true,
    });
    inlineCandidate(runtime, "optional-budget", "optional context ".repeat(3_000), {
      kind: "file",
    });
    const selected = runtime.select();
    const optional = runtime.snapshot().candidates.find(
      (item) => item.candidateId === "optional-budget",
    );

    expect(selected.map((item) => item.name)).toEqual(["required-budget"]);
    expect(optional?.state).toBe("dropped");
    expect(optional?.reason).toBe("total_token_limit");
  });

  test("re-evaluates existing candidates after policy tightening", () => {
    const runtime = new CompactRestoreRuntime(
      restorePolicy({ deniedPatterns: [] }),
    );
    runtime.discover({
      candidateId: "later-denied",
      kind: "file",
      name: "generated",
      path: "generated/output.txt",
      sourceId: "workspace",
      content: "generated output",
    });
    runtime.configure({ deniedPatterns: ["(?:^|/)generated(?:$|/)"] });

    const candidate = runtime.snapshot().candidates[0];
    expect(candidate?.state).toBe("denied");
    expect(candidate?.reason).toContain("denied_pattern");
    expect(runtime.select()).toEqual([]);
  });

  test("projects allowed content into context attachment candidates", () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    inlineCandidate(runtime, "plan-attachment", "plan body", {
      kind: "plan",
      priority: 9,
      required: true,
    });
    inlineCandidate(runtime, "agent-attachment", "agent body", {
      kind: "agent",
      priority: 3,
    });
    const projected = runtime.toAttachmentCandidates();

    expect(projected).toHaveLength(2);
    expect(projected[0]?.kind).toBe("plan");
    expect(projected[0]?.priority).toBe(10_009);
    expect(projected[1]?.kind).toBe("agent");
  });

  test("round-trips candidates receipts and policy through a snapshot", () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    inlineCandidate(runtime, "snapshot-memory", "snapshot body", {
      kind: "memory",
    });
    runtime.select();
    const snapshot = runtime.snapshot();
    const restored = new CompactRestoreRuntime();
    restored.restore(snapshot);

    expect(restored.snapshot()).toEqual(snapshot);
    expect(restored.toAttachmentCandidates()[0]?.content).toBe("snapshot body");
    expect(restored.snapshot().policy.workspaceRoot).toBe(WORKSPACE);
  });

  test("rejects checksum and content digest tampering", () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    inlineCandidate(runtime, "tamper-memory", "original body", {
      kind: "memory",
    });
    const checksumTamper = runtime.snapshot();
    checksumTamper.policy.maximumFiles += 1;
    expect(() => new CompactRestoreRuntime().restore(checksumTamper)).toThrow(
      "compact restore snapshot checksum mismatch",
    );

    const digestTamper = runtime.snapshot();
    digestTamper.candidates[0]!.content = "changed body";
    const unsigned = { ...digestTamper } as Record<string, unknown>;
    delete unsigned.checksum;
    digestTamper.checksum = digest(unsigned);
    expect(() => new CompactRestoreRuntime().restore(digestTamper)).toThrow(
      "restore candidate digest mismatch",
    );
  });

  test("routes module discovery selection and inspection into canonical state", () => {
    const runtime = new CompactRestoreRuntime(restorePolicy());
    const candidate = runtime.restore_module({
      action: "discover",
      candidate: {
        candidate_id: "module-memory",
        kind: "memory",
        name: "module memory",
        source_id: "memory-store",
        content: "remember this",
        priority: 7,
      },
    });
    const selection = runtime.restore_module({ action: "select" });
    const inspection = runtime.restore_module({ action: "inspect" });

    expect(candidate.state).toBe("allowed");
    expect(Array.isArray(selection.attachments)).toBe(true);
    expect(Array.isArray(inspection.receipts)).toBe(true);
    expect(inspection.revision).toBeGreaterThan(0);
  });
});

describe("E01 durable transition journal", () => {
  test("starts with an empty TypeScript-owned session state", () => {
    const journal = new Journal("run-1", "session-1");
    const snapshot = journal.snapshot();

    expect(journal.revision).toBe(0);
    expect(journal.restartEpoch).toBe(0);
    expect(journal.state).toEqual({});
    expect(snapshot.owner).toBe("typescript");
    expect(snapshot.pending).toEqual([]);
    expect(snapshot.committed).toEqual([]);
    expect(snapshot.stateDigest).toBe(digest({}));
  });

  test("prepares a transition without mutating canonical state", () => {
    const journal = new Journal("run-1", "session-1");
    const value = transition(journal, { query: { phase: "reason" } });
    const prepared = journal.prepare(value);

    expect(prepared.phase).toBe("prepare");
    expect(prepared.status).toBe("pending");
    expect(prepared.revisionBefore).toBe(0);
    expect(prepared.revisionAfter).toBe(0);
    expect(journal.state).toEqual({});
    expect(journal.pending()).toHaveLength(1);
  });

  test("deduplicates the same prepared command identity", () => {
    const journal = new Journal("run-1", "session-1");
    const value = transition(journal, { query: { phase: "observe" } });
    const first = journal.prepare(value);
    const repeated = journal.prepare(value);

    expect(first.duplicate).toBe(false);
    expect(repeated.duplicate).toBe(true);
    expect(repeated.commandDigest).toBe(first.commandDigest);
    expect(journal.pending()).toHaveLength(1);
  });

  test("rejects transition ID reuse with a changed command", () => {
    const journal = new Journal("run-1", "session-1");
    const first = transition(journal, { value: 1 });
    journal.prepare(first);
    const conflicting = transition(
      journal,
      { value: 2 },
      { identity: first.identity },
    );

    expect(() => journal.prepare(conflicting)).toThrow("idempotency_conflict");
    expect(journal.pending()[0]?.payloadDigest).toBe(digest({ value: 1 }));
  });

  test("reserves write sets until the owning transition commits", () => {
    const journal = new Journal("run-1", "session-1");
    const first = transition(journal, { value: 1 }, { writeSet: ["session.state"] });
    journal.prepare(first);
    const second = transition(journal, { value: 2 }, { writeSet: ["session.state"] });

    expect(() => journal.prepare(second)).toThrow("write_conflict");
    journal.commit(first, { session: { value: 1 } }, []);
    const afterCommit = transition(journal, { value: 2 }, { writeSet: ["session.state"] });
    expect(journal.prepare(afterCommit).status).toBe("pending");
  });

  test("allows disjoint prepared writes but commits with compare-and-swap", () => {
    const journal = new Journal("run-1", "session-1");
    const sessionWrite = transition(journal, { value: 1 }, { writeSet: ["session"] });
    const contextWrite = transition(journal, { value: 2 }, { writeSet: ["context"] });
    journal.prepare(sessionWrite);
    journal.prepare(contextWrite);
    journal.commit(sessionWrite, { session: { value: 1 } }, []);

    expect(() =>
      journal.commit(contextWrite, { context: { value: 2 } }, []),
    ).toThrow("stale_revision");
    expect(journal.state).toEqual({ session: { value: 1 } });
  });

  test("requires prepare before effect or commit", () => {
    const journal = new Journal("run-1", "session-1");
    const value = transition(journal, { tool: "write_file" }, { requiresEffect: true });

    expect(() => journal.effect(value, { bytes: 12 })).toThrow("prepare_missing");
    expect(() => journal.commit(value, { tool: { done: true } }, [])).toThrow(
      "prepare_missing",
    );
  });

  test("records one terminal effect for repeated delivery", () => {
    const journal = new Journal("run-1", "session-1");
    const value = transition(
      journal,
      { path: "runtime.ts" },
      { requiresEffect: true },
    );
    journal.prepare(value);
    const first = journal.effect(value, { bytes_written: 128 });
    const repeated = journal.effect(value, { bytes_written: 999 });

    expect(first.ok).toBe(true);
    expect(repeated).toEqual(first);
    expect(repeated.resultDigest).toBe(digest({ bytes_written: 128 }));
    expect(journal.pending()[0]?.status).toBe("effect_recorded");
  });

  test("blocks an effectful commit until a terminal effect exists", () => {
    const journal = new Journal("run-1", "session-1");
    const value = transition(
      journal,
      { tool: "shell" },
      { requiresEffect: true },
    );
    journal.prepare(value);

    expect(() => journal.commit(value, { tool: { status: "done" } }, [])).toThrow(
      "effect_missing",
    );
    expect(journal.revision).toBe(0);
    expect(journal.state).toEqual({});
  });

  test("does not commit canonical success after a failed side effect", () => {
    const journal = new Journal("run-1", "session-1");
    const value = transition(
      journal,
      { tool: "write_file" },
      { requiresEffect: true },
    );
    journal.prepare(value);
    journal.effect(value, { path: "runtime.ts" }, "permission denied");

    expect(() =>
      journal.commit(value, { tool: { status: "succeeded" } }, []),
    ).toThrow("effect_failed");
    expect(journal.state).toEqual({});
    expect(journal.revision).toBe(0);
  });

  test("deep-merges committed state while preserving sibling domains", () => {
    const journal = new Journal("run-1", "session-1");
    const first = transition(journal, { phase: "start" });
    journal.prepare(first);
    journal.commit(
      first,
      { session: { phase: "start", counters: { turns: 1 } } },
      [],
    );
    const second = transition(journal, { phase: "tool" });
    journal.prepare(second);
    journal.commit(
      second,
      { session: { phase: "tool", counters: { tools: 1 } } },
      [],
    );

    expect(journal.state).toEqual({
      session: {
        phase: "tool",
        counters: { turns: 1, tools: 1 },
      },
    });
    expect(journal.revision).toBe(2);
  });

  test("emits durable ordered outbox records from commit", () => {
    const journal = new Journal("run-1", "session-1");
    const value = transition(journal, { phase: "compact" });
    journal.prepare(value);
    const committed = journal.commit(
      value,
      { compact: { generation: 1 } },
      [
        { type: "compact.started", generation: 1 },
        { type: "compact.completed", generation: 1 },
      ],
    );
    const outbox = journal.undelivered();

    expect(committed.outboxIds).toHaveLength(2);
    expect(outbox.map((record) => record.sequence)).toEqual([1, 2]);
    expect(outbox[0]?.transitionId).toBe(value.identity.transitionId);
    expect(outbox[1]?.payloadDigest).toBe(
      digest({ type: "compact.completed", generation: 1 }),
    );
  });

  test("projects an outbox effect exactly once across lost acknowledgements", () => {
    const journal = new Journal("run-1", "session-1");
    const value = transition(journal, { event: "result" });
    journal.prepare(value);
    const committed = journal.commit(
      value,
      { result: { delivered: true } },
      [{ type: "runtime.result", result_id: "result-1" }],
    );
    const outboxId = committed.outboxIds[0]!;
    const firstProjection = journal.project(outboxId, "2026-07-15T01:00:00.000Z");
    const repeatedProjection = journal.project(outboxId, "2026-07-15T02:00:00.000Z");

    expect(firstProjection.delivered).toBe(true);
    expect(repeatedProjection.deliveredAt).toBe(firstProjection.deliveredAt);
    expect(journal.undelivered()).toEqual([]);
    expect(journal.state).toEqual({ result: { delivered: true } });
    expect(journal.revision).toBe(1);
  });

  test("deduplicates commit replay before and after ACK", () => {
    const journal = new Journal("run-1", "session-1");
    const value = transition(journal, { command: "cancel" });
    journal.prepare(value);
    const first = journal.commit(
      value,
      { session: { cancelled: true } },
      [{ type: "session.cancelled" }],
    );
    const replayBeforeAck = journal.commit(
      value,
      { session: { cancelled: false } },
      [{ type: "should.not.repeat" }],
    );
    const acked = journal.ack(value.identity.transitionId);
    const replayAfterAck = journal.commit(value, {}, []);

    expect(first.duplicate).toBe(false);
    expect(replayBeforeAck.duplicate).toBe(true);
    expect(acked.status).toBe("acked");
    expect(replayAfterAck.duplicate).toBe(true);
    expect(replayAfterAck.status).toBe("acked");
    expect(journal.revision).toBe(1);
    expect(journal.snapshot().outbox).toHaveLength(1);
  });

  test("quarantines a pending transition without advancing revision", () => {
    const journal = new Journal("run-1", "session-1");
    const value = transition(journal, { malformed: true });
    journal.prepare(value);
    const quarantined = journal.quarantine(
      value.identity.transitionId,
      "schema validation failed",
    );

    expect(quarantined.phase).toBe("quarantine");
    expect(quarantined.status).toBe("quarantined");
    expect(quarantined.error).toBe("schema validation failed");
    expect(journal.revision).toBe(0);
    expect(journal.pending()).toHaveLength(1);
  });

  test("restores committed state before allocating a new transition identity", () => {
    const before = new Journal("run-before", "session-1");
    const old = transition(before, { value: "before" });
    before.prepare(old);
    before.commit(old, { session: { value: "before" } }, []);
    const snapshot = before.snapshot();
    const after = new Journal("placeholder", "session-1");
    after.restore(snapshot, "run-after");
    const freshIdentity = identity(after);

    expect(after.runId).toBe("run-after");
    expect(after.restartEpoch).toBe(1);
    expect(after.revision).toBe(snapshot.revision);
    expect(after.state).toEqual({ session: { value: "before" } });
    expect(freshIdentity.restartEpoch).toBe(1);
    expect(freshIdentity.expectedRevision).toBe(snapshot.revision);
    expect(freshIdentity.transitionId).not.toBe(old.identity.transitionId);
  });

  test("reconstructs pending write locks during same-session restore", () => {
    const before = new Journal("run-before", "session-1");
    const pending = transition(
      before,
      { cache: "pending" },
      { domain: "context", writeSet: ["context.cache.entries"] },
    );
    before.prepare(pending);
    const after = new Journal("placeholder", "session-1");
    after.restore(before.snapshot(), "run-after");
    const competing = transition(
      after,
      { cache: "competing" },
      { domain: "context", writeSet: ["context.cache.entries"] },
    );

    expect(() => after.prepare(competing)).toThrow("write_conflict");
    expect(after.pending()[0]?.identity.transitionId).toBe(
      pending.identity.transitionId,
    );
  });

  test("keeps undelivered outbox work after a process restart", () => {
    const before = new Journal("run-before", "session-1");
    const value = transition(before, { event: "pending-delivery" });
    before.prepare(value);
    const committed = before.commit(
      value,
      { delivery: { committed: true } },
      [{ type: "delivery.pending", delivery_id: "delivery-1" }],
    );
    const after = new Journal("placeholder", "session-1");
    after.restore(before.snapshot(), "run-after");

    expect(after.undelivered().map((record) => record.outboxId)).toEqual(
      committed.outboxIds,
    );
    after.project(committed.outboxIds[0]!, "2026-07-15T03:00:00.000Z");
    expect(after.undelivered()).toEqual([]);
    expect(after.revision).toBe(before.revision);
  });

  test("rejects a snapshot for a different session", () => {
    const source = new Journal("run-1", "session-source");
    const target = new Journal("run-2", "session-target");

    expect(() => target.restore(source.snapshot(), "run-3")).toThrow(
      "session_mismatch",
    );
    expect(target.revision).toBe(0);
    expect(target.state).toEqual({});
  });

  test("rejects checksum and state digest corruption before mutation", () => {
    const source = new Journal("run-1", "session-1");
    const value = transition(source, { value: "canonical" });
    source.prepare(value);
    source.commit(value, { session: { value: "canonical" } }, []);
    const checksumTamper = source.snapshot();
    checksumTamper.revision += 1;
    expect(() =>
      new Journal("run-2", "session-1").restore(checksumTamper, "run-2"),
    ).toThrow("snapshot_checksum");

    const stateTamper = source.snapshot();
    stateTamper.stateDigest = digest({ session: { value: "forged" } });
    const unsigned = { ...stateTamper } as Partial<typeof stateTamper>;
    delete unsigned.checksum;
    stateTamper.checksum = digest(unsigned);
    expect(() =>
      new Journal("run-2", "session-1").restore(stateTamper, "run-2"),
    ).toThrow("state_digest");
  });

  test("rejects stale run epoch and revision command identities", () => {
    const journal = new Journal("run-1", "session-1");
    const correct = identity(journal);
    const wrongRun = transition(journal, {}, {
      identity: { ...correct, runId: "run-other" },
    });
    expect(() => journal.prepare(wrongRun)).toThrow("run_mismatch");

    const wrongEpoch = transition(journal, {}, {
      identity: { ...correct, restartEpoch: 9 },
    });
    expect(() => journal.prepare(wrongEpoch)).toThrow("epoch_mismatch");

    const wrongRevision = transition(journal, {}, {
      identity: { ...correct, expectedRevision: 9 },
    });
    expect(() => journal.prepare(wrongRevision)).toThrow("stale_revision");
  });

  test("rejects Python ownership in command and payload", () => {
    const journal = new Journal("run-1", "session-1");
    const wrongOwner = transition(journal);
    (wrongOwner as unknown as { owner: string }).owner = "python";
    expect(() => journal.prepare(wrongOwner)).toThrow("stale_owner");

    const payloadOwner = transition(journal, {
      logical_owner: "python",
      value: "must not enter E01",
    });
    expect(() => journal.prepare(payloadOwner)).toThrow("stale_owner");
    expect(journal.pending()).toEqual([]);
  });

  test("rejects payload digest tampering and duplicate write paths", () => {
    const journal = new Journal("run-1", "session-1");
    const tampered = transition(journal, { value: 1 });
    tampered.payload.value = 2;
    expect(() => journal.prepare(tampered)).toThrow("payload_digest");

    const duplicateWrites = transition(journal, { value: 3 }, {
      writeSet: ["session", "session"],
    });
    expect(() => journal.prepare(duplicateWrites)).toThrow("duplicate_write");
  });

  test("produces zero transition ID overlap after same-session resume", () => {
    const before = new Journal("run-before", "session-1");
    const oldIds = new Set<string>();
    for (const phase of ["reason", "tool", "observe"]) {
      const value = transition(before, { phase });
      before.prepare(value);
      before.commit(value, { session: { phase } }, []);
      oldIds.add(value.identity.transitionId);
    }
    const after = new Journal("placeholder", "session-1");
    after.restore(before.snapshot(), "run-after");
    const newIds = new Set<string>();
    for (const phase of ["revise", "compact", "complete"]) {
      const value = transition(after, { phase });
      after.prepare(value);
      after.commit(value, { session: { phase } }, []);
      newIds.add(value.identity.transitionId);
    }

    const overlap = [...newIds].filter((transitionId) => oldIds.has(transitionId));
    expect(overlap).toEqual([]);
    expect(after.revision).toBe(6);
    expect(after.restartEpoch).toBe(1);
    expect(after.committed()).toHaveLength(6);
  });
});
