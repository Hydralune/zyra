import { afterEach, expect, test } from "bun:test";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  ClaudeRuntimeCore,
  SkillCoordinator,
  type ArtifactReceipt,
  type ArtifactRequest,
  type JsonObject,
  type RuntimeEvent,
  type RuntimeHost,
  type RuntimeRunInput,
  type SkillSourceRoot,
  type ToolBatch,
  type ToolExecutionRequest,
  type ToolExecutionResponse,
} from "../../src/index.ts";
import { E01RuntimeCoordinator } from "../../src/e01/coordinator.ts";

afterEach(() => {
  delete process.env.ZYRA_DISABLE_SKILL_MEMORY_RUNTIME;
});

test("real 03C SkillCoordinator outcome reaches 06C without transferring loader or invocation ownership", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "zyra-06c-skill-outcome-"));
  const rootPath = join(workspace, "skills");
  const skillPath = join(rootPath, "reviewer");
  await mkdir(skillPath, { recursive: true });
  await writeFile(join(skillPath, "SKILL.md"), `---
id: reviewer
name: reviewer
description: Review a source change and produce evidence
version: 1.0.0
arguments: {"target":{"type":"string","required":true}}
tools: {"allowed":["file_read"],"denied":["shell"],"namespaces":["builtin"],"mcp_servers":[],"read_only":true,"inherit_parent":true,"maximum_calls":2,"maximum_parallel":1,"require_approval":[]}
context: {"inherit_conversation":true,"inherit_system":true,"inherit_memory":true,"include_workspace_instructions":true,"include_mcp_instructions":false,"maximum_input_tokens":4096,"maximum_resource_tokens":1024,"maximum_output_tokens":1024,"compaction_strategy":"truncate_resources"}
execution: {"mode":"inline","timeout_ms":30000,"maximum_turns":4,"sandbox":"workspace_read","allow_network":false,"persist_transcript":true,"persist_artifacts":true}
enabled: true
---
Review {{target}} and return the observed result.`, "utf8");
  const roots: SkillSourceRoot[] = [{
    sourceId: "06c-project-skills",
    kind: "project",
    rootPath,
    priority: 300,
    enabled: true,
    recursive: true,
    followSymlinks: false,
    maximumDepth: 8,
    includePatterns: ["**/SKILL.md"],
    excludePatterns: ["**/.git/**"],
    pluginId: null,
    revision: 1,
    metadata: { slice: "M1-S06C-01" },
  }];
  const coordinator = new SkillCoordinator({
    workspaceRoot: workspace,
    epoch: 0,
    roots,
    watch: false,
    executor: async (context) => {
      context.assertToolAllowed("file_read", "builtin", "", true);
      context.recordToolCall("file_read", { path: "src/runtime.ts" });
      return {
        output: { reviewed: true, target: context.plan.arguments.target },
        artifacts: [{ artifact_id: "artifact-06c-review", kind: "review" }],
        inputTokens: 90,
        outputTokens: 24,
        costMicros: 42,
        metadata: { execution_owner: "03C SkillCoordinator" },
      };
    },
  });
  try {
    await coordinator.open();
    const execution = await coordinator.execute(
      "skill",
      { skill: "reviewer", arguments: { target: "src/runtime.ts" } },
      {
        runId: "run-06c",
        taskId: "task-06c",
        sessionId: "session-06c",
        sessionRevision: 1,
        workerRequestId: "worker-06c",
        toolCallId: "tool-call-06c",
      },
    );
    const reference = execution.output.outcome_reference as JsonObject;
    expect(reference.protocol).toBe("zyra.skill-coordinator-outcome/v1");
    expect((reference.metadata as JsonObject).executable_skill_cache).toBe(false);

    const e01 = new E01RuntimeCoordinator(
      "run-06c",
      "session-06c",
      "task-06c",
      "worker-06c",
    );
    const recorded = e01.recordSkillToolOutcome({
      toolCallId: "tool-call-06c",
      toolName: "skill",
      ok: true,
      summary: execution.summary,
      output: execution.output,
      artifacts: [{ artifact_id: "artifact-06c-review", kind: "review" }],
      error: null,
      metadata: execution.metadata,
      identity: {
        runId: "run-06c",
        taskId: "task-06c",
        sessionId: "session-06c",
        workerRequestId: "worker-06c",
        epoch: 0,
      },
      eventSequence: 17,
      occurredAt: "2026-07-21T08:20:00.000Z",
    });
    expect(recorded).not.toBeNull();
    expect(e01.skillMemory.listSkillOutcomes({ reusableOnly: true })).toHaveLength(1);
    expect(e01.skillMemory.health().invokes_skills).toBe(false);
    expect(coordinator.owns("skill")).toBe(true);
    expect(coordinator.toolSpecs().map((item) => item.name)).toContain("skill");
    expect(coordinator.toolSpecs().map((item) => item.name)).not.toContain("reuse_skill_outcome");

    const snapshot = e01.snapshot();
    const restored = new E01RuntimeCoordinator(
      "run-06c",
      "session-06c",
      "task-06c",
      "worker-06c",
    );
    restored.restore(snapshot);
    expect(restored.skillMemory.listSkillOutcomes({ reusableOnly: true })).toHaveLength(1);
    const restoredSkillMemory = restored.skillMemory.snapshot();
    expect(restoredSkillMemory.revision).toBe(snapshot.skillMemory!.revision);
    expect(restoredSkillMemory.outcomes.records).toEqual(snapshot.skillMemory!.outcomes.records);
    expect(restoredSkillMemory.signals.signals).toEqual(snapshot.skillMemory!.signals.signals);
  } finally {
    await coordinator.close();
    await rm(workspace, { recursive: true, force: true });
  }
});

test("disabling 06C leaves the real 03C skill invocation path available", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "zyra-06c-disable-"));
  const rootPath = join(workspace, "skills");
  const skillPath = join(rootPath, "noop");
  await mkdir(skillPath, { recursive: true });
  await writeFile(join(skillPath, "SKILL.md"), `---
id: noop
name: noop
description: Verify the loader remains independent
tools: {"allowed":[],"denied":[],"namespaces":[],"mcp_servers":[],"read_only":true,"inherit_parent":true,"maximum_calls":0,"maximum_parallel":1,"require_approval":[]}
execution: {"mode":"inline","timeout_ms":30000,"maximum_turns":1,"sandbox":"workspace_read","allow_network":false,"persist_transcript":true,"persist_artifacts":false}
enabled: true
---
Return a deterministic result.`, "utf8");
  const coordinator = new SkillCoordinator({
    workspaceRoot: workspace,
    epoch: 0,
    roots: [{
      sourceId: "disable-project-skills",
      kind: "project",
      rootPath,
      priority: 300,
      enabled: true,
      recursive: true,
      followSymlinks: false,
      maximumDepth: 8,
      includePatterns: ["**/SKILL.md"],
      excludePatterns: [],
      pluginId: null,
      revision: 1,
      metadata: {},
    }],
    watch: false,
    executor: async () => ({
      output: { ok: true },
      artifacts: [],
      inputTokens: 1,
      outputTokens: 1,
      costMicros: 0,
    }),
  });
  try {
    process.env.ZYRA_DISABLE_SKILL_MEMORY_RUNTIME = "1";
    await coordinator.open();
    const execution = await coordinator.execute("skill", { skill: "noop", arguments: {} });
    expect((execution.output.outcome_reference as JsonObject).protocol).toBe("zyra.skill-coordinator-outcome/v1");
    const e01 = new E01RuntimeCoordinator("disable-run", "disable-session", "disable-task", "disable-worker");
    expect(() => e01.recordSkillToolOutcome({
      toolCallId: "disable-call",
      toolName: "skill",
      ok: true,
      summary: execution.summary,
      output: execution.output,
      artifacts: [],
      error: null,
      metadata: execution.metadata,
      identity: {
        runId: "disable-run",
        taskId: "disable-task",
        sessionId: "disable-session",
        workerRequestId: "disable-worker",
        epoch: 0,
      },
      eventSequence: 1,
      occurredAt: "2026-07-21T08:30:00.000Z",
    })).toThrow("disabled");
    expect(coordinator.registry.resolve("noop").skillId).toBe("noop");
  } finally {
    await coordinator.close();
    await rm(workspace, { recursive: true, force: true });
  }
});

class CompactHost implements RuntimeHost {
  readonly events: RuntimeEvent[] = [];
  readonly artifacts: ArtifactReceipt[] = [];

  async emitEvent(event: RuntimeEvent): Promise<void> {
    this.events.push(event);
  }

  async checkpointState(): Promise<void> {}

  async executeBatch(
    _batch: ToolBatch,
    requests: ToolExecutionRequest[],
  ): Promise<ToolExecutionResponse[]> {
    return requests.map((request) => ({
      tool_call_id: request.toolCallId,
      ok: true,
      summary: `executed ${request.toolName}`,
      output: { content: "verified source output ".repeat(220) },
      artifacts: [],
      error: null,
      metadata: { host: "06c-compact-test" },
    }));
  }

  async externalize(request: ArtifactRequest): Promise<ArtifactReceipt> {
    const artifact = {
      artifact_id: `artifact-${this.artifacts.length + 1}`,
      kind: request.kind,
      uri: `memory://${request.requestId}`,
      title: request.title,
      metadata: request.metadata,
    };
    this.artifacts.push(artifact);
    return artifact;
  }

  isAborted(): boolean {
    return false;
  }
}

test("ClaudeRuntimeCore dynamically reaches 06C compact archive and restore projection", async () => {
  const host = new CompactHost();
  const input: RuntimeRunInput = {
    runId: "query-run-06c",
    taskId: "query-task-06c",
    nodeId: "query-node-06c",
    workerRequestId: "query-worker-06c",
    sessionId: "query-session-06c",
    messages: [{ role: "user", content: `Audit the source carefully. ${"constraint ".repeat(900)}` }],
    turns: [
      [{ tool_name: "read", arguments: { path: "source-a.ts" } }],
      [{ tool_name: "read", arguments: { path: "source-b.ts" } }],
    ],
    tools: [{
      name: "read",
      purpose: "read",
      source: "06c-test",
      input_schema: {
        type: "object",
        required: ["path"],
        properties: { path: { type: "string" } },
      },
      output_schema: {},
      metadata: { read_only: "true", concurrency_safe: "true" },
    }],
    config: {
      maxToolResultChars: 120,
      maxQueryContextChars: 240,
      runtimeConstraints: { force_compact_restore: true },
    },
  };
  const result = await new ClaudeRuntimeCore().run(input, host);
  expect(result.ok).toBe(true);
  const restored = host.events.filter((event) => event.phase === "skill_memory_compact_restored");
  if (!restored[0]) {
    throw new Error(JSON.stringify(host.events.filter((event) => event.phase.startsWith("skill_memory_compact"))));
  }
  expect(restored.length).toBeGreaterThan(0);
  expect((restored[0] as unknown as JsonObject).changes_next_provider_context).toBe(true);
  const skillState = (result.sessionSnapshot.e01Runtime as JsonObject).skillMemory as JsonObject;
  const restoreBridge = skillState.restoreBridge as JsonObject;
  expect(Number(restoreBridge.contextEpoch)).toBeGreaterThan(0);
  expect((restoreBridge.appliedBoundaryIds as unknown[]).length).toBeGreaterThan(0);
});
