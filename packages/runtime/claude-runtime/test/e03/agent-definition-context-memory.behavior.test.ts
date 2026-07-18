import assert from "node:assert/strict";
import { test } from "node:test";
import {
  AgentContextFork,
  rootContext,
} from "../../src/agents/context-fork.ts";
import { AgentDefinitionLoader } from "../../src/agents/definition-loader.ts";
import { AgentDefinitionRegistry } from "../../src/agents/definition-registry.ts";
import { AgentMemoryRuntime } from "../../src/agents/memory-runtime.ts";
import {
  AgentScopeLattice,
  rootCapabilityScope,
} from "../../src/agents/scope-lattice.ts";
import {
  digest,
  emptySnapshot,
  E03RuntimeError,
} from "../../src/e03/contracts.ts";
import { context, definition, scope, task, TestClock } from "./fixtures.ts";

function assertCode(error: unknown, code: string): boolean {
  assert.ok(error instanceof E03RuntimeError);
  assert.equal(error.code, code);
  assert.ok(error.message.trim());
  return true;
}

function definitionRegistry() {
  const registry = new AgentDefinitionRegistry();
  const builtin = registry.register({
    name: "reviewer",
    description: "Built-in reviewer",
    version: "1",
    source: "builtin",
    tools: ["read"],
    skills: ["review"],
  });
  return { registry, builtin };
}

function memoryRuntime(label: string) {
  const clock = new TestClock("2026-07-18T15:00:00.000Z");
  const runtime = new AgentMemoryRuntime(
    `memory-task-${label}`,
    `memory-session-${label}`,
    clock,
  );
  return { clock, runtime };
}

test("e03.definition registers normalized defaults", () => {
  const registry = new AgentDefinitionRegistry();
  const registered = registry.register({
    name: "normalized",
    description: "Normalized agent definition",
  });
  assert.equal(registered.name, "normalized");
  assert.equal(registered.version, "1");
  assert.equal(registered.source, "user");
  assert.equal(registered.priority, 300);
  assert.equal(registered.model, "inherit");
  assert.equal(registered.effort, "inherit");
  assert.equal(registered.permissionMode, "inherit");
  assert.equal(registered.isolation, "workspace");
  assert.equal(registered.background, false);
  assert.ok(registered.digest);
  assert.equal(registry.list().length, 1);
});

test("e03.definition resolves project precedence", () => {
  const { registry, builtin } = definitionRegistry();
  const plugin = registry.register({
    name: "reviewer",
    description: "Plugin reviewer",
    version: "2",
    source: "plugin",
  });
  const user = registry.register({
    name: "reviewer",
    description: "User reviewer",
    version: "3",
    source: "user",
  });
  const project = registry.register({
    name: "reviewer",
    description: "Project reviewer",
    version: "4",
    source: "project",
  });
  assert.equal(registry.resolve("reviewer").digest, project.digest);
  assert.equal(registry.resolve("reviewer", "builtin").digest, builtin.digest);
  assert.equal(registry.resolve("reviewer", "plugin").digest, plugin.digest);
  assert.equal(registry.resolve("reviewer", "user").digest, user.digest);
  assert.deepEqual(
    registry.list("reviewer").map((value) => value.source),
    ["project", "user", "plugin", "builtin"],
  );
});

test("e03.definition replays identical registration", () => {
  const { registry, builtin } = definitionRegistry();
  const generation = registry.generation();
  const replay = registry.register({
    name: "reviewer",
    description: "Built-in reviewer",
    version: "1",
    source: "builtin",
    tools: ["read"],
    skills: ["review"],
  });
  assert.equal(replay.digest, builtin.digest);
  assert.equal(registry.list("reviewer").length, 1);
  assert.equal(registry.generation(), generation);
});

test("e03.definition rejects changed duplicate version", () => {
  const { registry } = definitionRegistry();
  assert.throws(
    () =>
      registry.register({
        name: "reviewer",
        description: "Changed without version bump",
        version: "1",
        source: "builtin",
        tools: ["read"],
      }),
    (error) => assertCode(error, "definition_conflict"),
  );
  assert.equal(registry.resolve("reviewer").description, "Built-in reviewer");
  assert.equal(registry.list("reviewer").length, 1);
});

test("e03.definition rejects invalid agent name", () => {
  const registry = new AgentDefinitionRegistry();
  assert.throws(
    () =>
      registry.register({
        name: "invalid agent name",
        description: "Invalid definition name",
      }),
    (error) => assertCode(error, "invalid_agent_name"),
  );
  assert.equal(registry.list().length, 0);
  assert.equal(registry.generation().length, 64);
});

test("e03.definition rejects allow deny overlap", () => {
  const registry = new AgentDefinitionRegistry();
  assert.throws(
    () =>
      registry.register({
        name: "overlap",
        description: "Overlapping tool definition",
        tools: ["read", "write"],
        deniedTools: ["write", "shell"],
      }),
    (error) => assertCode(error, "tool_scope_overlap"),
  );
  assert.equal(registry.list().length, 0);
});

test("e03.definition rejects invalid isolation", () => {
  const registry = new AgentDefinitionRegistry();
  assert.throws(
    () =>
      registry.register({
        name: "invalid-isolation",
        description: "Invalid isolation definition",
        isolation: "container-escape",
      }),
    (error) => assertCode(error, "invalid_isolation"),
  );
  assert.equal(registry.list().length, 0);
});

test("e03.definition rejects invalid budget", () => {
  const registry = new AgentDefinitionRegistry();
  assert.throws(
    () =>
      registry.register({
        name: "invalid-budget",
        description: "Invalid budget definition",
        budget: { maxTurns: 0 },
      }),
    (error) => assertCode(error, "invalid_budget"),
  );
  assert.equal(registry.list().length, 0);
});

test("e03.definition rejects unknown resolution", () => {
  const registry = new AgentDefinitionRegistry();
  assert.throws(
    () => registry.resolve("missing-agent"),
    (error) => assertCode(error, "unknown_agent"),
  );
  assert.throws(
    () => registry.resolve("missing-agent", "project"),
    (error) => assertCode(error, "unknown_agent"),
  );
});

test("e03.definition removes selected source and falls back", () => {
  const { registry, builtin } = definitionRegistry();
  const project = registry.register({
    name: "reviewer",
    description: "Project reviewer",
    version: "2",
    source: "project",
  });
  assert.equal(registry.resolve("reviewer").digest, project.digest);
  assert.equal(registry.remove("reviewer", "project", "2"), true);
  assert.equal(registry.resolve("reviewer").digest, builtin.digest);
  assert.equal(registry.remove("reviewer", "project", "2"), false);
  assert.equal(registry.list("reviewer").length, 1);
});

test("e03.definition projects into durable snapshot", () => {
  const { registry, builtin } = definitionRegistry();
  const snapshot = emptySnapshot(new TestClock("2026-07-18T15:30:00.000Z"));
  const projected = registry.project(snapshot);
  assert.equal(projected.definitions.reviewer?.length, 1);
  assert.equal(projected.definitions.reviewer?.[0]?.digest, builtin.digest);
  assert.ok(projected.revision >= snapshot.revision);
  assert.notEqual(projected.checksum, snapshot.checksum);
  const restored = new AgentDefinitionRegistry();
  restored.restore(projected);
  assert.equal(restored.resolve("reviewer").digest, builtin.digest);
  assert.equal(restored.list().length, 1);
});

test("e03.definition loader validates aliases", () => {
  const registry = new AgentDefinitionRegistry();
  const loader = new AgentDefinitionLoader(registry);
  const validated = loader.validate(
    {
      name: "alias-worker",
      description: "Definition with snake aliases",
      version: "7",
      system_prompt: "Follow the evidence.",
      denied_tools: ["shell"],
      mcp_servers: ["local"],
      permission_mode: "ask",
      memory_scope: "session",
      metadata: { suite: "e03" },
    },
    "project",
    "memory://alias-worker",
  );
  assert.equal(validated.systemPrompt, "Follow the evidence.");
  assert.deepEqual(validated.deniedTools, ["shell"]);
  assert.deepEqual(validated.mcpServers, ["local"]);
  assert.equal(validated.permissionMode, "ask");
  assert.equal(validated.memoryScope, "session");
  assert.equal(validated.metadata.definition_path, "memory://alias-worker");
});

test("e03.scope derives least privilege intersection", () => {
  const lattice = new AgentScopeLattice();
  const parent = scope({
    tools: ["read", "write", "shell"],
    deniedTools: ["shell"],
    skills: ["analysis", "review"],
    mcpServers: ["local", "remote"],
    maxDepth: 5,
    maxChildren: 6,
  });
  const child = lattice.derive({
    parent,
    definition: definition("least-privilege", {
      tools: ["read"],
      deniedTools: ["write"],
      skills: ["review"],
      mcpServers: ["local"],
    }),
    requestedTools: ["read", "write", "shell"],
    requestedSkills: ["review"],
    requestedMcpServers: ["local", "remote"],
    requestedWorkspaceRoots: [process.cwd()],
    requestedIsolation: "workspace",
    depth: 1,
  });
  assert.deepEqual(child.tools, ["read"]);
  assert.deepEqual(child.deniedTools.sort(), ["shell", "write"]);
  assert.deepEqual(child.skills, ["review"]);
  assert.deepEqual(child.mcpServers, ["local"]);
  assert.equal(child.permissionCeilingDigest, parent.permissionCeilingDigest);
  lattice.assertMonotonic(parent, child);
});

test("e03.scope rejects depth escape", () => {
  const lattice = new AgentScopeLattice();
  const parent = scope({ maxDepth: 2 });
  assert.throws(
    () =>
      lattice.derive({
        parent,
        definition: definition("depth-escape"),
        requestedIsolation: "workspace",
        depth: 3,
      }),
    (error) => assertCode(error, "depth_exceeded"),
  );
  assert.equal(parent.maxDepth, 2);
});

test("e03.scope rejects workspace escape", () => {
  const lattice = new AgentScopeLattice();
  const parent = scope({ workspaceRoots: [process.cwd()] });
  assert.throws(
    () =>
      lattice.derive({
        parent,
        definition: definition("workspace-escape"),
        requestedWorkspaceRoots: ["C:/outside-authority"],
        requestedIsolation: "workspace",
        depth: 1,
      }),
    (error) => assertCode(error, "workspace_scope_exceeded"),
  );
  assert.deepEqual(parent.workspaceRoots, [process.cwd()]);
});

test("e03.scope rejects isolation escalation", () => {
  const lattice = new AgentScopeLattice();
  const parent = scope({ isolationModes: ["workspace", "worktree"] });
  assert.throws(
    () =>
      lattice.derive({
        parent,
        definition: definition("isolation-escalation", {
          isolation: "remote",
        }),
        requestedIsolation: "remote",
        depth: 1,
      }),
    (error) => assertCode(error, "isolation_scope_exceeded"),
  );
  assert.deepEqual(parent.isolationModes, ["workspace", "worktree"]);
});

test("e03.scope rejects permission ceiling escalation", () => {
  const lattice = new AgentScopeLattice();
  const parent = scope({ permissionMode: "ask" });
  assert.throws(
    () =>
      lattice.derive({
        parent,
        definition: definition("permission-escalation", {
          permissionMode: "allow",
        }),
        requestedPermissionMode: "allow",
        requestedIsolation: "workspace",
        depth: 1,
      }),
    (error) => assertCode(error, "permission_ceiling_exceeded"),
  );
  assert.equal(parent.permissionMode, "ask");
});

test("e03.scope detects tampered child digest", () => {
  const lattice = new AgentScopeLattice();
  const parent = scope();
  const child = lattice.derive({
    parent,
    definition: definition("digest-child"),
    requestedIsolation: "workspace",
    depth: 1,
  });
  assert.throws(
    () => lattice.assertMonotonic(parent, { ...child, digest: digest("bad") }),
    (error) => assertCode(error, "scope_digest_mismatch"),
  );
  lattice.assertMonotonic(parent, child);
});

test("e03.scope builds normalized root scope", () => {
  const root = rootCapabilityScope({
    tools: ["read", "read", "write"],
    deniedTools: ["shell", "shell"],
    skills: ["analysis", "analysis"],
    mcpServers: ["local", "local"],
    permissionMode: "ask",
    permissionCeilingDigest: digest("root-ceiling"),
    workspaceRoots: [process.cwd(), process.cwd()],
    isolationModes: ["workspace", "sandbox"],
    maxDepth: 7,
    maxChildren: 11,
  });
  assert.deepEqual(root.tools, ["read", "write"]);
  assert.deepEqual(root.deniedTools, ["shell"]);
  assert.deepEqual(root.skills, ["analysis"]);
  assert.deepEqual(root.mcpServers, ["local"]);
  assert.equal(root.maxDepth, 7);
  assert.equal(root.maxChildren, 11);
  assert.ok(root.digest);
});

test("e03.context forks inherited references", () => {
  const capabilityScope = scope();
  const parent = context("context-parent", "context-session", capabilityScope, {
    messageRefs: ["message-parent"],
    artifactRefs: ["artifact-parent"],
    evidenceRefs: ["evidence-parent"],
    memoryRefs: ["memory-parent"],
    topologyRevision: 4,
  });
  const runtime = new AgentContextFork(
    new TestClock("2026-07-18T16:00:00.000Z"),
  );
  const child = runtime.fork({
    parent,
    childTaskId: "context-child",
    childSessionId: "context-child-session",
    mode: "fork",
    scope: capabilityScope,
    messageRefs: ["message-parent", "message-child"],
    artifactRefs: ["artifact-child"],
    evidenceRefs: ["evidence-child"],
    memoryRefs: ["memory-child"],
  });
  assert.equal(child.parentSnapshotId, parent.snapshotId);
  assert.notEqual(child.branchId, parent.branchId);
  assert.deepEqual(child.messageRefs, ["message-child", "message-parent"]);
  assert.deepEqual(child.artifactRefs, ["artifact-child", "artifact-parent"]);
  assert.deepEqual(child.evidenceRefs, ["evidence-child", "evidence-parent"]);
  assert.deepEqual(child.memoryRefs, ["memory-child", "memory-parent"]);
  assert.equal(child.topologyRevision, 4);
});

test("e03.context isolates parent content", () => {
  const capabilityScope = scope();
  const parent = context(
    "isolated-parent",
    "isolated-session",
    capabilityScope,
    {
      messageRefs: ["secret-message"],
      artifactRefs: ["secret-artifact"],
      memoryRefs: ["secret-memory"],
    },
  );
  const runtime = new AgentContextFork();
  const isolated = runtime.fork({
    parent,
    childTaskId: "isolated-child",
    childSessionId: "isolated-child-session",
    mode: "isolated",
    scope: capabilityScope,
    messageRefs: ["public-message"],
    artifactRefs: ["public-artifact"],
    memoryRefs: ["public-memory"],
  });
  assert.deepEqual(isolated.messageRefs, ["public-message"]);
  assert.deepEqual(isolated.artifactRefs, ["public-artifact"]);
  assert.deepEqual(isolated.memoryRefs, ["public-memory"]);
  assert.equal(isolated.parentSnapshotId, parent.snapshotId);
  assert.equal(isolated.sequence, 0);
});

test("e03.context resumes branch and compact boundaries", () => {
  const capabilityScope = scope();
  const runtime = new AgentContextFork(
    new TestClock("2026-07-18T16:30:00.000Z"),
  );
  const parent = context("resume-parent", "resume-session", capabilityScope, {
    sequence: 7,
    compactBoundaryIds: ["compact-1"],
    memoryRefs: ["memory-before"],
  });
  const resumed = runtime.fork({
    parent,
    childTaskId: "resume-child",
    childSessionId: "resume-child-session",
    mode: "resume",
    scope: capabilityScope,
    memoryRefs: ["memory-after"],
  });
  assert.equal(resumed.branchId, parent.branchId);
  assert.equal(resumed.sequence, parent.sequence + 1);
  assert.deepEqual(resumed.compactBoundaryIds, ["compact-1"]);
  assert.deepEqual(resumed.memoryRefs, ["memory-after", "memory-before"]);
  assert.equal(
    resumed.permissionDigest,
    capabilityScope.permissionCeilingDigest,
  );
});

test("e03.context appends and deduplicates references", () => {
  const capabilityScope = scope();
  const runtime = new AgentContextFork(
    new TestClock("2026-07-18T16:45:00.000Z"),
  );
  const original = context(
    "append-context",
    "append-session",
    capabilityScope,
    {
      messageRefs: ["message-1"],
      topologyRevision: 2,
    },
  );
  const appended = runtime.append(original, {
    messageRefs: ["message-1", "message-2"],
    artifactRefs: ["artifact-1"],
    evidenceRefs: ["evidence-1"],
    memoryRefs: ["memory-1"],
    compactBoundaryId: "compact-append",
    topologyRevision: 3,
  });
  assert.equal(appended.sequence, original.sequence + 1);
  assert.deepEqual(appended.messageRefs, ["message-1", "message-2"]);
  assert.deepEqual(appended.artifactRefs, ["artifact-1"]);
  assert.deepEqual(appended.evidenceRefs, ["evidence-1"]);
  assert.deepEqual(appended.memoryRefs, ["memory-1"]);
  assert.deepEqual(appended.compactBoundaryIds, ["compact-append"]);
  assert.equal(appended.topologyRevision, 3);
  assert.notEqual(appended.checksum, original.checksum);
});

test("e03.context rejects restore identity mismatch", () => {
  const capabilityScope = scope();
  const snapshot = context(
    "restore-context",
    "restore-session",
    capabilityScope,
  );
  const runtime = new AgentContextFork();
  assert.throws(
    () =>
      runtime.restore(snapshot, {
        sessionId: "another-session",
        taskId: snapshot.taskId,
        permissionDigest: capabilityScope.permissionCeilingDigest,
      }),
    (error) => assertCode(error, "context_identity_mismatch"),
  );
  assert.equal(snapshot.sessionId, "restore-session");
});

test("e03.context rejects permission mismatch", () => {
  const capabilityScope = scope();
  const snapshot = context(
    "permission-context",
    "permission-session",
    capabilityScope,
  );
  const runtime = new AgentContextFork();
  assert.throws(
    () =>
      runtime.restore(snapshot, {
        sessionId: snapshot.sessionId,
        taskId: snapshot.taskId,
        permissionDigest: digest("different-ceiling"),
      }),
    (error) => assertCode(error, "context_permission_mismatch"),
  );
  assert.equal(
    snapshot.permissionDigest,
    capabilityScope.permissionCeilingDigest,
  );
});

test("e03.context rejects checksum tamper", () => {
  const capabilityScope = scope();
  const snapshot = context("tamper-context", "tamper-session", capabilityScope);
  const runtime = new AgentContextFork();
  assert.throws(
    () => runtime.validate({ ...snapshot, sequence: snapshot.sequence + 1 }),
    (error) => assertCode(error, "checksum_mismatch"),
  );
  runtime.validate(snapshot);
});

test("e03.context creates canonical root", () => {
  const clock = new TestClock("2026-07-18T17:00:00.000Z");
  const root = rootContext({
    sessionId: "root-context-session",
    taskId: "root-context-task",
    permissionDigest: digest("root-permission"),
    toolCatalogDigest: digest(["read", "write"]),
    topologyRevision: 9,
    clock,
  });
  assert.equal(root.parentSnapshotId, null);
  assert.equal(root.sequence, 0);
  assert.deepEqual(root.messageRefs, []);
  assert.deepEqual(root.artifactRefs, []);
  assert.deepEqual(root.evidenceRefs, []);
  assert.deepEqual(root.memoryRefs, []);
  assert.equal(root.topologyRevision, 9);
  assert.ok(root.checksum);
});

test("e03.memory captures deduplicated provenance", () => {
  const { runtime } = memoryRuntime("capture");
  const record = runtime.capture({
    kind: "decision",
    content: "Use TypeScript as the canonical execution owner.",
    importance: 0.9,
    sourceMessageIds: ["message-1", "message-1", "message-2"],
    sourceArtifactIds: ["artifact-1", "artifact-1"],
  });
  assert.equal(record.kind, "decision");
  assert.equal(record.importance, 0.9);
  assert.deepEqual(record.sourceMessageIds, ["message-1", "message-2"]);
  assert.deepEqual(record.sourceArtifactIds, ["artifact-1"]);
  assert.ok(record.tokenEstimate > 0);
  assert.equal(runtime.snapshot().revision, 1);
  assert.equal(runtime.snapshot().records.length, 1);
  assert.equal(runtime.snapshot().totalTokenEstimate, record.tokenEstimate);
});

test("e03.memory rejects invalid importance", () => {
  const { runtime } = memoryRuntime("invalid-importance");
  assert.throws(
    () =>
      runtime.capture({
        kind: "observation",
        content: "importance too high",
        importance: 1.1,
      }),
    (error) => assertCode(error, "invalid_memory_importance"),
  );
  assert.throws(
    () =>
      runtime.capture({
        kind: "observation",
        content: "importance too low",
        importance: -0.1,
      }),
    (error) => assertCode(error, "invalid_memory_importance"),
  );
  assert.equal(runtime.snapshot().records.length, 0);
});

test("e03.memory compacts by importance and tokens", () => {
  const { runtime } = memoryRuntime("compact");
  const low = runtime.capture({
    kind: "observation",
    content: "low priority memory record",
    importance: 0.1,
  });
  const high = runtime.capture({
    kind: "instruction",
    content: "high priority memory record",
    importance: 1,
  });
  const result = runtime.compact(high.tokenEstimate);
  assert.deepEqual(
    result.retained.map((value) => value.memoryId),
    [high.memoryId],
  );
  assert.deepEqual(
    result.removed.map((value) => value.memoryId),
    [low.memoryId],
  );
  assert.equal(runtime.snapshot().records.length, 1);
  assert.equal(runtime.snapshot().records[0]?.importance, 1);
  assert.equal(runtime.snapshot().totalTokenEstimate, high.tokenEstimate);
});

test("e03.memory removes expired record", () => {
  const { runtime } = memoryRuntime("expired");
  const expired = runtime.capture({
    kind: "observation",
    content: "expired observation",
    importance: 1,
    expiresAt: "2026-07-18T14:00:00.000Z",
  });
  const live = runtime.capture({
    kind: "instruction",
    content: "live instruction",
    importance: 0.5,
    expiresAt: "2026-07-19T14:00:00.000Z",
  });
  const result = runtime.compact(10_000);
  assert.deepEqual(
    result.removed.map((value) => value.memoryId),
    [expired.memoryId],
  );
  assert.deepEqual(
    result.retained.map((value) => value.memoryId),
    [live.memoryId],
  );
  assert.equal(runtime.snapshot().records.length, 1);
});

test("e03.memory projects records into context", () => {
  const { runtime } = memoryRuntime("projection");
  const first = runtime.capture({
    kind: "instruction",
    content: "First projected memory",
  });
  const second = runtime.capture({
    kind: "result",
    content: "Second projected memory",
  });
  const source = context(
    "memory-task-projection",
    "memory-session-projection",
    scope(),
    { memoryRefs: [first.memoryId] },
  );
  const projected = runtime.projectContext(source);
  assert.equal(projected.sequence, source.sequence + 1);
  assert.deepEqual(projected.memoryRefs, [first.memoryId, second.memoryId]);
  assert.notEqual(projected.checksum, source.checksum);
  assert.equal(projected.taskId, source.taskId);
});

test("e03.memory restores exact snapshot", () => {
  const { runtime } = memoryRuntime("restore");
  runtime.capture({
    kind: "handoff",
    content: "Restore this handoff record.",
    importance: 0.8,
  });
  const snapshot = runtime.snapshot();
  const restored = new AgentMemoryRuntime(
    snapshot.taskId,
    snapshot.sessionId,
    new TestClock("2026-07-18T18:00:00.000Z"),
  );
  restored.restore(snapshot, {
    taskId: snapshot.taskId,
    sessionId: snapshot.sessionId,
  });
  assert.deepEqual(restored.snapshot(), snapshot);
  assert.notEqual(restored.snapshot(), snapshot);
  assert.equal(restored.snapshot().records[0]?.kind, "handoff");
});

test("e03.memory rejects restore identity mismatch", () => {
  const { runtime } = memoryRuntime("restore-mismatch");
  runtime.capture({ kind: "result", content: "sealed result" });
  const snapshot = runtime.snapshot();
  const restored = new AgentMemoryRuntime("other-task", "other-session");
  assert.throws(
    () =>
      restored.restore(snapshot, {
        taskId: "other-task",
        sessionId: "other-session",
      }),
    (error) => assertCode(error, "memory_identity_mismatch"),
  );
  assert.equal(restored.snapshot().records.length, 0);
});

test("e03.memory rejects corrupt restore", () => {
  const { runtime } = memoryRuntime("corrupt-restore");
  runtime.capture({ kind: "result", content: "sealed result" });
  const snapshot = runtime.snapshot();
  const restored = new AgentMemoryRuntime(snapshot.taskId, snapshot.sessionId);
  assert.throws(
    () =>
      restored.restore(
        { ...snapshot, totalTokenEstimate: snapshot.totalTokenEstimate + 1 },
        { taskId: snapshot.taskId, sessionId: snapshot.sessionId },
      ),
    (error) => assertCode(error, "checksum_mismatch"),
  );
  assert.equal(restored.snapshot().records.length, 0);
});

test("e03.memory rejects task mismatch ingestion", () => {
  const { runtime } = memoryRuntime("task-mismatch");
  const another = task("another-memory-task");
  assert.throws(
    () => runtime.fromTask(another),
    (error) => assertCode(error, "memory_task_mismatch"),
  );
  assert.equal(runtime.snapshot().records.length, 0);
});
