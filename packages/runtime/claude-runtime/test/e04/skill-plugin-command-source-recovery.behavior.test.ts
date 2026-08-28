import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { test } from "bun:test";

import {
  PluginCoordinator,
  TypeScriptCapabilityRuntime,
  type AgentExecutionContext,
  type JsonObject,
  type PluginManifest,
  type PluginSourceRoot,
  type RuntimeRunInput,
  type RuntimeRunResult,
} from "../../src/index.ts";
import { digest } from "../../src/e02/index.ts";
import { SkillFrontmatterRuntime } from "../../src/skills/frontmatter-runtime.ts";
import { SkillRegistryRuntime } from "../../src/skills/registry-runtime.ts";
import { SkillReloadRuntime } from "../../src/skills/reload-runtime.ts";
import { SkillSourceRuntime } from "../../src/skills/source-runtime.ts";

const e04TemporaryRoot = join(process.cwd(), ".tmp");

function skillMarkdown(description: string): string {
  return `---
id: e04-fork-skill
name: e04-fork-skill
display_name: E04 fork skill
description: ${description}
version: 1.0.0
license: Apache-2.0
author: Zyra E04
tags: ["e04","source-recovery"]
aliases: ["e04-fork"]
arguments: {"target":{"type":"string","required":true,"description":"Target"}}
tools: {"allowed":["file_read"],"denied":["shell"],"namespaces":["builtin"],"mcp_servers":[],"read_only":true,"inherit_parent":true,"maximum_calls":3,"maximum_parallel":1,"require_approval":[]}
context: {"inherit_conversation":true,"inherit_system":true,"inherit_memory":true,"include_workspace_instructions":true,"include_mcp_instructions":true,"maximum_input_tokens":4096,"maximum_resource_tokens":1024,"maximum_output_tokens":1024,"compaction_strategy":"truncate_resources"}
execution: {"mode":"fork","agent":"e04-reader","timeout_ms":30000,"maximum_turns":4,"sandbox":"workspace_read","allow_network":false,"persist_transcript":true,"persist_artifacts":true}
resources: []
hooks: []
environment: {}
enabled: true
metadata: {"execution_id":"E04-D","source_custody":"claude-code-best:loadSkillsFromSkillsDir+executeForkedSkill"}
---
Inspect {{target}} in the forked child and return its canonical evidence.`;
}

function commandMarkdown(): string {
  return `---
name: e04-plugin-inspect
aliases: ["e04-pi"]
display_name: E04 plugin inspect
description: Render the E04 plugin command
usage: /e04-plugin-inspect <target>
category: e04
handler: {"kind":"plugin","id":"plugin:e04-plugin-inspect","plugin":"e04-plugin"}
permission: {"operation":"read","risk":"low","ask_in_interactive":false,"deny_in_sealed":false,"workspace_mutation":false,"network_access":false,"process_execution":false}
arguments: [{"name":"target","required":true,"positional":true,"type":"string","description":"Target"}]
metadata: {"execution_id":"E04-D"}
---
Inspect {{target}} through the committed plugin command registry.`;
}

function pluginManifest(version: string, hookId = "e04-hook-v1", withAgent = false): JsonObject {
  return {
    manifest_version: 1,
    id: "e04-plugin",
    name: "E04 Plugin",
    version,
    description: "E04 atomic hook fixture",
    license: "Apache-2.0",
    enabled: true,
    skills: [],
    commands: [{
      name: "e04-plugin-inspect",
      path: "commands/inspect.md",
      aliases: ["e04-pi"],
      hidden: false,
      permission: "read",
    }],
    hooks: [{
      id: hookId,
      event: "before_tool",
      path: `hooks/${hookId}.json`,
      priority: 100,
      timeout_ms: 2_000,
      fail_closed: true,
      can_mutate: false,
      tools: ["file_read"],
    }],
    agents: withAgent ? [{
      id: "e04-agent",
      path: "agents/e04-agent.md",
      description: "Triggers the staged integration failure",
      tools: { allowed: ["file_read"] },
    }] : [],
    mcp: {},
    permissions: { filesystem: ["read"], network: false },
    metadata: {
      execution_id: "E04-D",
      source_custody: "claude-code-best:loadPluginHooks",
    },
  };
}

async function createCapabilityWorkspace(prefix: string): Promise<{
  workspace: string;
  skillPath: string;
}> {
  await mkdir(e04TemporaryRoot, { recursive: true });
  const workspace = await mkdtemp(join(e04TemporaryRoot, prefix));
  const skillDirectory = join(workspace, "skills", "e04-fork-skill");
  const commandDirectory = join(workspace, "commands");
  const pluginDirectory = join(workspace, "plugins", "e04-plugin");
  await mkdir(skillDirectory, { recursive: true });
  await mkdir(commandDirectory, { recursive: true });
  await mkdir(join(pluginDirectory, "commands"), { recursive: true });
  await mkdir(join(pluginDirectory, "hooks"), { recursive: true });
  const skillPath = join(skillDirectory, "SKILL.md");
  await writeFile(skillPath, skillMarkdown("E04 fork revision one"), "utf8");
  await writeFile(join(commandDirectory, "project-inspect.md"), `---
name: e04-project-inspect
description: E04 project command
handler: {"kind":"builtin","id":"builtin:e02-help"}
permission: {"operation":"read","risk":"low"}
---
Inspect the project registry.`, "utf8");
  await writeFile(join(pluginDirectory, "commands", "inspect.md"), commandMarkdown(), "utf8");
  await writeFile(join(pluginDirectory, "hooks", "e04-hook-v1.json"), JSON.stringify({
    effect: "continue",
    reason: "e04 hook v1",
  }), "utf8");
  await writeFile(join(pluginDirectory, "plugin.json"), JSON.stringify(pluginManifest("1.0.0"), null, 2), "utf8");
  return { workspace, skillPath };
}

function runtimeInput(workspace: string, restoredState: JsonObject | null = null): RuntimeRunInput {
  return {
    runId: "e04-skill-run",
    taskId: "e04-skill-task",
    nodeId: "e04-skill-node",
    sessionId: "e04-skill-session",
    workerRequestId: "e04-skill-worker",
    messages: [{ role: "user", content: "Run the E04 source-recovered skill." }],
    turns: [],
    tools: [],
    config: {
      permissionPolicy: { mode: "default", default_effect: "allow" },
      runtimeConstraints: {
        workspaceRoot: workspace,
        watchSkills: false,
        watchPlugins: false,
        typescriptSkillRoots: [{
          source_id: "e04-skills",
          kind: "managed",
          path: "skills",
          priority: 900,
          required: true,
          recursive: true,
          maximum_depth: 8,
        }],
        typescriptCommandRoots: [{
          root_id: "e04-commands",
          source_kind: "managed",
          path: "commands",
          source_priority: 900,
          required: true,
          recursive: true,
          maximum_depth: 8,
        }],
        typescriptPluginRoots: [{
          root_id: "e04-plugins",
          source_kind: "managed",
          path: "plugins",
          priority: 900,
          required: true,
          recursive: true,
          maximum_depth: 8,
        }],
      },
    },
    restoredState,
    metadata: { session_revision: 4, e04_slice: "04d" },
  } as RuntimeRunInput;
}

function childResult(): RuntimeRunResult {
  return {
    ok: true,
    stoppedReason: "completed",
    turnCount: 1,
    toolCallCount: 1,
    contextCompactionCount: 0,
    stepSummaries: ["forked E04 skill completed"],
    artifacts: [],
    sessionSnapshot: { child: true },
    metadata: { cost_micros: "17", e04_child: "true" },
  } as RuntimeRunResult;
}

function executionContext(input: RuntimeRunInput, calls: RuntimeRunInput[]): AgentExecutionContext {
  return {
    parentInput: input,
    host: {} as AgentExecutionContext["host"],
    runChild: async (child) => {
      calls.push(child);
      return childResult();
    },
  };
}

test("e04-skill-plugin-command", async () => {
  const fixture = await createCapabilityWorkspace("zyra-e04-skill-capability-");
  const input = runtimeInput(fixture.workspace);
  input.restoredState = { parent_only_snapshot: true };
  Object.assign(input.config.runtimeConstraints as JsonObject, {
    requires_delivery_artifact: true,
  });
  Object.assign(input.metadata as JsonObject, {
    delivery_contract: {
      workspace_mutation_required: true,
      required_paths: ["deliverables/parent-only.md"],
    },
    task_handoff_progress: {
      requiredDeliveryMissing: true,
      providerRounds: 9,
      preDeliveryObservationCount: 7,
    },
    task_handoff_semantic_stall: {
      reasoningLoopDetections: 3,
      redirectCount: 2,
    },
  });
  const childCalls: RuntimeRunInput[] = [];
  const capabilities = await TypeScriptCapabilityRuntime.open(input);
  try {
    const listed = await capabilities.execute("list_skills", {}, undefined, { toolCallId: "e04-list-skills-1" });
    assert.equal((listed.output.skills as JsonObject[])[0]?.name, "e04-fork-skill");
    assert.match(listed.metadata.source_custody, /loadSkillsFromSkillsDir/);

    const invoked = await capabilities.execute("skill", {
      skill: "e04-fork-skill",
      arguments: { target: "docs/e04-evidence.md" },
    }, executionContext(input, childCalls), { toolCallId: "e04-invoke-skill-1" });
    assert.equal(childCalls.length, 1);
    assert.match(childCalls[0]!.taskId, /:skill:e04-fork-skill:/);
    assert.equal(childCalls[0]!.restoredState, null);
    assert.deepEqual(childCalls[0]!.turns, []);
    assert.equal(childCalls[0]!.config.maxTurns, 4);
    assert.equal((childCalls[0]!.config.runtimeConstraints as JsonObject).skill_network_allowed, false);
    assert.equal(
      (childCalls[0]!.config.runtimeConstraints as JsonObject).requires_delivery_artifact,
      false,
    );
    assert.deepEqual((childCalls[0]!.metadata as JsonObject).delivery_contract, {});
    assert.deepEqual((childCalls[0]!.metadata as JsonObject).task_handoff_progress, {});
    assert.deepEqual((childCalls[0]!.metadata as JsonObject).task_handoff_semantic_stall, {});
    assert.deepEqual(
      (childCalls[0]!.config.runtimeConstraints as JsonObject).skill_ancestry,
      ["e04-fork-skill"],
    );
    assert.equal(
      (childCalls[0]!.config.runtimeConstraints as JsonObject).skill_depth_remaining,
      0,
    );
    assert.equal(
      ((childCalls[0]!.config.runtimeConstraints as JsonObject).skill_tool_scope as JsonObject).readOnly,
      true,
    );
    assert.equal(childCalls[0]!.messages.at(-2)?.role, "system");
    assert.match(String(childCalls[0]!.messages.at(-2)?.content), /already executing.*e04-fork-skill/i);
    assert.match(String(childCalls[0]!.messages.at(-2)?.content), /sole objective is this bound skill body/i);
    assert.match(String(childCalls[0]!.messages.at(-2)?.content), /Do not invoke sibling skills or agents/i);
    assert.match(String(childCalls[0]!.messages.at(-2)?.content), /Return immediately once the bounded skill result/i);
    assert.equal(childCalls[0]!.messages.at(-1)?.role, "user");
    assert.match(String(childCalls[0]!.messages.at(-1)?.content), /docs\/e04-evidence\.md/);
    assert.equal(
      ((childCalls[0]!.messages.at(-1)?.metadata as JsonObject).skill_arguments as JsonObject).target,
      "docs/e04-evidence.md",
    );
    const invocation = invoked.output.invocation as JsonObject;
    assert.equal(invocation.status, "completed");
    assert.equal((invocation.metadata as JsonObject).source_custody, "claude-code-best:executeForkedSkill");

    const plugins = await capabilities.execute("list_plugins", {}, undefined, { toolCallId: "e04-list-plugins-1" });
    assert.equal((plugins.output.plugins as JsonObject[])[0]?.plugin_id, "e04-plugin");
    assert.match(plugins.metadata.source_custody, /loadPluginHooks/);
    const pluginCommand = await capabilities.execute("plugin_command", {
      plugin_id: "e04-plugin",
      command: "e04-plugin-inspect",
      arguments: { target: "source-custody" },
    }, undefined, { toolCallId: "e04-plugin-command-1" });
    const rendered = ((pluginCommand.output.output as JsonObject).rendered_command as string);
    assert.match(rendered, /source-custody/);

    const commands = await capabilities.execute("list_commands", {}, undefined, { toolCallId: "e04-list-commands-1" });
    assert.ok((commands.output.commands as JsonObject[]).some((command) => command.name === "e04-plugin-inspect"));
    assert.match(commands.metadata.source_custody, /plugin-command-dispatch/);

    const revisionBefore = capabilities.e02.skills.registry.revision;
    await writeFile(fixture.skillPath, skillMarkdown("E04 fork revision two"), "utf8");
    await capabilities.execute("reload_skills", {}, undefined, { toolCallId: "e04-reload-skills-1" });
    assert.equal(capabilities.e02.skills.registry.revision, revisionBefore + 1);
    assert.equal(capabilities.e02.skills.registry.resolve("e04-fork-skill").descriptor.description, "E04 fork revision two");
    assert.equal(capabilities.e02.skills.snapshot().journal.records.length, 1);
  } finally {
    await capabilities.close();
    await rm(fixture.workspace, { recursive: true, force: true });
  }
});

test("e04 skill directory loader owns discovery parse filter and atomic registration", async () => {
  const workspace = await mkdtemp(join(e04TemporaryRoot, "zyra-e04-loader-"));
  const skillDirectory = join(workspace, "skills", "retained-loader");
  await mkdir(skillDirectory, { recursive: true });
  await writeFile(join(skillDirectory, "SKILL.md"), skillMarkdown("Retained loader control flow"), "utf8");
  const registry = new SkillRegistryRuntime();
  const reload = new SkillReloadRuntime({
    sources: new SkillSourceRuntime({ workspaceRoot: workspace }),
    parser: new SkillFrontmatterRuntime(),
    registry,
  });
  const roots = [{
    sourceId: "e04-loader-root",
    kind: "project" as const,
    rootPath: join(workspace, "skills"),
    priority: 100,
    enabled: true,
    recursive: true,
    followSymlinks: false,
    maximumDepth: 8,
    includePatterns: [],
    excludePatterns: [],
    pluginId: null,
    revision: 1,
    metadata: { source_custody: "claude-code-best:loadSkillsFromSkillsDir" },
  }];
  try {
    const loaded = await reload.loadSkillsFromSkillsDir({
      roots,
      expectedRevision: 0,
      metadata: { trigger: "e04-retained-loader" },
    });
    assert.equal(loaded.scan.sources.length, 1);
    assert.equal(loaded.scan.sources[0]?.manifestPath, join(skillDirectory, "SKILL.md"));
    assert.equal(loaded.scan.descriptors.length, 1);
    assert.equal(loaded.scan.descriptors[0]?.description, "Retained loader control flow");
    assert.equal(loaded.revision?.revision, 1);
    assert.equal(registry.resolve("e04-fork-skill").descriptor.body.includes("{{target}}"), true);

    await assert.rejects(
      reload.loadSkillsFromSkillsDir({ roots, expectedRevision: 0 }),
      /expected revision 0, current 1/,
    );
    assert.equal(registry.revision, 1);
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }
});

test("e04-skill-fork-failure", async () => {
  const fixture = await createCapabilityWorkspace("zyra-e04-skill-failure-");
  const input = runtimeInput(fixture.workspace);
  const capabilities = await TypeScriptCapabilityRuntime.open(input);
  const context: AgentExecutionContext = {
    parentInput: input,
    host: {} as AgentExecutionContext["host"],
    runChild: async () => ({
      ...childResult(),
      ok: false,
      stoppedReason: "model_stream_failed",
      turnCount: 0,
      toolCallCount: 0,
      stepSummaries: [],
    }),
  };
  try {
    await assert.rejects(capabilities.execute("skill", {
      skill: "e04-fork-skill",
      arguments: { target: "failure" },
    }, context, { toolCallId: "e04-fork-failure-call" }), /skill_child_run_failed|model_stream_failed/);
    const records = capabilities.e02.skills.snapshot().journal.records;
    assert.equal(records.length, 1);
    assert.equal(records[0]?.status, "failed");
    assert.equal(records[0]?.result, null);
  } finally {
    await capabilities.close();
    await rm(fixture.workspace, { recursive: true, force: true });
  }
});

test("e04 skill ancestry rejects cycles and preserves bounded non-cyclic composition", async () => {
  const fixture = await createCapabilityWorkspace("zyra-e04-skill-ancestry-");
  const input = runtimeInput(fixture.workspace);
  const capabilities = await TypeScriptCapabilityRuntime.open(input);
  try {
    const recursiveParent = runtimeInput(fixture.workspace);
    Object.assign(recursiveParent.config.runtimeConstraints as JsonObject, {
      skill_id: "e04-fork-skill",
      skill_ancestry: ["e04-fork-skill"],
      skill_depth_remaining: 1,
    });
    const recursiveCalls: RuntimeRunInput[] = [];
    await assert.rejects(
      capabilities.execute("skill", {
        skill: "e04-fork-skill",
        arguments: { target: "recursive" },
      }, executionContext(recursiveParent, recursiveCalls), { toolCallId: "e04-recursive-call" }),
      /skill_recursive_invocation_denied|already active in the current skill ancestry/,
    );
    assert.equal(recursiveCalls.length, 0);

    const exhaustedParent = runtimeInput(fixture.workspace);
    Object.assign(exhaustedParent.config.runtimeConstraints as JsonObject, {
      skill_id: "outer-skill",
      skill_ancestry: ["outer-skill"],
      skill_depth_remaining: 0,
    });
    const exhaustedCalls: RuntimeRunInput[] = [];
    await assert.rejects(
      capabilities.execute("skill", {
        skill: "e04-fork-skill",
        arguments: { target: "exhausted" },
      }, executionContext(exhaustedParent, exhaustedCalls), { toolCallId: "e04-depth-exhausted-call" }),
      /skill_nested_invocation_denied|does not permit invoking nested skill/,
    );
    assert.equal(exhaustedCalls.length, 0);

    const composableParent = runtimeInput(fixture.workspace);
    Object.assign(composableParent.config.runtimeConstraints as JsonObject, {
      skill_id: "outer-skill",
      skill_ancestry: ["outer-skill"],
      skill_depth_remaining: 1,
    });
    const composedCalls: RuntimeRunInput[] = [];
    await capabilities.execute("skill", {
      skill: "e04-fork-skill",
      arguments: { target: "bounded-composition" },
    }, executionContext(composableParent, composedCalls), { toolCallId: "e04-bounded-composition-call" });
    assert.equal(composedCalls.length, 1);
    assert.deepEqual(
      (composedCalls[0]!.config.runtimeConstraints as JsonObject).skill_ancestry,
      ["outer-skill", "e04-fork-skill"],
    );
    assert.equal(
      (composedCalls[0]!.config.runtimeConstraints as JsonObject).skill_depth_remaining,
      0,
    );
  } finally {
    await capabilities.close();
    await rm(fixture.workspace, { recursive: true, force: true });
  }
});

test("e04 repeated skill calls reuse deterministic context but keep distinct invocation records", async () => {
  const fixture = await createCapabilityWorkspace("zyra-e04-skill-repeat-");
  const input = runtimeInput(fixture.workspace);
  const calls: RuntimeRunInput[] = [];
  const capabilities = await TypeScriptCapabilityRuntime.open(input);
  try {
    const argumentsValue = {
      skill: "e04-fork-skill",
      arguments: { target: "same-context" },
    };
    const first = await capabilities.execute(
      "skill",
      argumentsValue,
      executionContext(input, calls),
      { toolCallId: "e04-repeat-call-one" },
    );
    const second = await capabilities.execute(
      "skill",
      argumentsValue,
      executionContext(input, calls),
      { toolCallId: "e04-repeat-call-two" },
    );
    assert.equal(first.output.composition_id, second.output.composition_id);
    const snapshot = capabilities.e02.skills.snapshot();
    assert.equal(snapshot.context.compositions.length, 1);
    assert.equal(snapshot.journal.records.length, 2);
    assert.notEqual(
      snapshot.journal.records[0]?.invocationId,
      snapshot.journal.records[1]?.invocationId,
    );
  } finally {
    await capabilities.close();
    await rm(fixture.workspace, { recursive: true, force: true });
  }
});

function pluginRoot(workspace: string): PluginSourceRoot {
  return {
    rootId: "e04-atomic-plugin-root",
    rootPath: join(workspace, "plugins"),
    sourceKind: "managed",
    priority: 900,
    required: true,
    recursive: true,
    maximumDepth: 8,
    enabled: true,
    metadata: { e04_slice: "04d" },
  };
}

function hookContext(hookCallId: string): Parameters<PluginCoordinator["beforeTool"]>[0] {
  const argumentsValue = { path: "docs/e04.md" };
  return {
    runId: "e04-hook-run",
    taskId: "e04-hook-task",
    sessionId: "e04-hook-session",
    sessionRevision: 1,
    workerRequestId: "e04-hook-worker",
    toolCallId: hookCallId,
    toolName: "file_read",
    namespace: "builtin",
    serverId: "",
    operation: "read",
    arguments: argumentsValue,
    argumentsDigest: digest(argumentsValue),
    metadata: { e04_slice: "04d" },
  };
}

test("e04-skill-hook-failure", async () => {
  const fixture = await createCapabilityWorkspace("zyra-e04-plugin-atomic-");
  const manifestPath = join(fixture.workspace, "plugins", "e04-plugin", "plugin.json");
  const pluginDirectory = join(fixture.workspace, "plugins", "e04-plugin");
  await mkdir(join(pluginDirectory, "agents"), { recursive: true });
  await writeFile(join(pluginDirectory, "agents", "e04-agent.md"), "E04 agent", "utf8");
  await writeFile(join(pluginDirectory, "hooks", "e04-hook-v2.json"), "{}", "utf8");
  await writeFile(join(pluginDirectory, "hooks", "e04-hook-v3.json"), "{}", "utf8");
  const integrated: string[] = [];
  const integration = {
    addSkills: async (manifest: PluginManifest) => ({ plugin: manifest.pluginId }),
    addCommands: async (manifest: PluginManifest) => ({ plugin: manifest.pluginId }),
    addHooks: async (manifest: PluginManifest) => {
      integrated.push(`staged:${manifest.version}:${manifest.hooks.map((hook) => hook.hookId).join(",")}`);
      return { staged: manifest.hooks.length };
    },
    addAgents: async (manifest: PluginManifest) => {
      if (manifest.version === "3.0.0") throw new Error("e04 staged agent integration failed");
      return { agents: manifest.agents.length };
    },
    addMcpServers: async (manifest: PluginManifest) => ({ servers: manifest.mcpServers.length }),
    remove: async () => undefined,
  };
  const executedHooks: string[] = [];
  const hookExecutor = async ({ hook }: Parameters<NonNullable<ConstructorParameters<typeof PluginCoordinator>[0]["hookExecutor"]>>[0]) => {
    executedHooks.push(hook.hookId);
    return { effect: "continue" as const, reason: hook.hookId };
  };
  const coordinator = new PluginCoordinator({
    workspaceRoot: fixture.workspace,
    roots: [pluginRoot(fixture.workspace)],
    integration,
    hookExecutor,
    invokeCommand: async () => ({}),
    watch: false,
  });
  let restored: PluginCoordinator | null = null;
  try {
    await coordinator.open();
    const firstRevision = coordinator.snapshot().hookRevision;
    await coordinator.beforeTool(hookContext("e04-hook-call-v1"));
    assert.deepEqual(executedHooks, ["e04-hook-v1"]);

    await writeFile(manifestPath, JSON.stringify(pluginManifest("2.0.0", "e04-hook-v2"), null, 2), "utf8");
    await coordinator.reload({ reason: "e04_atomic_success" });
    assert.equal(coordinator.snapshot().hookRevision, firstRevision + 1);
    executedHooks.length = 0;
    await coordinator.beforeTool(hookContext("e04-hook-call-v2"));
    assert.deepEqual(executedHooks, ["e04-hook-v2"]);

    const committed = coordinator.snapshot();
    await writeFile(manifestPath, JSON.stringify(pluginManifest("3.0.0", "e04-hook-v3", true), null, 2), "utf8");
    await assert.rejects(coordinator.reload({ reason: "e04_atomic_failure" }), /plugin load failed|agent integration failed/i);
    assert.equal(coordinator.snapshot().hookRevision, committed.hookRevision);
    assert.equal(coordinator.snapshot().hookRegistrations[0]?.hook.hookId, "e04-hook-v2");
    executedHooks.length = 0;
    await coordinator.beforeTool(hookContext("e04-hook-call-after-failure"));
    assert.deepEqual(executedHooks, ["e04-hook-v2"]);

    await coordinator.close();
    restored = new PluginCoordinator({
      workspaceRoot: fixture.workspace,
      roots: [pluginRoot(fixture.workspace)],
      integration,
      hookExecutor,
      invokeCommand: async () => ({}),
      watch: false,
      snapshot: committed,
    });
    await restored.open();
    executedHooks.length = 0;
    await restored.beforeTool(hookContext("e04-hook-call-restored"));
    assert.deepEqual(executedHooks, ["e04-hook-v2"]);
    assert.ok(restored.snapshot().hookRevision > committed.hookRevision);
    assert.ok(integrated.some((entry) => entry.includes("2.0.0:e04-hook-v2")));
  } finally {
    await restored?.close();
    await coordinator.close();
    await rm(fixture.workspace, { recursive: true, force: true });
  }
});

test("e04-skill-reload", async () => {
  const fixture = await createCapabilityWorkspace("zyra-e04-skill-restore-");
  const input = runtimeInput(fixture.workspace);
  const first = await TypeScriptCapabilityRuntime.open(input);
  let second: TypeScriptCapabilityRuntime | null = null;
  try {
    await first.execute("skill", {
      skill: "e04-fork-skill",
      arguments: { target: "restore" },
    }, executionContext(input, []), { toolCallId: "e04-restore-invoke" });
    const snapshot = first.snapshot();
    await first.close();
    second = await TypeScriptCapabilityRuntime.open(runtimeInput(fixture.workspace, {
      e02: snapshot as unknown as JsonObject,
    }));
    const listed = await second.execute("list_skills", {}, undefined, { toolCallId: "e04-restore-list" });
    assert.equal((listed.output.skills as JsonObject[])[0]?.name, "e04-fork-skill");
    assert.equal(second.e02.skills.snapshot().journal.records.length, 1);
    assert.equal(second.e02.skills.snapshot().journal.records[0]?.status, "acknowledged");
    assert.equal(second.e02.runtime.epoch, snapshot.runtime.epoch + 1);
  } finally {
    await second?.close();
    await first.close();
    await rm(fixture.workspace, { recursive: true, force: true });
  }
});

test("e04-skill-disable", async () => {
  const fixture = await createCapabilityWorkspace("zyra-e04-skill-disable-");
  const input = runtimeInput(fixture.workspace);
  const capabilities = await TypeScriptCapabilityRuntime.open(input);
  const previous = process.env.ZYRA_DISABLE_E04_SKILL_SOURCE_RUNTIME;
  try {
    const normal = await capabilities.execute("list_skills", {}, undefined, { toolCallId: "e04-disable-normal" });
    assert.equal((normal.output.skills as JsonObject[]).length, 1);
    process.env.ZYRA_DISABLE_E04_SKILL_SOURCE_RUNTIME = "1";
    await assert.rejects(
      capabilities.execute("list_commands", {}, undefined, { toolCallId: "e04-disable-kill" }),
      /skill\/plugin\/command source runtime is disabled|no legacy or Python fallback/i,
    );
  } finally {
    if (previous === undefined) delete process.env.ZYRA_DISABLE_E04_SKILL_SOURCE_RUNTIME;
    else process.env.ZYRA_DISABLE_E04_SKILL_SOURCE_RUNTIME = previous;
    await capabilities.close();
    await rm(fixture.workspace, { recursive: true, force: true });
  }
});
