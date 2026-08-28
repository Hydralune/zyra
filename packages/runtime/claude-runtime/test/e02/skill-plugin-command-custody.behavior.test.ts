import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "bun:test";

import {
  CommandCompletionRuntime,
  CommandDescriptorRuntime,
  CommandDispatchRuntime,
  CommandHelpRuntime,
  CommandHistoryRuntime,
  CommandRegistryRuntime,
  LocalCommandRuntime,
  PluginCacheRuntime,
  PluginCapabilityRuntime,
  PluginDependencyRuntime,
  PluginHookRuntime,
  PluginManifestRuntime,
  PluginRuntime,
  PluginSupplyChainRuntime,
  SkillCoordinator,
  SkillContextRuntime,
  SkillFrontmatterRuntime,
  SkillInvocationJournal,
  SkillInvocationRuntime,
  SkillRegistryRuntime,
  SkillReloadRuntime,
  SkillResourceRuntime,
  SkillRootRuntime,
  SkillSearchRuntime,
  SkillSourceRuntime,
  SkillWatcherRuntime,
  type CommandDescriptor,
  type CommandInvocationRequest,
  type JsonObject,
  type PluginHookExecutorOutput,
  type PluginManifest,
  type PluginSupplyChainPolicy,
  type SkillDescriptor,
  type SkillInvocationRequest,
  type SkillParentContext,
  type SkillSourceFile,
  type SkillSourceRoot,
  type SkillToolScope,
} from "../../src/index.ts";
import { digest } from "../../src/e02/index.ts";

const instant = "2026-07-17T08:00:00.000Z";

test("plugin supply-chain restore migrates only the empty legacy regex policy", () => {
  const policy: PluginSupplyChainPolicy = {
    workspaceRoot: "G:/workspace",
    allowedExtensions: [".ts"],
    deniedExtensions: [".exe"],
    maximumFiles: 100,
    maximumFileBytes: 1024,
    maximumTotalBytes: 4096,
    allowSymlinks: false,
    allowNativeBinaries: false,
    allowPackageScripts: false,
    requireLicense: false,
    forbiddenPathFragments: [".git/objects"],
    forbiddenContentPatterns: ["child_process\\.execSync\\("],
    metadata: { owner: "typescript-plugin-coordinator" },
  };
  const current = new PluginSupplyChainRuntime({ policy }).snapshot();
  const { digest: _currentDigest, ...currentWithoutDigest } = current;
  const legacyWithoutDigest = {
    ...currentWithoutDigest,
    policy: {
      ...current.policy,
      forbiddenContentPatterns: ["child_process.execSync("],
    },
  };
  const legacy = {
    ...legacyWithoutDigest,
    digest: digest(legacyWithoutDigest),
  };
  const restored = new PluginSupplyChainRuntime({ policy, snapshot: legacy });
  assert.equal(restored.snapshot().revision, 0);
  assert.throws(
    () => new PluginSupplyChainRuntime({
      policy,
      snapshot: {
        ...legacy,
        revision: 1,
        digest: digest({ ...legacyWithoutDigest, revision: 1 }),
      },
    }),
    /policy changed/i,
  );
});

test("e02.live.skill.disk-reload atomically observes add change and delete before the next invocation", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "zyra-e02-live-skill-"));
  const rootPath = join(workspace, "skills");
  const alphaDirectory = join(rootPath, "alpha");
  const betaDirectory = join(rootPath, "beta");
  await mkdir(alphaDirectory, { recursive: true });
  await writeFile(join(alphaDirectory, "SKILL.md"), skillMarkdown({
    id: "live-alpha",
    description: "alpha revision one",
  }), "utf8");
  const roots: SkillSourceRoot[] = [{
    sourceId: "live-project-skills",
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
    metadata: { execution_id: "E02", live_disk_reload: true },
  }];
  const coordinator = new SkillCoordinator({
    workspaceRoot: workspace,
    epoch: 1,
    roots,
    watch: false,
    executor: async () => ({
      output: { ok: true },
      artifacts: [],
      inputTokens: 0,
      outputTokens: 0,
      costMicros: 0,
    }),
  });
  try {
    await coordinator.open();
    assert.equal(coordinator.registry.revision, 1);
    assert.equal(coordinator.registry.resolve("live-alpha").descriptor.description, "alpha revision one");

    await writeFile(join(alphaDirectory, "SKILL.md"), skillMarkdown({
      id: "live-alpha",
      description: "alpha revision two",
    }), "utf8");
    await coordinator.execute("reload_skills", {}, {
      runId: "live-skill-run",
      taskId: "live-skill-task",
      sessionId: "live-skill-session",
      sessionRevision: 3,
      workerRequestId: "live-skill-worker",
      toolCallId: "live-skill-change",
    });
    assert.equal(coordinator.registry.revision, 2);
    assert.equal(coordinator.registry.resolve("live-alpha").descriptor.description, "alpha revision two");

    await mkdir(betaDirectory, { recursive: true });
    await writeFile(join(betaDirectory, "SKILL.md"), skillMarkdown({
      id: "live-beta",
      description: "beta added from disk",
    }), "utf8");
    await coordinator.execute("reload_skills", {}, { toolCallId: "live-skill-add" });
    assert.equal(coordinator.registry.revision, 3);
    assert.equal(coordinator.registry.resolve("live-beta").descriptor.description, "beta added from disk");

    await rm(alphaDirectory, { recursive: true, force: true });
    await coordinator.execute("reload_skills", {}, { toolCallId: "live-skill-delete" });
    assert.equal(coordinator.registry.revision, 4);
    assert.throws(() => coordinator.registry.resolve("live-alpha"), /not found/i);
    assert.equal(coordinator.registry.resolve("live-beta").descriptor.availability, "available");

    const history = coordinator.snapshot().reloadHistory;
    assert.ok(history.some((receipt) => receipt.updated.includes("live-alpha")));
    assert.ok(history.some((receipt) => receipt.added.includes("live-beta")));
    assert.ok(history.some((receipt) => receipt.removed.includes("live-alpha")));
    assert.equal(coordinator.health().python_skill_fallback, false);
  } finally {
    await coordinator.close();
    await rm(workspace, { recursive: true, force: true });
  }
});

test("e02 staged skill admission commits the admitted scan even if disk changes before commit", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "zyra-e02-staged-skill-"));
  const rootPath = join(workspace, "skills");
  const skillDirectory = join(rootPath, "staged");
  const manifestPath = join(skillDirectory, "SKILL.md");
  await mkdir(skillDirectory, { recursive: true });
  await writeFile(
    manifestPath,
    skillMarkdown({
      id: "staged-skill",
      description: "staged revision one",
    }),
    "utf8",
  );
  const coordinator = new SkillCoordinator({
    workspaceRoot: workspace,
    epoch: 1,
    roots: [{
      sourceId: "staged-project-skills",
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
      metadata: { execution_id: "E02", staged_admission: true },
    }],
    watch: false,
    executor: async () => ({
      output: { ok: true },
      artifacts: [],
      inputTokens: 0,
      outputTokens: 0,
      costMicros: 0,
    }),
  });
  try {
    await coordinator.open();
    await writeFile(
      manifestPath,
      skillMarkdown({
        id: "staged-skill",
        description: "staged revision two",
      }),
      "utf8",
    );
    const admitted = await coordinator.reloadSkillsWithAdmission(
      {
        runId: "staged-run",
        taskId: "staged-task",
        sessionId: "staged-session",
        sessionRevision: 2,
        workerRequestId: "staged-worker",
        toolCallId: "staged-update",
      },
      async (descriptors, scan) => {
        const proposed = descriptors.find(
          (descriptor) => descriptor.skillId === "staged-skill",
        );
        assert.equal(proposed?.description, "staged revision two");
        await writeFile(
          manifestPath,
          skillMarkdown({
            id: "staged-skill",
            description: "unadmitted revision three",
          }),
          "utf8",
        );
        return {
          admitted_descriptor_digest: proposed!.descriptorDigest,
          admitted_scan_digest: scan.digest,
        };
      },
    );
    const committed = coordinator.registry.resolve("staged-skill");
    assert.equal(committed.descriptor.description, "staged revision two");
    assert.equal(
      admitted.admission.admitted_descriptor_digest,
      committed.descriptor.descriptorDigest,
    );
    assert.ok(admitted.admission.admitted_scan_digest);

    await coordinator.execute(
      "reload_skills",
      {},
      { toolCallId: "load-later-unadmitted-disk-revision" },
    );
    assert.equal(
      coordinator.registry.resolve("staged-skill").descriptor.description,
      "unadmitted revision three",
    );
  } finally {
    await coordinator.close();
    await rm(workspace, { recursive: true, force: true });
  }
});

function skillSource(input: {
  skillId: string;
  sourceId?: string;
  sourceKind?: SkillSourceFile["sourceKind"];
  sourcePriority?: number;
  pluginId?: string | null;
  directory?: string;
  markdown?: string;
}): SkillSourceFile {
  const directory = input.directory ?? `G:/workspace/skills/${input.skillId}`;
  const manifestPath = join(directory, "SKILL.md");
  return {
    sourceId: input.sourceId ?? `source-${input.skillId}`,
    sourceKind: input.sourceKind ?? "project",
    sourcePriority: input.sourcePriority ?? 300,
    pluginId: input.pluginId ?? null,
    skillDirectory: directory,
    manifestPath,
    relativePath: "SKILL.md",
    realPath: manifestPath,
    sizeBytes: Buffer.byteLength(input.markdown ?? "", "utf8"),
    modifiedAtMs: 1_783_900_000_000,
    inode: null,
    contentDigest: digest(input.markdown ?? input.skillId),
    discoveredAt: instant,
    metadata: { execution_id: "E02" },
  };
}

function skillMarkdown(input: {
  id: string;
  name?: string;
  description?: string;
  body?: string;
  mode?: "inline" | "fork" | "background";
  enabled?: boolean;
  allowed?: string[];
  denied?: string[];
  maximumCalls?: number;
  maximumInputTokens?: number;
  maximumResourceTokens?: number;
  tags?: string[];
  aliases?: string[];
  resources?: JsonObject[];
}): string {
  return `---
id: ${input.id}
name: ${input.name ?? input.id}
display_name: ${input.name ?? input.id} display
description: ${input.description ?? `Behavior skill ${input.id}`}
version: 2.1.0
license: Apache-2.0
author: Zyra Runtime
tags: ${JSON.stringify(input.tags ?? ["runtime", "custody"])}
aliases: ${JSON.stringify(input.aliases ?? [`${input.id}-alias`])}
arguments: {"target":{"type":"string","required":true,"description":"Target identifier"},"count":{"type":"integer","required":false,"default":1,"minimum":1,"maximum":10}}
tools: {"allowed":${JSON.stringify(input.allowed ?? ["file_read", "mcp__*"])},"denied":${JSON.stringify(input.denied ?? ["shell"])},"namespaces":["builtin","mcp"],"mcp_servers":["catalog"],"read_only":true,"inherit_parent":true,"maximum_calls":${input.maximumCalls ?? 6},"maximum_parallel":2,"require_approval":["mcp__catalog__replace"]}
context: {"inherit_conversation":true,"inherit_system":true,"inherit_memory":true,"include_workspace_instructions":true,"include_mcp_instructions":true,"maximum_input_tokens":${input.maximumInputTokens ?? 4096},"maximum_resource_tokens":${input.maximumResourceTokens ?? 2048},"maximum_output_tokens":1024,"compaction_strategy":"truncate_resources"}
execution: {"mode":"${input.mode ?? "inline"}","timeout_ms":30000,"maximum_turns":8,"sandbox":"workspace_read","allow_network":false,"persist_transcript":true,"persist_artifacts":true}
resources: ${JSON.stringify(input.resources ?? [])}
hooks: [{"id":"before-${input.id}","event":"before_invoke","module":"hooks/before.ts","timeout_ms":1000,"fail_closed":true,"mutation_allowed":false,"priority":10}]
environment: {"API_TOKEN":"credential:skill-token"}
enabled: ${input.enabled ?? true}
metadata: {"execution_id":"E02","canonical_owner":"typescript"}
---
${input.body ?? `Inspect {{target}} exactly {{count}} time(s), retain the evidence, and report the canonical result.`}`;
}

function descriptor(input: Parameters<typeof skillMarkdown>[0] & {
  sourcePriority?: number;
  sourceKind?: SkillSourceFile["sourceKind"];
  pluginId?: string | null;
  directory?: string;
}): SkillDescriptor {
  const markdown = skillMarkdown(input);
  const source = skillSource({
    skillId: input.id,
    sourcePriority: input.sourcePriority,
    sourceKind: input.sourceKind,
    pluginId: input.pluginId,
    directory: input.directory,
    markdown,
  });
  return new SkillFrontmatterRuntime({
    now: () => new Date(instant),
  }).parseText(source, markdown);
}

function broadScope(overrides: Partial<SkillToolScope> = {}): SkillToolScope {
  return {
    allowed: ["*"],
    denied: [],
    namespaces: ["*"],
    mcpServers: ["*"],
    readOnly: false,
    inheritParent: true,
    maximumCalls: 20,
    maximumParallel: 4,
    requireApproval: [],
    ...overrides,
  };
}

function parentContext(overrides: Partial<SkillParentContext> = {}): SkillParentContext {
  return {
    system: ["System policy: preserve canonical state."],
    conversation: [{ role: "user", content: "Inspect task-17." }],
    memory: [{ kind: "fact", value: "task-17 owns artifact-4" }],
    workspaceInstructions: ["Workspace policy: reads only."],
    mcpInstructions: ["MCP catalog is read-only for this task."],
    variables: { run_id: "run-skill", task_id: "task-skill" },
    metadata: { execution_id: "E02" },
    ...overrides,
  };
}

function invocationRequest(
  name: string,
  revision: number,
  overrides: Partial<SkillInvocationRequest> = {},
): SkillInvocationRequest {
  return {
    identity: {
      runId: `run-${name}`,
      taskId: `task-${name}`,
      sessionId: `session-${name}`,
      sessionRevision: 3,
      workerRequestId: `worker-${name}`,
      toolCallId: `call-${name}`,
      invocationId: "",
    },
    skillName: name,
    registryRevision: revision,
    arguments: { target: "task-17", count: 2 },
    parentContext: parentContext() as unknown as JsonObject,
    parentToolScope: broadScope(),
    workspaceRoot: "G:/workspace",
    metadata: { execution_id: "E02" },
    ...overrides,
  };
}

function pluginManifest(pluginId = "custody-plugin", overrides: Partial<PluginManifest> = {}): PluginManifest {
  const base = {
    manifestVersion: 1 as const,
    pluginId,
    name: `${pluginId} display`,
    version: "1.4.0",
    description: `Plugin ${pluginId}`,
    author: "Zyra Runtime",
    homepage: "https://plugins.example.test",
    license: "Apache-2.0",
    sourceKind: "project" as const,
    rootPath: `G:/workspace/.zyra/plugins/${pluginId}`,
    manifestPath: `G:/workspace/.zyra/plugins/${pluginId}/plugin.json`,
    enabled: true,
    minimumZyraVersion: "0.1.0",
    dependencies: [],
    skillRoots: [{
      sourceId: `${pluginId}-skills`,
      kind: "plugin" as const,
      rootPath: `G:/workspace/.zyra/plugins/${pluginId}/skills`,
      priority: 400,
      enabled: true,
      recursive: true,
      followSymlinks: false,
      maximumDepth: 8,
      includePatterns: ["**/SKILL.md"],
      excludePatterns: ["**/node_modules/**"],
      pluginId,
      revision: 1,
      metadata: {},
    }],
    commands: [{
      name: `${pluginId}-inspect`,
      path: `commands/inspect.md`,
      aliases: [`${pluginId}-i`],
      hidden: false,
      permission: "read",
      metadata: {},
    }],
    hooks: [{
      hookId: `${pluginId}-before-read`,
      event: "before_tool",
      path: "hooks/before-read.ts",
      command: null,
      priority: 100,
      timeoutMs: 2_000,
      failClosed: true,
      canMutate: true,
      toolPatterns: ["file_read", "mcp__*"],
      metadata: {},
    }],
    agents: [{
      agentId: `${pluginId}-worker`,
      path: "agents/worker.md",
      description: "Read-only plugin worker",
      model: null,
      toolScope: { allowed: ["file_read"] },
      metadata: {},
    }],
    mcpServers: [{
      serverId: `${pluginId}-mcp`,
      configPath: "mcp/server.json",
      config: { transport: "stdio" },
      enabledByDefault: true,
      permissionScope: { read: true },
    }],
    environmentHandles: { PLUGIN_TOKEN: "credential:plugin-token" },
    permissions: { filesystem: ["read"], network: false },
    metadata: { execution_id: "E02" },
  };
  const value = { ...base, ...overrides };
  return {
    ...value,
    manifestDigest: overrides.manifestDigest ?? digest(value),
  };
}

function commandDescriptor(input: {
  name: string;
  kind?: "builtin" | "local" | "skill" | "mcp_prompt" | "plugin" | "control";
  priority?: number;
  sourceId?: string;
  aliases?: string[];
  enabled?: boolean;
  hidden?: boolean;
  risk?: "low" | "medium" | "high" | "critical";
}): CommandDescriptor {
  return new CommandDescriptorRuntime().parseText({
    text: `---
name: ${input.name}
aliases: ${JSON.stringify(input.aliases ?? [`${input.name}-alias`])}
display_name: ${input.name} display
description: Execute ${input.name}
usage: /${input.name} <target>
examples: ["/${input.name} task-17"]
category: runtime
hidden: ${input.hidden ?? false}
enabled: ${input.enabled ?? true}
handler: {"kind":"${input.kind ?? "local"}","id":"${input.kind ?? "local"}:${input.name}","skill":"custody-skill","server":"catalog","prompt":"explain","plugin":"custody-plugin","control":"status"}
permission: {"operation":"command/${input.name}","risk":"${input.risk ?? "low"}","ask_in_interactive":false,"deny_in_sealed":true,"workspace_mutation":false,"network_access":false,"process_execution":${(input.kind ?? "local") === "local"},"scope":{"workspace":"G:/workspace"}}
arguments: [{"name":"target","required":true,"positional":true,"type":"string","description":"Target"}]
options: [{"name":"verbose","short":"v","type":"boolean","required":false,"repeatable":false,"default":false,"description":"Verbose output"}]
metadata: {"execution_id":"E02"}
---
Execute the command against the exact target.`,
    path: `G:/workspace/.zyra/commands/${input.name}.md`,
    sourceKind: "project",
    sourceId: input.sourceId ?? `source-${input.name}`,
    sourcePriority: input.priority ?? 300,
  });
}

function commandRequest(input: string, revision: number, overrides: Partial<CommandInvocationRequest> = {}): CommandInvocationRequest {
  const suffix = digest({ input, revision, overrides }).slice(0, 12);
  return {
    identity: {
      runId: `run-command-${suffix}`,
      taskId: `task-command-${suffix}`,
      sessionId: "session-command-custody",
      sessionRevision: 4,
      workerRequestId: `worker-command-${suffix}`,
      commandCallId: `command-call-${suffix}`,
    },
    input,
    commandName: null,
    arguments: [],
    options: {},
    registryRevision: revision,
    workspaceRoot: "G:/workspace",
    interactive: true,
    sealedAutonomous: false,
    metadata: { execution_id: "E02" },
    ...overrides,
  };
}

test("e02.custody.skills-plugins-commands.default-path", async () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const selected = descriptor({
    id: "custody-skill",
    name: "custody-skill",
    description: "Inspect a task through the TypeScript skill runtime",
    body: "Inspect {{target}} exactly {{count}} times and return durable evidence.",
    tags: ["runtime", "inspection", "custody"],
    aliases: ["inspect-custody", "custody-inspect"],
    allowed: ["file_read", "mcp__catalog__inspect"],
    denied: ["shell", "file_write"],
    maximumCalls: 5,
    maximumInputTokens: 4_096,
    maximumResourceTokens: 1_024,
  });
  assert.equal(selected.skillId, "custody-skill");
  assert.equal(selected.name, "custody-skill");
  assert.equal(selected.displayName, "custody-skill display");
  assert.equal(selected.description, "Inspect a task through the TypeScript skill runtime");
  assert.equal(selected.version, "2.1.0");
  assert.equal(selected.license, "Apache-2.0");
  assert.equal(selected.author, "Zyra Runtime");
  assert.deepEqual(selected.tags, ["runtime", "inspection", "custody"]);
  assert.deepEqual(selected.aliases, ["inspect-custody", "custody-inspect"]);
  assert.equal(selected.availability, "available");
  assert.equal(selected.disabledReason, null);
  assert.match(selected.body, /Inspect \{\{target\}\}/);
  assert.equal(selected.bodyDigest.length, 64);
  assert.equal(selected.frontmatterDigest.length, 64);
  assert.equal(selected.descriptorDigest.length, 64);
  assert.equal(selected.arguments.length, 2);
  const targetArgument = selected.arguments.find((argument) => argument.name === "target")!;
  const countArgument = selected.arguments.find((argument) => argument.name === "count")!;
  assert.equal(targetArgument.required, true);
  assert.equal(targetArgument.type, "string");
  assert.equal(countArgument.required, false);
  assert.equal(countArgument.type, "integer");
  assert.equal(countArgument.defaultValue, 1);
  assert.equal(countArgument.minimum, 1);
  assert.equal(countArgument.maximum, 10);
  assert.deepEqual(selected.toolScope.allowed, ["file_read", "mcp__catalog__inspect"]);
  assert.deepEqual(selected.toolScope.denied, ["shell", "file_write"]);
  assert.deepEqual(selected.toolScope.namespaces, ["builtin", "mcp"]);
  assert.deepEqual(selected.toolScope.mcpServers, ["catalog"]);
  assert.equal(selected.toolScope.readOnly, true);
  assert.equal(selected.toolScope.inheritParent, true);
  assert.equal(selected.toolScope.maximumCalls, 5);
  assert.equal(selected.toolScope.maximumParallel, 2);
  assert.deepEqual(selected.toolScope.requireApproval, ["mcp__catalog__replace"]);
  assert.equal(selected.context.maximumInputTokens, 4_096);
  assert.equal(selected.context.maximumResourceTokens, 1_024);
  assert.equal(selected.context.maximumOutputTokens, 1_024);
  assert.equal(selected.context.compactionStrategy, "truncate_resources");
  assert.equal(selected.execution.mode, "inline");
  assert.equal(selected.execution.sandbox, "workspace_read");
  assert.equal(selected.execution.allowNetwork, false);
  assert.equal(selected.execution.persistTranscript, true);
  assert.equal(selected.execution.persistArtifacts, true);
  assert.equal(selected.hooks.length, 1);
  assert.equal(selected.hooks[0]?.event, "before_invoke");
  assert.equal(selected.hooks[0]?.failClosed, true);
  assert.equal(selected.environmentHandles.API_TOKEN, "credential:skill-token");
  assert.equal(selected.metadata.canonical_owner, "typescript");
  assert.deepEqual(selected.warnings, []);
  const registry = new SkillRegistryRuntime({
    now,
    maximumRevisions: 8,
  });
  const revision = registry.commitRevision({
    baseRevision: 0,
    descriptors: [selected],
    metadata: { execution_id: "E02" },
  });
  assert.equal(revision.revision, 1);
  assert.equal(revision.parentRevisionId, null);
  assert.deepEqual(revision.added, ["custody-skill"]);
  assert.deepEqual(revision.updated, []);
  assert.deepEqual(revision.removed, []);
  assert.equal(revision.skills.length, 1);
  assert.equal(revision.skills[0]?.skillId, "custody-skill");
  assert.equal(revision.digest.length, 64);
  assert.equal(registry.revision, 1);
  const resolution = registry.resolve("inspect-custody", 1);
  assert.equal(resolution.skillId, "custody-skill");
  assert.equal(resolution.requestedName, "inspect-custody");
  assert.equal(resolution.revision, 1);
  assert.equal(resolution.resolutionKind, "alias");
  assert.equal(resolution.descriptor.descriptorDigest, selected.descriptorDigest);
  assert.equal(registry.list().length, 1);
  assert.deepEqual(registry.sourceSkills(selected.source.sourceId), ["custody-skill"]);
  const resources = new SkillResourceRuntime({
    workspaceRoot: "G:/workspace",
    allowOutsideWorkspace: false,
    now,
  });
  const executorCalls: JsonObject[] = [];
  const invocation = new SkillInvocationRuntime({
    registry,
    resources,
    now,
    executor: async (context) => {
      executorCalls.push({
        invocation_id: context.plan.invocationId,
        skill_id: context.plan.skillId,
        context_digest: digest(context.plan.context),
        tool_scope_digest: digest(context.plan.effectiveToolScope),
      });
      context.assertToolAllowed("file_read", "builtin", "", true);
      context.recordToolCall("file_read", { path: "README.md" });
      context.assertToolAllowed("mcp__catalog__inspect", "mcp", "catalog", true);
      context.recordToolCall("mcp__catalog__inspect", { target: "task-17" });
      return {
        output: {
          inspected: true,
          target: context.plan.arguments.target,
          calls: 2,
        },
        inputTokens: 180,
        outputTokens: 24,
        artifacts: [{ kind: "evidence", artifact_id: "artifact-17" }],
        metadata: { executor: "typescript" },
      };
    },
  });
  const request = invocationRequest("custody-skill", registry.revision, {
    arguments: { target: "task-17", count: 2 },
    parentToolScope: broadScope({
      denied: ["shell"],
      maximumCalls: 8,
    }),
  });
  const result = await invocation.invoke(request);
  assert.equal(result.status, "completed");
  assert.equal(result.skillId, "custody-skill");
  const output = result.output as JsonObject;
  assert.equal(output.inspected, true);
  assert.equal(output.target, "task-17");
  assert.equal(output.calls, 2);
  assert.equal(result.inputTokens, 180);
  assert.equal(result.outputTokens, 24);
  assert.equal(result.artifacts.length, 1);
  assert.equal(result.artifacts[0]?.artifact_id, "artifact-17");
  assert.equal(result.toolCalls, 2);
  assert.equal(result.failure, null);
  assert.ok(result.completedAt);
  assert.equal(result.metadata.executor, "typescript");
  assert.equal(executorCalls.length, 1);
  assert.equal(executorCalls[0]?.skill_id, "custody-skill");
  assert.equal(invocation.getResult(result.invocationId)?.status, "completed");
  assert.equal(invocation.inFlightPlans().length, 0);
  const search = new SkillSearchRuntime({ now });
  const searchSnapshot = search.rebuild(registry.snapshot());
  assert.equal(searchSnapshot.registryRevision, 1);
  assert.equal(searchSnapshot.documents.length, 1);
  const hits = search.search({
    text: "inspect custody task",
    tags: ["custody"],
    sourceKinds: ["project"],
    includeUnavailable: false,
    maximumResults: 10,
    minimumScore: 0,
    metadata: { execution_id: "E02" },
  });
  assert.equal(hits.searchedDocuments, 1);
  assert.equal(hits.filteredDocuments, 1);
  assert.equal(hits.hits.length, 1);
  assert.equal(hits.hits[0]?.skillId, "custody-skill");
  assert.ok(hits.hits[0]!.matchedTerms.includes("custody"));
  assert.equal(hits.digest.length, 64);
  assert.equal(search.suggest("inspect-c")[0]?.skillId, "custody-skill");
  const searchRestored = new SkillSearchRuntime({
    now,
    snapshot: search.snapshot(),
  });
  assert.equal(searchRestored.search({ text: "custody", minimumScore: 0 }).hits[0]?.skillId, "custody-skill");
});

test("e02.custody.skills-plugins-commands.failure-path", async () => {
  const parser = new SkillFrontmatterRuntime({
    maximumBodyBytes: 512,
    maximumFrontmatterBytes: 256,
    now: () => new Date(instant),
  });
  const baseSource = skillSource({
    skillId: "failure-skill",
    markdown: "---\nname: failure-skill\n---\nbody",
  });
  assert.throws(() => parser.parseText(baseSource, "---\nname: failure-skill\nbody"), /not terminated/i);
  assert.throws(() => parser.parseText(baseSource, "---\nname:\n---\nbody"), /name.*required/i);
  assert.throws(() => parser.parseText(baseSource, "---\nname: failure-skill\nexecution: {\"mode\":\"remote\"}\n---\nbody"), /execution mode/i);
  assert.throws(() => parser.parseText(baseSource, "---\nname: failure-skill\nexecution: {\"sandbox\":\"host\"}\n---\nbody"), /sandbox/i);
  assert.throws(() => parser.parseText(baseSource, "---\nname: failure-skill\narguments: {\"bad\":{\"type\":\"opaque\"}}\n---\nbody"), /unsupported type/i);
  assert.throws(() => parser.parseText(baseSource, `---\nname: failure-skill\ndescription: ${"x".repeat(300)}\n---\nbody`), /frontmatter exceeds/i);
  const first = descriptor({
    id: "failure-skill",
    name: "failure-skill",
    description: "Failure behavior",
    allowed: ["file_read"],
    denied: ["shell"],
    maximumCalls: 2,
  });
  const registry = new SkillRegistryRuntime({
    now: () => new Date(instant),
    maximumRevisions: 4,
  });
  const committed = registry.commitRevision({
    baseRevision: 0,
    descriptors: [first],
    metadata: { failure_path: true },
  });
  assert.equal(committed.revision, 1);
  assert.throws(() => registry.commitRevision({
    baseRevision: 0,
    descriptors: [first],
  }), /revision/i);
  assert.throws(() => registry.resolve("missing-skill"), /not found/i);
  assert.throws(() => registry.resolve("failure-skill", 99), /revision/i);
  const disabled = descriptor({
    id: "disabled-skill",
    name: "disabled-skill",
    enabled: false,
  });
  registry.commitRevision({
    baseRevision: 1,
    descriptors: [first, disabled],
  });
  assert.equal(registry.list({ availableOnly: true }).length, 1);
  assert.throws(() => registry.resolve("disabled-skill"), /disabled/i);
  const resources = new SkillResourceRuntime({
    workspaceRoot: "G:/workspace",
    allowOutsideWorkspace: false,
    now: () => new Date(instant),
  });
  const invocation = new SkillInvocationRuntime({
    registry,
    resources,
    now: () => new Date(instant),
    executor: async () => ({
      output: { should_not_run: true },
      inputTokens: 1,
      outputTokens: 1,
      artifacts: [],
      metadata: {},
    }),
  });
  await assert.rejects(invocation.invoke(invocationRequest("missing-skill", registry.revision)), /not found/i);
  await assert.rejects(invocation.invoke(invocationRequest("disabled-skill", registry.revision)), /disabled/i);
  await assert.rejects(invocation.invoke(invocationRequest("failure-skill", 1, {
    arguments: { count: 2 },
  })), /target|required/i);
  await assert.rejects(invocation.invoke(invocationRequest("failure-skill", 1, {
    arguments: { target: "task-17", unknown: true },
  })), /unknown/i);
  await assert.rejects(invocation.invoke(invocationRequest("failure-skill", 1, {
    arguments: { target: "task-17", count: 0 },
  })), /below 1|minimum|at least/i);
  const parent = broadScope({
    allowed: ["file_read"],
    denied: ["shell"],
    namespaces: ["builtin"],
    mcpServers: [],
    readOnly: true,
    maximumCalls: 1,
    maximumParallel: 1,
  });
  const child = broadScope({
    allowed: ["file_read", "file_write", "mcp__catalog__inspect"],
    denied: ["file_delete"],
    namespaces: ["builtin", "mcp"],
    mcpServers: ["catalog"],
    readOnly: false,
    maximumCalls: 5,
    maximumParallel: 3,
  });
  const bounded = invocation.applyToolScope(parent, child);
  assert.deepEqual(bounded.allowed, ["file_read"]);
  assert.ok(bounded.denied.includes("shell"));
  assert.ok(bounded.denied.includes("file_delete"));
  assert.deepEqual(bounded.namespaces, ["builtin"]);
  assert.deepEqual(bounded.mcpServers, []);
  assert.equal(bounded.readOnly, true);
  assert.equal(bounded.maximumCalls, 1);
  assert.equal(bounded.maximumParallel, 1);
  assert.throws(() => invocation.assertToolAllowed(bounded, 0, "file_write", "builtin", "", false), /not allowed|denied|read-only/i);
  assert.throws(() => invocation.assertToolAllowed(bounded, 0, "shell", "builtin", "", false), /denied|not allowed|read-only/i);
  assert.throws(() => invocation.assertToolAllowed(bounded, 0, "file_read", "other", "", true), /namespace/i);
  assert.throws(() => invocation.assertToolAllowed(bounded, 1, "file_read", "builtin", "", true), /budget|exhausted/i);
  const contextRuntime = new SkillContextRuntime({
    now: () => new Date(instant),
  });
  const tinyContextDescriptor = descriptor({
    id: "tiny-context",
    maximumInputTokens: 4,
    maximumResourceTokens: 2,
    body: "Required body content that exceeds the tiny context budget.",
  });
  assert.throws(() => contextRuntime.compose({
    descriptor: tinyContextDescriptor,
    parent: parentContext({
      system: ["system text that cannot all fit"],
      conversation: [],
      memory: [],
      workspaceInstructions: [],
      mcpInstructions: [],
    }),
    resources: [],
    arguments: { target: "task-17" },
  }), /required skill context|budget/i);
  const journal = new SkillInvocationJournal({
    epoch: 3,
    now: () => new Date(instant),
  });
  assert.throws(() => journal.restore({
    version: "zyra.skill-invocation-journal/v1",
    epoch: 2,
    revision: 0,
    sequence: 0,
    records: [],
    digest: "invalid",
    capturedAt: instant,
  } as any, 3), /digest|snapshot|epoch/i);
  const temporaryRoot = join(process.cwd(), ".tmp");
  await mkdir(temporaryRoot, { recursive: true });
  const badPluginRoot = await mkdtemp(join(temporaryRoot, "zyra-e02-plugin-failure-"));
  try {
    const manifestPath = join(badPluginRoot, "plugin.json");
    await writeFile(manifestPath, JSON.stringify({
      manifest_version: 1,
      id: "bad-plugin",
      name: "Bad Plugin",
      version: "1.0.0",
      commands: [{ name: "escape", path: "../outside.md" }],
    }), "utf8");
    const pluginParser = new PluginManifestRuntime({
      workspaceRoot: badPluginRoot,
      allowOutsideWorkspace: false,
    });
    await assert.rejects(pluginParser.parse(manifestPath, "project"), /outside|escape|workspace/i);
  } finally {
    await rm(badPluginRoot, { recursive: true, force: true });
  }
  const commandParser = new CommandDescriptorRuntime();
  assert.throws(() => commandParser.parseText({
    text: "---\nname: bad-command\nkind: unknown\n---\nbody",
    path: null,
    sourceKind: "project",
    sourceId: "bad-source",
    sourcePriority: 1,
  }), /handler kind/i);
  assert.throws(() => commandParser.parseText({
    text: "---\nname: bad-option\noptions: [{\"name\":\"verbose\",\"short\":\"vv\"}]\n---\nbody",
    path: null,
    sourceKind: "project",
    sourceId: "bad-source",
    sourcePriority: 1,
  }), /one character/i);
});

test("e02 bundled skill invocation preserves the declared nesting budget", () => {
  const markdown = `---
schema: zyra.skill/v1
name: pdf-analysis
description: Extract page-level PDF evidence
invocation: {"mode":"fork","agent":"DataWorker","max-skill-depth":0}
allowed-tools: ["builtin/file_read","builtin/shell"]
---
Extract PDF evidence without recursively invoking this skill.`;
  const parsed = new SkillFrontmatterRuntime({
    now: () => new Date(instant),
  }).parseText(skillSource({ skillId: "pdf-analysis", markdown }), markdown);
  assert.equal(parsed.execution.mode, "fork");
  assert.equal(parsed.execution.agent, "DataWorker");
  assert.equal(parsed.execution.maximumSkillDepth, 0);
  assert.deepEqual(parsed.toolScope.allowed, ["file_read", "shell"]);
  assert.deepEqual(parsed.toolScope.namespaces, ["builtin"]);
  assert.deepEqual(parsed.toolScope.mcpServers, []);
  assert.equal(parsed.toolScope.inheritParent, true);
  assert.equal(parsed.warnings.some((warning) => /allowed-tools/.test(warning)), false);
  assert.equal(parsed.warnings.some((warning) => /invocation/.test(warning)), false);
});

test("skill frontmatter preserves arguments, tool scope, hooks, resources, and unknown-key warnings", () => {
  const markdown = `---
id: structured-skill
name: structured-skill
display_name: Structured Skill
description: Parse every supported frontmatter boundary
version: 3.2.1
license: MIT
author: Runtime Team
tags: ["parser","behavior","typescript"]
aliases: ["structured","parse-skill"]
arguments: {"path":{"type":"path","required":true,"positional":true,"description":"Input path"},"mode":{"type":"enum","required":false,"default":"safe","enum":["safe","fast"]},"secret":{"type":"string","required":false,"sensitive":true}}
tools: {"allowed":["file_read","file_list"],"denied":["file_write"],"namespaces":["builtin"],"mcp_servers":[],"read_only":true,"inherit_parent":false,"maximum_calls":3,"maximum_parallel":1,"require_approval":[]}
context: {"inherit_conversation":false,"inherit_system":true,"inherit_memory":false,"include_workspace_instructions":true,"include_mcp_instructions":false,"include_files":["AGENTS.md"],"exclude_files":["secrets/**"],"maximum_input_tokens":2048,"maximum_resource_tokens":512,"maximum_output_tokens":256,"compaction_strategy":"reject"}
execution: {"mode":"fork","agent":"reader","model":"model-a","timeout_ms":12000,"maximum_turns":4,"maximum_cost_micros":5000,"working_directory":"docs","sandbox":"workspace_read","permission_mode":"sealed","allow_network":false,"persist_transcript":true,"persist_artifacts":false}
resources: [{"id":"guide","path":"references/guide.md","kind":"markdown","required":true,"maximum_bytes":4096,"charset":"utf-8","media_type":"text/markdown","metadata":{"priority":"high"}},{"id":"schema","path":"references/schema.json","kind":"json","required":false,"maximum_bytes":2048}]
hooks: [{"id":"before","event":"before_invoke","module":"hooks/before.ts","timeout_ms":500,"fail_closed":true,"mutation_allowed":false,"priority":20},{"id":"failure","event":"on_failure","command":"record-failure","timeout_ms":1000,"fail_closed":false,"mutation_allowed":false,"priority":5}]
environment: {"SERVICE_TOKEN":"credential:service"}
metadata: {"owner":"typescript","revision":3}
experimental_flag: true
---
# Structured behavior

Read the selected path and return structured evidence.`;
  const source = skillSource({
    skillId: "structured-skill",
    markdown,
  });
  const parsed = new SkillFrontmatterRuntime({
    now: () => new Date(instant),
  }).parseText(source, markdown);
  assert.equal(parsed.skillId, "structured-skill");
  assert.equal(parsed.name, "structured-skill");
  assert.equal(parsed.displayName, "Structured Skill");
  assert.equal(parsed.description, "Parse every supported frontmatter boundary");
  assert.equal(parsed.version, "3.2.1");
  assert.equal(parsed.license, "MIT");
  assert.equal(parsed.author, "Runtime Team");
  assert.deepEqual(parsed.tags, ["parser", "behavior", "typescript"]);
  assert.deepEqual(parsed.aliases, ["structured", "parse-skill"]);
  assert.equal(parsed.arguments.length, 3);
  const pathArgument = parsed.arguments.find((argument) => argument.name === "path")!;
  const modeArgument = parsed.arguments.find((argument) => argument.name === "mode")!;
  const secretArgument = parsed.arguments.find((argument) => argument.name === "secret")!;
  assert.equal(pathArgument.type, "path");
  assert.equal(pathArgument.required, true);
  assert.equal(pathArgument.positional, true);
  assert.equal(modeArgument.type, "enum");
  assert.equal(modeArgument.defaultValue, "safe");
  assert.deepEqual(modeArgument.enumValues, ["safe", "fast"]);
  assert.equal(secretArgument.sensitive, true);
  assert.deepEqual(parsed.toolScope.allowed, ["file_read", "file_list"]);
  assert.deepEqual(parsed.toolScope.denied, ["file_write"]);
  assert.deepEqual(parsed.toolScope.namespaces, ["builtin"]);
  assert.deepEqual(parsed.toolScope.mcpServers, []);
  assert.equal(parsed.toolScope.readOnly, true);
  assert.equal(parsed.toolScope.inheritParent, false);
  assert.equal(parsed.toolScope.maximumCalls, 3);
  assert.equal(parsed.toolScope.maximumParallel, 1);
  assert.equal(parsed.context.inheritConversation, false);
  assert.equal(parsed.context.inheritSystem, true);
  assert.equal(parsed.context.inheritMemory, false);
  assert.equal(parsed.context.includeWorkspaceInstructions, true);
  assert.equal(parsed.context.includeMcpInstructions, false);
  assert.deepEqual(parsed.context.includeFiles, ["AGENTS.md"]);
  assert.deepEqual(parsed.context.excludeFiles, ["secrets/**"]);
  assert.equal(parsed.context.maximumInputTokens, 2_048);
  assert.equal(parsed.context.maximumResourceTokens, 512);
  assert.equal(parsed.context.maximumOutputTokens, 256);
  assert.equal(parsed.context.compactionStrategy, "reject");
  assert.equal(parsed.execution.mode, "fork");
  assert.equal(parsed.execution.agent, "reader");
  assert.equal(parsed.execution.model, "model-a");
  assert.equal(parsed.execution.timeoutMs, 12_000);
  assert.equal(parsed.execution.maximumTurns, 4);
  assert.equal(parsed.execution.maximumCostMicros, 5_000);
  assert.equal(parsed.execution.workingDirectory, "docs");
  assert.equal(parsed.execution.sandbox, "workspace_read");
  assert.equal(parsed.execution.permissionMode, "sealed");
  assert.equal(parsed.execution.allowNetwork, false);
  assert.equal(parsed.execution.persistTranscript, true);
  assert.equal(parsed.execution.persistArtifacts, false);
  assert.equal(parsed.resources.length, 2);
  assert.equal(parsed.resources[0]?.resourceId, "guide");
  assert.equal(parsed.resources[0]?.path, "references/guide.md");
  assert.equal(parsed.resources[0]?.kind, "markdown");
  assert.equal(parsed.resources[0]?.required, true);
  assert.equal(parsed.resources[0]?.maximumBytes, 4_096);
  assert.equal(parsed.resources[0]?.charset, "utf-8");
  assert.equal(parsed.resources[0]?.mediaType, "text/markdown");
  assert.equal(parsed.resources[0]?.metadata.priority, "high");
  assert.equal(parsed.resources[1]?.kind, "json");
  assert.equal(parsed.resources[1]?.required, false);
  assert.equal(parsed.hooks.length, 2);
  assert.equal(parsed.hooks[0]?.hookId, "before");
  assert.equal(parsed.hooks[0]?.event, "before_invoke");
  assert.equal(parsed.hooks[0]?.module, "hooks/before.ts");
  assert.equal(parsed.hooks[0]?.priority, 20);
  assert.equal(parsed.hooks[1]?.event, "on_failure");
  assert.equal(parsed.hooks[1]?.command, "record-failure");
  assert.equal(parsed.hooks[1]?.failClosed, false);
  assert.equal(parsed.environmentHandles.SERVICE_TOKEN, "credential:service");
  assert.equal(parsed.metadata.owner, "typescript");
  assert.equal(parsed.metadata.revision, 3);
  assert.equal((parsed.metadata.unknown_frontmatter as JsonObject).experimental_flag, true);
  assert.ok(parsed.warnings.some((warning) => warning.includes("unknown frontmatter keys")));
  assert.match(parsed.body, /^# Structured behavior/);
  assert.equal(parsed.parsedAt, instant);
  assert.equal(parsed.descriptorDigest.length, 64);
});

test("skill registry resolves precedence, aliases, pins, revisions, filters, and exact snapshot restore", () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const project = descriptor({
    id: "shared-skill",
    name: "shared-skill",
    sourcePriority: 300,
    sourceKind: "project",
    tags: ["shared", "project"],
    aliases: ["shared-alias"],
    body: "Project implementation for {{target}}.",
  });
  const plugin = descriptor({
    id: "plugin:shared-skill",
    name: "shared-skill",
    sourcePriority: 200,
    sourceKind: "plugin",
    pluginId: "plugin-a",
    tags: ["shared", "plugin"],
    aliases: ["plugin-shared"],
    body: "Plugin implementation for {{target}}.",
  });
  const unique = descriptor({
    id: "unique-skill",
    name: "unique-skill",
    sourcePriority: 250,
    sourceKind: "user",
    tags: ["unique", "user"],
    aliases: ["unique-alias"],
  });
  const registry = new SkillRegistryRuntime({
    now,
    maximumRevisions: 2,
  });
  const first = registry.commitRevision({
    baseRevision: 0,
    descriptors: [plugin, unique, project],
    metadata: { reason: "initial" },
  });
  assert.equal(first.revision, 1);
  assert.equal(first.skills.length, 2);
  assert.deepEqual(first.added, ["shared-skill", "unique-skill"]);
  assert.deepEqual(first.removed, []);
  assert.deepEqual(first.updated, []);
  assert.ok(first.shadowed.includes("plugin:shared-skill"));
  assert.equal(registry.resolve("shared-skill").skillId, "shared-skill");
  assert.equal(registry.resolve("shared-alias").skillId, "shared-skill");
  assert.equal(registry.resolve("unique-alias").skillId, "unique-skill");
  assert.throws(() => registry.resolve("plugin-shared"), /not found/i);
  assert.equal(registry.list().length, 2);
  assert.equal(registry.list({ sourceId: project.source.sourceId }).length, 1);
  assert.equal(registry.list({ tag: "shared" }).length, 1);
  assert.equal(registry.list({ tag: "missing" }).length, 0);
  assert.deepEqual(registry.sourceSkills(project.source.sourceId), ["shared-skill"]);
  assert.deepEqual(registry.sourceSkills(unique.source.sourceId), ["unique-skill"]);
  const releasePin = registry.pin(1);
  const updatedProject = descriptor({
    id: "shared-skill",
    name: "shared-skill",
    sourcePriority: 300,
    sourceKind: "project",
    tags: ["shared", "project", "updated"],
    aliases: ["shared-alias"],
    body: "Updated project implementation for {{target}}.",
  });
  const second = registry.commitRevision({
    baseRevision: 1,
    descriptors: [updatedProject, unique],
    metadata: { reason: "update" },
  });
  assert.equal(second.revision, 2);
  assert.deepEqual(second.added, []);
  assert.deepEqual(second.removed, []);
  assert.deepEqual(second.updated, ["shared-skill"]);
  assert.equal(registry.resolve("shared-skill", 1).descriptor.descriptorDigest, project.descriptorDigest);
  assert.equal(registry.resolve("shared-skill", 2).descriptor.descriptorDigest, updatedProject.descriptorDigest);
  const third = registry.commitRevision({
    baseRevision: 2,
    descriptors: [updatedProject],
    metadata: { reason: "remove unique" },
  });
  assert.equal(third.revision, 3);
  assert.deepEqual(third.removed, ["unique-skill"]);
  assert.equal(registry.resolve("shared-skill").descriptor.descriptorDigest, updatedProject.descriptorDigest);
  assert.throws(() => registry.resolve("unique-skill"), /not found/i);
  assert.equal(registry.resolve("shared-skill", 1).descriptor.descriptorDigest, project.descriptorDigest);
  releasePin();
  const fourth = registry.commitRevision({
    baseRevision: 3,
    descriptors: [updatedProject, unique],
    metadata: { reason: "restore unique" },
  });
  assert.equal(fourth.revision, 4);
  assert.deepEqual(fourth.added, ["unique-skill"]);
  assert.throws(() => registry.resolve("shared-skill", 1), /revision/i);
  assert.throws(() => registry.commitRevision({
    baseRevision: 3,
    descriptors: [updatedProject],
  }), /revision/i);
  const snapshot = registry.snapshot();
  assert.equal(snapshot.version, "zyra.skill-registry/v2");
  assert.equal(snapshot.revision, 4);
  assert.equal(snapshot.activeSkills.length, 2);
  assert.equal(snapshot.digest.length, 64);
  const restored = new SkillRegistryRuntime({
    now,
    maximumRevisions: 2,
    snapshot,
  });
  assert.equal(restored.revision, 4);
  assert.equal(restored.resolve("shared-alias").descriptor.descriptorDigest, updatedProject.descriptorDigest);
  assert.equal(restored.resolve("unique-alias").descriptor.descriptorDigest, unique.descriptorDigest);
  assert.deepEqual(restored.list().map((item) => item.skillId).sort(), ["shared-skill", "unique-skill"]);
  assert.equal(restored.snapshot().headRevisionId, snapshot.headRevisionId);
});

test("skill context composition renders inherited sections and durable resource provenance", () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const contextDescriptor = descriptor({
    id: "context-skill",
    name: "context-skill",
    body: "Inspect {{target}} using the declared evidence resource.",
    resources: [{
      id: "evidence",
      path: "references/evidence.md",
      kind: "markdown",
      required: true,
      maximum_bytes: 4_096,
      media_type: "text/markdown",
      metadata: { priority: "high" },
    }],
    maximumInputTokens: 8_192,
    maximumResourceTokens: 2_048,
  });
  const evidenceText = "# Evidence\n\nTask 17 has canonical revision 9.";
  const resources = [{
    resourceId: "evidence",
    skillId: "context-skill",
    path: "references/evidence.md",
    kind: "markdown" as const,
    mediaType: "text/markdown",
    sizeBytes: Buffer.byteLength(evidenceText),
    digest: digest(evidenceText),
    text: evidenceText,
    bytesBase64: null,
    tokenEstimate: 16,
    loadedAt: instant,
    metadata: { priority: "high" },
  }];
  const runtime = new SkillContextRuntime({
    now,
    maximumCompositions: 4,
  });
  const composition = runtime.compose({
    descriptor: contextDescriptor,
    parent: parentContext({
      system: ["System A", "System B"],
      conversation: [
        { role: "user", content: "Inspect task-17" },
        { role: "assistant", content: "I will inspect it." },
      ],
      memory: [
        { kind: "fact", value: "task-17 owns artifact-4" },
        { kind: "fact", value: "revision 8 was superseded" },
      ],
      workspaceInstructions: ["Do not mutate files."],
      mcpInstructions: ["Use catalog server for state lookup."],
      variables: { run_id: "run-context", task_id: "task-17" },
      metadata: { source: "parent-runtime" },
    }),
    resources,
    arguments: { target: "task-17", count: 1 },
    metadata: { execution_id: "E02" },
  });
  assert.match(composition.compositionId, /^skill-context-composition-/);
  assert.equal(composition.skillId, "context-skill");
  assert.equal(composition.descriptorDigest, contextDescriptor.descriptorDigest);
  assert.equal(composition.policyDigest, digest(contextDescriptor.context));
  assert.equal(composition.parentDigest.length, 64);
  assert.equal(composition.digest.length, 64);
  assert.equal(composition.maximumInputTokens, 8_192);
  assert.equal(composition.maximumResourceTokens, 2_048);
  assert.equal(composition.truncated, false);
  assert.equal(composition.compactionRequested, false);
  assert.ok(composition.inputTokens > composition.resourceTokens);
  assert.equal(composition.resourceTokens, 12);
  assert.equal(composition.metadata.execution_id, "E02");
  assert.equal(composition.createdAt, instant);
  const skillSection = composition.sections.find((section) => section.kind === "skill_body")!;
  assert.equal(skillSection.sourceId, "context-skill");
  assert.equal(skillSection.required, true);
  assert.equal(skillSection.included, true);
  assert.equal(skillSection.exclusionReason, null);
  assert.match(String(skillSection.content), /task-17/);
  assert.equal(skillSection.contentDigest.length, 64);
  const systemSections = composition.sections.filter((section) => section.kind === "system");
  assert.equal(systemSections.length, 2);
  assert.deepEqual(systemSections.map((section) => section.content), ["System A", "System B"]);
  assert.ok(systemSections.every((section) => section.included));
  const conversationSections = composition.sections.filter((section) => section.kind === "conversation");
  assert.equal(conversationSections.length, 2);
  assert.equal((conversationSections[0]?.content as JsonObject).role, "user");
  assert.equal((conversationSections[1]?.content as JsonObject).role, "assistant");
  const memorySections = composition.sections.filter((section) => section.kind === "memory");
  assert.equal(memorySections.length, 2);
  assert.ok(memorySections.every((section) => section.included));
  const workspaceSection = composition.sections.find((section) => section.kind === "workspace_instruction")!;
  assert.equal(workspaceSection.content, "Do not mutate files.");
  const mcpSection = composition.sections.find((section) => section.kind === "mcp_instruction")!;
  assert.equal(mcpSection.content, "Use catalog server for state lookup.");
  const resourceSection = composition.sections.find((section) => section.kind === "resource")!;
  assert.equal(resourceSection.sourceId, "evidence");
  assert.equal(resourceSection.required, true);
  assert.equal(resourceSection.included, true);
  assert.equal(resourceSection.content, evidenceText);
  assert.equal(resourceSection.metadata.digest, digest(evidenceText));
  assert.equal(resourceSection.metadata.kind, "markdown");
  assert.equal(resourceSection.metadata.media_type, "text/markdown");
  const variablesSection = composition.sections.find((section) => section.kind === "variables")!;
  assert.equal(variablesSection.required, true);
  assert.equal((variablesSection.content as JsonObject).run_id, "run-context");
  assert.equal(((variablesSection.content as JsonObject).arguments as JsonObject).target, "task-17");
  assert.equal((composition.rendered.skill as JsonObject).id, "context-skill");
  assert.match(String((composition.rendered.skill as JsonObject).body), /task-17/);
  assert.equal((composition.rendered.system as unknown[]).length, 2);
  assert.equal((composition.rendered.conversation as unknown[]).length, 2);
  assert.equal((composition.rendered.memory as unknown[]).length, 2);
  assert.equal((composition.rendered.resources as unknown[]).length, 1);
  assert.equal(runtime.get(composition.compositionId)?.digest, composition.digest);
  assert.equal(runtime.list().length, 1);
  assert.equal(runtime.list("context-skill").length, 1);
  assert.equal(runtime.list("other-skill").length, 0);
  const exactReplay = runtime.compose({
    descriptor: contextDescriptor,
    parent: parentContext({
      system: ["System A", "System B"],
      conversation: [
        { role: "user", content: "Inspect task-17" },
        { role: "assistant", content: "I will inspect it." },
      ],
      memory: [
        { kind: "fact", value: "task-17 owns artifact-4" },
        { kind: "fact", value: "revision 8 was superseded" },
      ],
      workspaceInstructions: ["Do not mutate files."],
      mcpInstructions: ["Use catalog server for state lookup."],
      variables: { run_id: "run-context", task_id: "task-17" },
      metadata: { source: "parent-runtime" },
    }),
    resources,
    arguments: { target: "task-17", count: 1 },
    metadata: { execution_id: "E02" },
  });
  assert.equal(exactReplay.compositionId, composition.compositionId);
  assert.equal(exactReplay.digest, composition.digest);
  assert.equal(runtime.list().length, 1);
  const snapshot = runtime.snapshot();
  assert.equal(snapshot.version, "zyra.skill-context-runtime/v1");
  assert.equal(snapshot.revision, 1);
  assert.equal(snapshot.compositions.length, 1);
  assert.equal(snapshot.digest.length, 64);
  const restored = new SkillContextRuntime({
    now,
    snapshot,
  });
  assert.equal(restored.get(composition.compositionId)?.digest, composition.digest);
  assert.equal(restored.list("context-skill")[0]?.resourceTokens, 12);
});

test("skill context budgets truncate optional sections but reject missing required resources", () => {
  const base = descriptor({
    id: "budget-skill",
    body: "Inspect {{target}} and preserve required evidence.",
    resources: [{
      id: "required-evidence",
      path: "required.md",
      kind: "markdown",
      required: true,
      maximum_bytes: 4_096,
      metadata: { priority: "high" },
    }, {
      id: "optional-notes",
      path: "notes.md",
      kind: "markdown",
      required: false,
      maximum_bytes: 4_096,
      metadata: { priority: "low" },
    }],
    maximumInputTokens: 140,
    maximumResourceTokens: 70,
  });
  const requiredText = "R".repeat(120);
  const optionalText = "O".repeat(240);
  const requiredResource = {
    resourceId: "required-evidence",
    skillId: "budget-skill",
    path: "required.md",
    kind: "markdown" as const,
    mediaType: "text/markdown",
    sizeBytes: requiredText.length,
    digest: digest(requiredText),
    text: requiredText,
    bytesBase64: null,
    tokenEstimate: 30,
    loadedAt: instant,
    metadata: { priority: "high" },
  };
  const optionalResource = {
    resourceId: "optional-notes",
    skillId: "budget-skill",
    path: "notes.md",
    kind: "markdown" as const,
    mediaType: "text/markdown",
    sizeBytes: optionalText.length,
    digest: digest(optionalText),
    text: optionalText,
    bytesBase64: null,
    tokenEstimate: 60,
    loadedAt: instant,
    metadata: { priority: "low" },
  };
  const runtime = new SkillContextRuntime({
    now: () => new Date(instant),
  });
  const composition = runtime.compose({
    descriptor: base,
    parent: parentContext({
      system: ["S".repeat(120)],
      conversation: [{ role: "user", content: "C".repeat(200) }],
      memory: [{ value: "M".repeat(160) }],
      workspaceInstructions: ["W".repeat(80)],
      mcpInstructions: ["P".repeat(80)],
    }),
    resources: [requiredResource, optionalResource],
    arguments: { target: "task-17", count: 1 },
  });
  assert.equal(composition.truncated, true);
  assert.equal(composition.compactionRequested, false);
  assert.ok(composition.inputTokens <= composition.maximumInputTokens);
  assert.ok(composition.resourceTokens <= composition.maximumResourceTokens);
  const requiredSection = composition.sections.find((section) => section.sourceId === "required-evidence")!;
  const optionalSection = composition.sections.find((section) => section.sourceId === "optional-notes")!;
  assert.equal(requiredSection.included, true);
  assert.equal(requiredSection.exclusionReason, null);
  assert.equal(optionalSection.included, false);
  assert.equal(optionalSection.exclusionReason, "resource_budget_exceeded");
  assert.ok(composition.sections.some((section) => !section.included && section.exclusionReason === "input_budget_exceeded"));
  assert.equal((composition.rendered.resources as unknown[]).length, 1);
  assert.equal(((composition.rendered.resources as JsonObject[])[0]?.resource_id), "required-evidence");
  assert.throws(() => runtime.compose({
    descriptor: base,
    parent: parentContext(),
    resources: [optionalResource],
    arguments: { target: "task-17", count: 1 },
  }), /required skill resource|required-evidence.*missing/i);
  assert.throws(() => runtime.compose({
    descriptor: base,
    parent: parentContext(),
    resources: [requiredResource, requiredResource],
    arguments: { target: "task-17", count: 1 },
  }), /duplicate skill resource/i);
  assert.throws(() => runtime.compose({
    descriptor: base,
    parent: parentContext(),
    resources: [{
      ...requiredResource,
      resourceId: "undeclared",
    }],
    arguments: { target: "task-17", count: 1 },
  }), /not declared/i);
  const digestBound = descriptor({
    id: "digest-bound-skill",
    resources: [{
      id: "bound",
      path: "bound.md",
      kind: "markdown",
      required: true,
      digest: "expected-digest",
    }],
  });
  assert.throws(() => runtime.compose({
    descriptor: digestBound,
    parent: parentContext(),
    resources: [{
      ...requiredResource,
      resourceId: "bound",
      skillId: "digest-bound-skill",
      digest: "different-digest",
    }],
    arguments: { target: "task-17", count: 1 },
  }), /digest mismatch/i);
  const rejectPolicy = {
    ...base,
    skillId: "reject-budget",
    name: "reject-budget",
    descriptorDigest: digest({ base: base.descriptorDigest, policy: "reject" }),
    context: {
      ...base.context,
      maximumInputTokens: 16,
      maximumResourceTokens: 16,
      compactionStrategy: "reject" as const,
    },
    resources: [],
  };
  assert.throws(() => runtime.compose({
    descriptor: rejectPolicy,
    parent: parentContext({
      system: ["X".repeat(256)],
    }),
    resources: [],
    arguments: { target: "task-17", count: 1 },
  }), /budget/i);
});

test("skill tool scope intersects parent policy and blocks write, namespace, server, and call overflow", () => {
  const registry = new SkillRegistryRuntime({
    now: () => new Date(instant),
  });
  const scopeDescriptor = descriptor({
    id: "scope-skill",
    allowed: ["file_*", "mcp__catalog__*", "browser_read"],
    denied: ["file_delete", "mcp__catalog__replace"],
    maximumCalls: 5,
  });
  registry.commitRevision({
    baseRevision: 0,
    descriptors: [scopeDescriptor],
  });
  const runtime = new SkillInvocationRuntime({
    registry,
    resources: new SkillResourceRuntime({
      workspaceRoot: "G:/workspace",
      allowOutsideWorkspace: false,
    }),
    executor: async () => ({ output: {}, inputTokens: 1, outputTokens: 1 }),
  });
  const parent = broadScope({
    allowed: ["file_read", "file_list", "mcp__*"],
    denied: ["shell", "file_write"],
    namespaces: ["builtin", "mcp"],
    mcpServers: ["catalog", "memory"],
    readOnly: true,
    maximumCalls: 3,
    maximumParallel: 1,
    requireApproval: ["mcp__catalog__inspect"],
  });
  const effective = runtime.applyToolScope(parent, scopeDescriptor.toolScope);
  assert.deepEqual(effective.allowed, ["file_list", "file_read", "mcp__catalog__*"]);
  assert.ok(effective.denied.includes("shell"));
  assert.ok(effective.denied.includes("file_write"));
  assert.ok(effective.denied.includes("file_delete"));
  assert.ok(effective.denied.includes("mcp__catalog__replace"));
  assert.deepEqual(effective.namespaces, ["builtin", "mcp"]);
  assert.deepEqual(effective.mcpServers, ["catalog"]);
  assert.equal(effective.readOnly, true);
  assert.equal(effective.inheritParent, false);
  assert.equal(effective.maximumCalls, 3);
  assert.equal(effective.maximumParallel, 1);
  assert.deepEqual(effective.requireApproval.sort(), ["mcp__catalog__inspect", "mcp__catalog__replace"]);
  assert.doesNotThrow(() => runtime.assertToolAllowed(
    effective,
    0,
    "file_read",
    "builtin",
    "",
    true,
  ));
  assert.doesNotThrow(() => runtime.assertToolAllowed(
    effective,
    1,
    "file_list",
    "builtin",
    "",
    true,
  ));
  assert.doesNotThrow(() => runtime.assertToolAllowed(
    effective,
    2,
    "mcp__catalog__inspect",
    "mcp",
    "catalog",
    true,
  ));
  assert.throws(() => runtime.assertToolAllowed(
    effective,
    0,
    "file_write",
    "builtin",
    "",
    false,
  ), /read-only|denies/i);
  assert.throws(() => runtime.assertToolAllowed(
    effective,
    0,
    "file_delete",
    "builtin",
    "",
    true,
  ), /denies/i);
  assert.throws(() => runtime.assertToolAllowed(
    effective,
    0,
    "browser_read",
    "browser",
    "",
    true,
  ), /not allow|namespace/i);
  assert.throws(() => runtime.assertToolAllowed(
    effective,
    0,
    "mcp__catalog__inspect",
    "mcp",
    "memory",
    true,
  ), /server.*not allowed/i);
  assert.throws(() => runtime.assertToolAllowed(
    effective,
    3,
    "file_read",
    "builtin",
    "",
    true,
  ), /budget.*exhausted/i);
  const detached = runtime.applyToolScope(parent, broadScope({
    allowed: ["browser_read"],
    denied: [],
    namespaces: ["browser"],
    mcpServers: [],
    readOnly: false,
    inheritParent: false,
    maximumCalls: 10,
    maximumParallel: 2,
  }));
  assert.deepEqual(detached.allowed, ["browser_read"]);
  assert.deepEqual(detached.denied, []);
  assert.deepEqual(detached.namespaces, ["browser"]);
  assert.deepEqual(detached.mcpServers, []);
  assert.equal(detached.readOnly, true);
  assert.equal(detached.maximumCalls, 3);
  assert.equal(detached.maximumParallel, 1);
});

test("skill invocation execution deduplicates exact requests and records background, failure, and budget states", async () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const inline = descriptor({
    id: "invoke-inline",
    mode: "inline",
    allowed: ["file_read"],
    denied: [],
    maximumCalls: 2,
  });
  const background = descriptor({
    id: "invoke-background",
    mode: "background",
    allowed: ["file_read"],
    denied: [],
    maximumCalls: 1,
  });
  const registry = new SkillRegistryRuntime({ now });
  registry.commitRevision({
    baseRevision: 0,
    descriptors: [inline, background],
  });
  let executions = 0;
  const runtime = new SkillInvocationRuntime({
    registry,
    resources: new SkillResourceRuntime({
      workspaceRoot: "G:/workspace",
      allowOutsideWorkspace: false,
      now,
    }),
    now,
    maximumConcurrentInvocations: 2,
    maximumRecordedResults: 8,
    executor: async (context) => {
      executions += 1;
      context.assertToolAllowed("file_read", "builtin", "", true);
      context.recordToolCall("file_read", { path: "README.md" });
      return {
        output: {
          invocation_id: context.plan.invocationId,
          rendered_body: context.plan.renderedBody,
          context_version: context.plan.context.version,
        },
        artifacts: [{ kind: "trace", trace_id: `trace-${executions}` }],
        inputTokens: 80,
        outputTokens: 20,
        costMicros: 250,
        metadata: { execution: executions },
      };
    },
  });
  const inlineRequest = invocationRequest("invoke-inline", registry.revision, {
    arguments: { target: "task-inline", count: 2 },
    parentContext: {
      system: ["Inline system"],
      conversation: [],
      memory: [],
      workspace_instructions: [],
      mcp_instructions: [],
    },
  });
  const first = await runtime.invoke(inlineRequest);
  assert.equal(first.status, "completed");
  assert.equal(first.skillId, "invoke-inline");
  assert.equal(first.toolCalls, 1);
  assert.equal(first.inputTokens, 80);
  assert.equal(first.outputTokens, 20);
  assert.equal(first.costMicros, 250);
  assert.equal(first.artifacts.length, 1);
  assert.equal(first.artifacts[0]?.trace_id, "trace-1");
  assert.equal((first.output as JsonObject).context_version, "zyra.skill-context/v2");
  assert.match(String((first.output as JsonObject).rendered_body), /task-inline/);
  assert.equal(first.metadata.execution, 1);
  assert.ok(first.completedAt);
  assert.equal(first.failure, null);
  const replay = await runtime.invoke(inlineRequest);
  assert.deepEqual(replay, first);
  assert.equal(executions, 1);
  assert.equal(runtime.getResult(first.invocationId)?.metadata.execution, 1);
  const backgroundResult = await runtime.invoke(invocationRequest("invoke-background", registry.revision, {
    arguments: { target: "task-background", count: 1 },
  }));
  assert.equal(backgroundResult.status, "background");
  assert.equal(backgroundResult.skillId, "invoke-background");
  assert.equal(backgroundResult.toolCalls, 1);
  assert.equal(backgroundResult.completedAt, null);
  assert.equal(backgroundResult.metadata.execution, 2);
  assert.equal(executions, 2);
  const forged = invocationRequest("invoke-inline", registry.revision, {
    identity: {
      ...inlineRequest.identity,
      invocationId: "forged-invocation-id",
    },
  });
  await assert.rejects(runtime.invoke(forged), /does not match canonical/i);
  const outputBudgetDescriptor = {
    ...inline,
    skillId: "output-budget",
    name: "output-budget",
    descriptorDigest: digest({ base: inline.descriptorDigest, kind: "output-budget" }),
    context: {
      ...inline.context,
      maximumOutputTokens: 4,
    },
  };
  registry.commitRevision({
    baseRevision: 1,
    descriptors: [inline, background, outputBudgetDescriptor],
  });
  await assert.rejects(runtime.invoke(invocationRequest("output-budget", registry.revision)), /output exceeded|output budget/i);
  const failedPlan = runtime.getResult(runtime.inFlightPlans()[0]?.invocationId ?? "missing");
  assert.equal(failedPlan, null);
  assert.equal(runtime.inFlightPlans().length, 0);
});

test("skill invocation journal fences effect, result, acknowledgement, restore, and reconciliation", () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const plan = {
    invocationId: "invocation-journal-17",
    skillId: "journal-skill",
    skillName: "journal-skill",
    registryRevision: 4,
    descriptorDigest: "descriptor-journal-17",
    identity: {
      runId: "run-journal-17",
      taskId: "task-journal-17",
      sessionId: "session-journal-17",
      sessionRevision: 2,
      workerRequestId: "worker-journal-17",
      toolCallId: "call-journal-17",
      invocationId: "invocation-journal-17",
    },
    arguments: { target: "task-17", count: 1 },
    renderedBody: "Inspect task-17 once.",
    resources: [],
    context: { version: "zyra.skill-context/v2" },
    effectiveToolScope: broadScope({
      allowed: ["file_read"],
      maximumCalls: 1,
    }),
    execution: descriptor({ id: "journal-skill" }).execution,
    inputTokenEstimate: 40,
    resourceTokenEstimate: 0,
    createdAt: instant,
    metadata: { execution_id: "E02" },
  };
  const journal = new SkillInvocationJournal({
    epoch: 7,
    now,
    maximumRecords: 16,
  });
  const prepared = journal.prepare(plan);
  assert.match(prepared.journalId, /^skill-invocation-journal-/);
  assert.match(prepared.transitionId, /^skill-invocation-transition-/);
  assert.equal(prepared.invocationId, "invocation-journal-17");
  assert.equal(prepared.skillId, "journal-skill");
  assert.equal(prepared.registryRevision, 4);
  assert.equal(prepared.descriptorDigest, "descriptor-journal-17");
  assert.equal(prepared.planDigest, digest(plan));
  assert.equal(prepared.status, "prepared");
  assert.equal(prepared.sequence, 1);
  assert.equal(prepared.epoch, 7);
  assert.equal(prepared.effectId, null);
  assert.equal(prepared.result, null);
  assert.equal(prepared.failure, null);
  assert.equal(prepared.startedAt, null);
  assert.equal(prepared.effectAt, null);
  assert.equal(prepared.committedAt, null);
  assert.equal(prepared.acknowledgedAt, null);
  assert.equal(prepared.previousHash, "sha256:zyra-skill-invocation-genesis");
  assert.equal(prepared.recordHash.length, 64);
  assert.equal(journal.prepare(plan).journalId, prepared.journalId);
  assert.equal(journal.pending().length, 1);
  const executing = journal.begin(prepared.journalId, prepared.transitionId);
  assert.equal(executing.status, "executing");
  assert.ok(executing.startedAt);
  assert.equal(journal.begin(prepared.journalId, prepared.transitionId).startedAt, executing.startedAt);
  const effect = journal.recordEffect(
    prepared.journalId,
    prepared.transitionId,
    "artifact_created",
    { artifact_id: "artifact-journal-17", digest: "sha256:artifact" },
    { reversible: false },
  );
  assert.equal(effect.status, "effect_recorded");
  assert.match(effect.effectId ?? "", /^skill-invocation-effect-/);
  assert.equal(effect.effectDigest?.length, 64);
  assert.equal(effect.effect?.kind, "artifact_created");
  assert.equal((effect.effect?.value as JsonObject).artifact_id, "artifact-journal-17");
  assert.equal((effect.effect!.metadata as JsonObject).reversible, false);
  assert.ok(effect.effectAt);
  const repeatedEffect = journal.recordEffect(
    prepared.journalId,
    prepared.transitionId,
    "artifact_created",
    { artifact_id: "artifact-journal-17", digest: "sha256:artifact" },
    { reversible: false },
  );
  assert.equal(repeatedEffect.effectId, effect.effectId);
  assert.throws(() => journal.recordEffect(
    prepared.journalId,
    prepared.transitionId,
    "artifact_created",
    { artifact_id: "different" },
  ), /conflicting effect/i);
  const result = {
    invocationId: "invocation-journal-17",
    skillId: "journal-skill",
    status: "completed" as const,
    output: { ok: true, artifact_id: "artifact-journal-17" },
    artifacts: [{ artifact_id: "artifact-journal-17" }],
    toolCalls: 1,
    inputTokens: 40,
    outputTokens: 8,
    costMicros: 100,
    startedAt: instant,
    completedAt: "2026-07-17T08:01:00.000Z",
    failure: null,
    metadata: { canonical_owner: "typescript" },
  };
  const committed = journal.commit(prepared.journalId, prepared.transitionId, result);
  assert.equal(committed.status, "committed");
  assert.deepEqual(committed.result, result);
  assert.equal(committed.resultDigest, digest(result));
  assert.ok(committed.committedAt);
  assert.equal(journal.commit(prepared.journalId, prepared.transitionId, result).resultDigest, committed.resultDigest);
  assert.throws(() => journal.fail(prepared.journalId, prepared.transitionId, new Error("late failure")), /cannot fail committed/i);
  assert.throws(() => journal.acknowledge(prepared.journalId, prepared.transitionId, "wrong-digest"), /acknowledgement mismatch/i);
  const acknowledged = journal.acknowledge(prepared.journalId, prepared.transitionId, committed.resultDigest!);
  assert.equal(acknowledged.status, "acknowledged");
  assert.ok(acknowledged.acknowledgedAt);
  assert.equal(journal.pending().length, 0);
  assert.equal(journal.get(prepared.journalId)?.status, "acknowledged");
  assert.equal(journal.findInvocation("invocation-journal-17")?.journalId, prepared.journalId);
  const snapshot = journal.snapshot();
  assert.equal(snapshot.version, "zyra.skill-invocation-journal/v1");
  assert.equal(snapshot.epoch, 7);
  assert.equal(snapshot.sequence, 1);
  assert.equal(snapshot.records.length, 1);
  assert.equal(snapshot.headHash, snapshot.records[0]?.recordHash);
  assert.equal(snapshot.digest.length, 64);
  const restored = new SkillInvocationJournal({
    epoch: 8,
    now,
    snapshot,
  });
  assert.equal(restored.get(prepared.journalId)?.status, "acknowledged");
  assert.equal(restored.findInvocation("invocation-journal-17")?.resultDigest, committed.resultDigest);
  assert.equal(restored.pending().length, 0);
  const secondPlan = {
    ...plan,
    invocationId: "invocation-pending-18",
    identity: {
      ...plan.identity,
      invocationId: "invocation-pending-18",
      toolCallId: "call-pending-18",
    },
  };
  const second = restored.prepare(secondPlan);
  restored.begin(second.journalId, second.transitionId);
  const pendingSnapshot = restored.snapshot();
  const restarted = new SkillInvocationJournal({
    epoch: 9,
    now,
    snapshot: pendingSnapshot,
  });
  const indeterminate = restarted.findInvocation("invocation-pending-18")!;
  assert.equal(indeterminate.status, "indeterminate");
  assert.equal(indeterminate.metadata.restored_from_epoch, 8);
  assert.equal(indeterminate.metadata.reconciliation_required, false);
  const safeRetry = restarted.reconcile(indeterminate.journalId, null, null);
  assert.equal(safeRetry.status, "prepared");
  assert.equal(safeRetry.metadata.safe_to_retry, true);
});

test("skill search supports exact, alias, tag, source, plugin, availability, update, and removal filters", () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const project = descriptor({
    id: "deploy-project",
    name: "deploy-project",
    description: "Deploy services to staging with verification",
    tags: ["deploy", "staging"],
    aliases: ["stage-deploy"],
    sourceKind: "project",
  });
  const plugin = descriptor({
    id: "plugin-a:deploy-cloud",
    name: "deploy-cloud",
    description: "Deploy cloud workloads with rollback",
    tags: ["deploy", "cloud"],
    aliases: ["cloud-release"],
    sourceKind: "plugin",
    pluginId: "plugin-a",
  });
  const disabled = descriptor({
    id: "disabled-search",
    name: "disabled-search",
    description: "Disabled diagnostic search",
    tags: ["diagnostic"],
    aliases: ["disabled-diagnostic"],
    enabled: false,
  });
  const registry = new SkillRegistryRuntime({ now });
  registry.commitRevision({
    baseRevision: 0,
    descriptors: [project, plugin, disabled],
  });
  const search = new SkillSearchRuntime({ now });
  const rebuilt = search.rebuild(registry.snapshot());
  assert.equal(rebuilt.indexRevision, 1);
  assert.equal(rebuilt.registryRevision, 1);
  assert.equal(rebuilt.documents.length, 3);
  assert.ok(rebuilt.averageLength > 0);
  assert.ok(Number(rebuilt.documentFrequency.deploy) >= 2);
  const exact = search.search({
    text: "deploy-project",
    maximumResults: 10,
    minimumScore: 0,
  });
  assert.equal(exact.hits[0]?.skillId, "deploy-project");
  assert.equal(exact.hits[0]?.exactName, true);
  assert.equal(exact.hits[0]?.aliasMatch, false);
  assert.ok(exact.hits[0]!.score >= 20);
  assert.equal(exact.searchedDocuments, 3);
  assert.equal(exact.filteredDocuments, 2);
  assert.ok(exact.queryTerms.includes("deploy-project"));
  assert.ok(exact.queryTerms.includes("deploy"));
  assert.ok(exact.queryTerms.includes("project"));
  const alias = search.search({
    text: "cloud-release",
    pluginIds: ["plugin-a"],
    maximumResults: 5,
    minimumScore: 0,
  });
  assert.equal(alias.filteredDocuments, 1);
  assert.equal(alias.hits[0]?.skillId, "plugin-a:deploy-cloud");
  assert.equal(alias.hits[0]?.aliasMatch, true);
  assert.equal(alias.hits[0]?.metadata.plugin_id, "plugin-a");
  const tagFiltered = search.search({
    text: "deploy",
    tags: ["staging"],
    sourceKinds: ["project"],
    maximumResults: 10,
    minimumScore: 0,
  });
  assert.equal(tagFiltered.filteredDocuments, 1);
  assert.equal(tagFiltered.hits[0]?.skillId, "deploy-project");
  const disabledExcluded = search.search({
    text: "diagnostic",
    includeUnavailable: false,
    minimumScore: 0,
  });
  assert.equal(disabledExcluded.filteredDocuments, 2);
  assert.ok(disabledExcluded.hits.every((hit) => hit.skillId !== "disabled-search"));
  const disabledIncluded = search.search({
    text: "diagnostic",
    includeUnavailable: true,
    minimumScore: 0,
  });
  assert.equal(disabledIncluded.hits[0]?.skillId, "disabled-search");
  assert.deepEqual(search.suggest("stage", 3).map((hit) => hit.skillId), ["deploy-project"]);
  assert.deepEqual(search.suggest("cloud", 3).map((hit) => hit.skillId), ["plugin-a:deploy-cloud"]);
  assert.deepEqual(search.suggest("", 3), []);
  const updatedProject = descriptor({
    id: "deploy-project",
    name: "deploy-project",
    description: "Deploy services to production with canary verification",
    tags: ["deploy", "production", "canary"],
    aliases: ["production-deploy"],
    sourceKind: "project",
  });
  const document = search.update(updatedProject, 2);
  assert.equal(document.skillId, "deploy-project");
  assert.equal(document.registryRevision, 2);
  assert.ok(document.searchableText.includes("production"));
  assert.ok(document.terms.canary >= 1);
  assert.equal(search.search({ text: "canary", minimumScore: 0 }).hits[0]?.skillId, "deploy-project");
  assert.throws(() => search.update(project, 1), /stale/i);
  assert.equal(search.remove("plugin-a:deploy-cloud", 3), true);
  assert.equal(search.remove("plugin-a:deploy-cloud", 3), false);
  assert.ok(search.search({ text: "cloud", includeUnavailable: true, minimumScore: 0 }).hits.every(
    (hit) => hit.skillId !== "plugin-a:deploy-cloud",
  ));
  assert.throws(() => search.remove("deploy-project", 2), /stale/i);
  const snapshot = search.snapshot();
  assert.equal(snapshot.indexRevision, 3);
  assert.equal(snapshot.registryRevision, 3);
  assert.equal(snapshot.documents.length, 2);
  assert.equal(snapshot.digest.length, 64);
  const restored = new SkillSearchRuntime({ now, snapshot });
  assert.equal(restored.search({ text: "production", minimumScore: 0 }).hits[0]?.skillId, "deploy-project");
  assert.ok(restored.search({ text: "cloud", minimumScore: 0 }).hits.every(
    (hit) => hit.skillId !== "plugin-a:deploy-cloud",
  ));
  assert.throws(() => restored.rebuild({
    ...registry.snapshot(),
    revision: 1,
  }), /cannot regress|digest/i);
});

test("skill root runtime commits project and plugin roots with CAS, reachability, failure, and restore custody", async () => {
  const temporaryRoot = join(process.cwd(), ".tmp");
  await mkdir(temporaryRoot, { recursive: true });
  const workspace = await mkdtemp(join(temporaryRoot, "zyra-e02-skill-roots-"));
  const projectDirectory = join(workspace, "skills-project");
  const pluginDirectory = join(workspace, "skills-plugin");
  await mkdir(projectDirectory, { recursive: true });
  await mkdir(pluginDirectory, { recursive: true });
  try {
    const projectRoot: SkillSourceRoot = {
      sourceId: "project-skills",
      kind: "project",
      rootPath: projectDirectory,
      priority: 300,
      enabled: true,
      recursive: true,
      followSymlinks: false,
      maximumDepth: 8,
      includePatterns: ["**/SKILL.md"],
      excludePatterns: ["**/.git/**"],
      pluginId: null,
      revision: 1,
      metadata: { owner: "typescript" },
    };
    const runtime = new SkillRootRuntime({
      workspaceRoot: workspace,
      roots: [projectRoot],
      now: () => new Date(instant),
      maximumRevisions: 8,
      maximumFailures: 8,
    });
    assert.equal(runtime.revision, 0);
    assert.equal(runtime.list().length, 1);
    assert.equal(runtime.list({ enabledOnly: true }).length, 1);
    assert.equal(runtime.list({ existingOnly: true }).length, 1);
    assert.equal(runtime.list({ pluginId: null }).length, 1);
    assert.equal(runtime.require("project-skills").rootPath, projectDirectory);
    assert.deepEqual(runtime.validateReachable({
      enabledOnly: true,
      requireDirectories: true,
    }), []);
    const replaced = runtime.replaceAll([projectRoot], 0, {
      reason: "initial-canonical-commit",
    });
    assert.equal(replaced.revisionBefore, 0);
    assert.equal(replaced.revisionAfter, 1);
    assert.deepEqual(replaced.added, []);
    assert.deepEqual(replaced.updated, []);
    assert.deepEqual(replaced.removed, []);
    assert.deepEqual(replaced.unchanged, ["project-skills"]);
    assert.equal(replaced.rejected.length, 0);
    assert.equal(replaced.metadata.reason, "initial-canonical-commit");
    assert.equal(replaced.digest.length, 64);
    const pluginRoot: SkillSourceRoot = {
      sourceId: "plugin-skills",
      kind: "plugin",
      rootPath: pluginDirectory,
      priority: 400,
      enabled: true,
      recursive: true,
      followSymlinks: false,
      maximumDepth: 6,
      includePatterns: ["**/SKILL.md"],
      excludePatterns: [],
      pluginId: "plugin-roots",
      revision: 2,
      metadata: {},
    };
    const pluginRevision = runtime.replacePlugin({
      pluginId: "plugin-roots",
      pluginRevision: 2,
      manifestDigest: "a".repeat(64),
      roots: [pluginRoot],
      enabled: true,
      metadata: { reason: "plugin-load" },
    }, 1);
    assert.equal(pluginRevision.revisionBefore, 1);
    assert.equal(pluginRevision.revisionAfter, 2);
    assert.deepEqual(pluginRevision.added, ["plugin-skills"]);
    assert.equal(runtime.list({ pluginId: "plugin-roots" }).length, 1);
    const committedPlugin = runtime.require("plugin-skills");
    assert.equal(committedPlugin.kind, "plugin");
    assert.equal(committedPlugin.pluginId, "plugin-roots");
    assert.equal(committedPlugin.revision, 2);
    assert.equal(committedPlugin.metadata.plugin_revision, 2);
    assert.equal(committedPlugin.metadata.plugin_manifest_digest, "a".repeat(64));
    assert.throws(() => runtime.replacePlugin({
      pluginId: "plugin-roots",
      pluginRevision: 3,
      manifestDigest: "invalid",
      roots: [pluginRoot],
      enabled: true,
    }, 2), /manifest digest/i);
    assert.throws(() => runtime.replaceAll([projectRoot], 1), /revision/i);
    assert.throws(() => runtime.require("missing-root"), /not found/i);
    const failure = runtime.recordFailure({
      sourceId: "project-skills",
      pluginId: null,
      code: "synthetic_reachability_failure",
      message: "root became unavailable during validation",
      fatal: true,
      metadata: { test: "root-custody" },
    });
    assert.equal(failure.sourceId, "project-skills");
    assert.equal(failure.pluginId, null);
    assert.equal(failure.code, "synthetic_reachability_failure");
    assert.equal(failure.fatal, true);
    assert.equal(failure.metadata.test, "root-custody");
    assert.match(failure.failureId, /^skill-root-failure-/);
    assert.equal(runtime.listFailures().length, 1);
    const removed = runtime.removePlugin("plugin-roots", 2, {
      reason: "plugin-unload",
    });
    assert.equal(removed.revisionAfter, 3);
    assert.deepEqual(removed.removed, ["plugin-skills"]);
    assert.equal(runtime.list({ pluginId: "plugin-roots" }).length, 0);
    assert.equal(runtime.list().length, 1);
    const snapshot = runtime.snapshot();
    assert.equal(snapshot.version, "zyra.skill-root-runtime/v1");
    assert.equal(snapshot.revision, 3);
    assert.equal(snapshot.roots.length, 1);
    assert.equal(snapshot.failures.length, 1);
    assert.equal(snapshot.revisions.length, 3);
    assert.equal(snapshot.digest.length, 64);
    const restored = new SkillRootRuntime({
      workspaceRoot: workspace,
      now: () => new Date(instant),
      snapshot,
    });
    assert.equal(restored.revision, 3);
    assert.equal(restored.require("project-skills").rootPath, projectDirectory);
    assert.equal(restored.listFailures()[0]?.failureId, failure.failureId);
    assert.deepEqual(restored.validateReachable(), []);
    assert.throws(() => new SkillRootRuntime({
      workspaceRoot: join(workspace, "other"),
      snapshot,
    }), /different workspace/i);
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }
});

test("skill watcher deduplicates change batches, bounds overflow, emits listeners, and restores pending state", async () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const root: SkillSourceRoot = {
    sourceId: "watch-root",
    kind: "project",
    rootPath: "G:/workspace/skills",
    priority: 300,
    enabled: true,
    recursive: true,
    followSymlinks: false,
    maximumDepth: 8,
    includePatterns: ["**/SKILL.md"],
    excludePatterns: ["**/.git/**"],
    pluginId: null,
    revision: 1,
    metadata: { execution_id: "E02" },
  };
  const runtime = new SkillWatcherRuntime({
    now,
    debounceMs: 60_000,
    maximumPending: 3,
    maximumHistory: 2,
  });
  const batches: string[] = [];
  const unsubscribe = runtime.onBatch((batch) => {
    batches.push(batch.batchId);
  });
  const first = runtime.observe(root, "change", "alpha/SKILL.md", {
    source: "editor",
  });
  assert.equal(first.sourceId, "watch-root");
  assert.equal(first.rootPath.replace(/\\/g, "/"), "G:/workspace/skills");
  assert.match(first.path.replace(/\\/g, "/"), /G:\/workspace\/skills\/alpha\/SKILL\.md$/);
  assert.equal(first.relativePath, "alpha/SKILL.md");
  assert.equal(first.kind, "change");
  assert.equal(first.generation, 0);
  assert.equal(first.sequence, 1);
  assert.equal(first.metadata.source, "editor");
  assert.match(first.eventId, /^skill-watch-event-/);
  const duplicate = runtime.observe(root, "change", "alpha/SKILL.md", {
    source: "filesystem",
  });
  assert.equal(duplicate.sequence, 2);
  assert.notEqual(duplicate.eventId, first.eventId);
  const renamed = runtime.observe(root, "rename", "beta/SKILL.md", {
    source: "filesystem",
  });
  assert.equal(renamed.kind, "rename");
  assert.equal(renamed.sequence, 3);
  const firstBatch = await runtime.flush();
  assert.ok(firstBatch);
  assert.equal(firstBatch?.generation, 0);
  assert.equal(firstBatch?.events.length, 2);
  assert.equal(firstBatch?.events[0]?.relativePath, "alpha/SKILL.md");
  assert.equal(firstBatch?.events[0]?.metadata.source, "filesystem");
  assert.equal(firstBatch?.events[1]?.relativePath, "beta/SKILL.md");
  assert.deepEqual(firstBatch?.sourceIds, ["watch-root"]);
  assert.equal(firstBatch?.paths.length, 2);
  assert.equal(firstBatch?.digest.length, 64);
  assert.equal(batches.length, 1);
  assert.equal(batches[0], firstBatch?.batchId);
  assert.equal(runtime.batches().length, 1);
  assert.equal(await runtime.flush(), null);
  runtime.observe(root, "custom", "gamma/SKILL.md");
  runtime.observe(root, "change", "delta/SKILL.md");
  runtime.observe(root, "change", "epsilon/SKILL.md");
  runtime.observe(root, "change", "zeta/SKILL.md");
  const overflowSnapshot = runtime.snapshot();
  assert.equal(overflowSnapshot.pending.length, 4);
  assert.equal(overflowSnapshot.pending[0]?.kind, "overflow");
  assert.equal(overflowSnapshot.pending[0]?.metadata.dropped, 1);
  assert.equal(overflowSnapshot.active, false);
  assert.equal(overflowSnapshot.history.length, 1);
  assert.equal(overflowSnapshot.digest.length, 64);
  const restored = new SkillWatcherRuntime({
    now,
    debounceMs: 60_000,
    maximumPending: 4,
    maximumHistory: 2,
    snapshot: overflowSnapshot,
  });
  const restoredSnapshot = restored.snapshot();
  assert.equal(restoredSnapshot.generation, overflowSnapshot.generation);
  assert.equal(restoredSnapshot.sequence, overflowSnapshot.sequence);
  assert.equal(restoredSnapshot.pending.length, 4);
  assert.equal(restoredSnapshot.history.length, 1);
  assert.equal(restoredSnapshot.active, false);
  const restoredBatch = await restored.flush();
  assert.ok(restoredBatch);
  assert.ok(restoredBatch!.events.some((event) => event.kind === "overflow"));
  assert.equal(restored.batches().length, 2);
  runtime.observe(root, "error", "", { message: "watch failed" });
  const errorBatch = await runtime.flush();
  const errorEvent = errorBatch?.events.find((event) => event.kind === "error");
  assert.equal(errorEvent?.kind, "error");
  assert.equal(errorEvent?.metadata.message, "watch failed");
  assert.equal(runtime.batches().length, 2);
  unsubscribe();
  runtime.observe(root, "change", "eta/SKILL.md");
  await runtime.flush();
  assert.equal(batches.length, 2);
  assert.throws(() => runtime.observe(root, "change", "../escape/SKILL.md"), /escapes/i);
  await runtime.stop();
  await restored.stop();
});

test("plugin manifest parsing internalizes dependencies, capabilities, paths, permissions, and boundary failures", async () => {
  const temporaryRoot = join(process.cwd(), ".tmp");
  await mkdir(temporaryRoot, { recursive: true });
  const workspace = await mkdtemp(join(temporaryRoot, "zyra-e02-plugin-manifest-"));
  const pluginRoot = join(workspace, "manifest-plugin");
  await mkdir(join(pluginRoot, "skills"), { recursive: true });
  await mkdir(join(pluginRoot, "commands"), { recursive: true });
  await mkdir(join(pluginRoot, "hooks"), { recursive: true });
  await mkdir(join(pluginRoot, "agents"), { recursive: true });
  const manifestPath = join(pluginRoot, "plugin.json");
  const rawManifest = {
    manifest_version: 1,
    id: "manifest-plugin",
    name: "Manifest Plugin",
    version: "2.3.4-beta.1",
    description: "Original description",
    author: "Zyra Runtime",
    homepage: "https://plugins.example.test/manifest",
    license: "Apache-2.0",
    enabled: true,
    minimum_zyra_version: "0.1.0",
    dependencies: {
      "base-plugin": "^1.2.0",
      "optional-plugin": {
        version: "~3.4.0",
        optional: true,
        capabilities: ["skill", "mcp", "unknown"],
      },
    },
    skills: [{
      path: "skills",
      priority: 450,
      recursive: true,
      follow_symlinks: false,
      maximum_depth: 6,
      include: ["**/SKILL.md"],
      exclude: ["**/draft/**"],
      metadata: { channel: "project" },
    }],
    commands: [{
      name: "manifest-inspect",
      path: "commands/inspect.md",
      aliases: ["mi"],
      hidden: false,
      permission: "read",
      metadata: { category: "inspection" },
    }],
    hooks: [{
      id: "manifest-before-read",
      event: "before_tool",
      path: "hooks/before-read.ts",
      priority: 90,
      timeout_ms: 1200,
      fail_closed: true,
      can_mutate: true,
      tools: ["file_read", "mcp__catalog__*"],
    }],
    agents: [{
      id: "manifest-reader",
      path: "agents/reader.md",
      description: "Read-only reader",
      model: "model-a",
      tools: { allowed: ["file_read"] },
    }],
    mcp: {
      catalog: {
        config_path: "mcp/catalog.json",
        config: { transport: "stdio", command: "catalog" },
        enabled: true,
        permissions: { read: true, write: false },
      },
    },
    environment: {
      PLUGIN_TOKEN: "credential:manifest-token",
      REGION: "config:region",
    },
    permissions: {
      filesystem: ["read"],
      network: false,
    },
    metadata: {
      execution_id: "E02",
      canonical_owner: "typescript",
    },
  };
  try {
    await writeFile(manifestPath, JSON.stringify(rawManifest, null, 2), "utf8");
    const parser = new PluginManifestRuntime({
      workspaceRoot: workspace,
      allowOutsideWorkspace: false,
      maximumManifestBytes: 128 * 1024,
    });
    const parsed = await parser.parse(manifestPath, "project", {
      description: "Override description",
    });
    assert.equal(parsed.manifestVersion, 1);
    assert.equal(parsed.pluginId, "manifest-plugin");
    assert.equal(parsed.name, "Manifest Plugin");
    assert.equal(parsed.version, "2.3.4-beta.1");
    assert.equal(parsed.description, "Override description");
    assert.equal(parsed.author, "Zyra Runtime");
    assert.equal(parsed.homepage, "https://plugins.example.test/manifest");
    assert.equal(parsed.license, "Apache-2.0");
    assert.equal(parsed.sourceKind, "project");
    assert.equal(parsed.enabled, true);
    assert.equal(parsed.minimumZyraVersion, "0.1.0");
    assert.equal(parsed.rootPath, pluginRoot);
    assert.equal(parsed.manifestPath, manifestPath);
    assert.equal(parsed.dependencies.length, 2);
    const baseDependency = parsed.dependencies.find((value) => value.pluginId === "base-plugin")!;
    assert.equal(baseDependency.versionRange, "^1.2.0");
    assert.equal(baseDependency.optional, false);
    assert.deepEqual(baseDependency.capabilities, []);
    const optionalDependency = parsed.dependencies.find((value) => value.pluginId === "optional-plugin")!;
    assert.equal(optionalDependency.versionRange, "~3.4.0");
    assert.equal(optionalDependency.optional, true);
    assert.deepEqual(optionalDependency.capabilities, ["skill", "mcp"]);
    assert.equal(parsed.skillRoots.length, 1);
    assert.equal(parsed.skillRoots[0]?.sourceId, "manifest-plugin:skills:1");
    assert.equal(parsed.skillRoots[0]?.rootPath, join(pluginRoot, "skills"));
    assert.equal(parsed.skillRoots[0]?.priority, 450);
    assert.equal(parsed.skillRoots[0]?.maximumDepth, 6);
    assert.deepEqual(parsed.skillRoots[0]?.includePatterns, ["**/SKILL.md"]);
    assert.deepEqual(parsed.skillRoots[0]?.excludePatterns, ["**/draft/**"]);
    assert.equal(parsed.skillRoots[0]?.pluginId, "manifest-plugin");
    assert.equal(parsed.commands[0]?.name, "manifest-inspect");
    assert.equal(parsed.commands[0]?.path, join(pluginRoot, "commands", "inspect.md"));
    assert.deepEqual(parsed.commands[0]?.aliases, ["mi"]);
    assert.equal(parsed.commands[0]?.permission, "read");
    assert.equal(parsed.hooks[0]?.hookId, "manifest-before-read");
    assert.equal(parsed.hooks[0]?.event, "before_tool");
    assert.equal(parsed.hooks[0]?.priority, 90);
    assert.equal(parsed.hooks[0]?.timeoutMs, 1200);
    assert.equal(parsed.hooks[0]?.failClosed, true);
    assert.equal(parsed.hooks[0]?.canMutate, true);
    assert.deepEqual(parsed.hooks[0]?.toolPatterns, ["file_read", "mcp__catalog__*"]);
    assert.equal(parsed.agents[0]?.agentId, "manifest-reader");
    assert.equal(parsed.agents[0]?.model, "model-a");
    assert.equal(parsed.mcpServers[0]?.serverId, "catalog");
    assert.equal(parsed.mcpServers[0]?.configPath, join(pluginRoot, "mcp", "catalog.json"));
    assert.equal(parsed.mcpServers[0]?.enabledByDefault, true);
    assert.equal(parsed.mcpServers[0]?.permissionScope.read, true);
    assert.equal(parsed.environmentHandles.PLUGIN_TOKEN, "credential:manifest-token");
    assert.equal(parsed.permissions.network, false);
    assert.equal(parsed.metadata.execution_id, "E02");
    assert.ok(Number(parsed.metadata.manifest_size_bytes) > 0);
    assert.equal(parsed.manifestDigest.length, 64);
    const { manifestDigest, ...manifestPayload } = parsed;
    assert.equal(manifestDigest, digest(manifestPayload));
    const tooSmall = new PluginManifestRuntime({
      workspaceRoot: workspace,
      maximumManifestBytes: 8,
    });
    await assert.rejects(tooSmall.parse(manifestPath, "project"), /exceeds/i);
    await writeFile(manifestPath, JSON.stringify({
      ...rawManifest,
      version: "not-semver",
    }), "utf8");
    await assert.rejects(parser.parse(manifestPath, "project"), /semantic version/i);
    await writeFile(manifestPath, JSON.stringify({
      ...rawManifest,
      commands: [{ name: "escape", path: "../escape.md" }],
    }), "utf8");
    await assert.rejects(parser.parse(manifestPath, "project"), /escapes/i);
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }
});

test("plugin cache enforces per-plugin CAS, content keys, usage accounting, eviction, invalidation, and restore", () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const cache = new PluginCacheRuntime({
    now,
    maximumEntries: 2,
  });
  const alpha = pluginManifest("cache-alpha");
  const beta = pluginManifest("cache-beta");
  const gamma = pluginManifest("cache-gamma");
  const staged = cache.commit({
    manifest: alpha,
    sourceDigest: "a".repeat(64),
    compiledCapabilities: { skills: ["alpha-skill"] },
    status: "loading",
    expectedRevision: 0,
    metadata: { phase: "staged" },
  });
  assert.match(staged.cacheKey, /^plugin-cache-/);
  assert.equal(staged.pluginId, "cache-alpha");
  assert.equal(staged.pluginVersion, "1.4.0");
  assert.equal(staged.manifestDigest, alpha.manifestDigest);
  assert.equal(staged.sourceDigest, "a".repeat(64));
  assert.equal(staged.revision, 1);
  assert.equal(staged.status, "loading");
  assert.deepEqual(staged.compiledCapabilities.skills, ["alpha-skill"]);
  assert.equal(staged.error, null);
  assert.equal(staged.useCount, 0);
  assert.equal(staged.metadata.phase, "staged");
  assert.throws(() => cache.commit({
    manifest: alpha,
    sourceDigest: "a".repeat(64),
    compiledCapabilities: {},
    status: "active",
    expectedRevision: 0,
  }), /revision/i);
  const active = cache.commit({
    manifest: alpha,
    sourceDigest: "a".repeat(64),
    compiledCapabilities: {
      skills: ["alpha-skill"],
      commands: ["alpha-command"],
    },
    status: "active",
    expectedRevision: 1,
    metadata: { phase: "committed" },
  });
  assert.equal(active.cacheKey, staged.cacheKey);
  assert.equal(active.revision, 2);
  assert.equal(active.createdAt, staged.createdAt);
  assert.notEqual(active.updatedAt, staged.updatedAt);
  assert.equal(active.status, "active");
  assert.equal(active.metadata.phase, "committed");
  assert.equal(cache.get("cache-alpha", "wrong-digest"), null);
  const firstRead = cache.get("cache-alpha", alpha.manifestDigest)!;
  assert.equal(firstRead.useCount, 1);
  assert.notEqual(firstRead.lastUsedAt, active.lastUsedAt);
  cache.commit({
    manifest: beta,
    sourceDigest: "b".repeat(64),
    compiledCapabilities: { commands: ["beta-command"] },
    status: "active",
    expectedRevision: 0,
  });
  const refreshed = cache.get("cache-alpha")!;
  assert.equal(refreshed.useCount, 2);
  cache.commit({
    manifest: gamma,
    sourceDigest: "c".repeat(64),
    compiledCapabilities: { hooks: ["gamma-hook"] },
    status: "failed",
    error: { code: "compile_failed", message: "hook compile failed" },
    expectedRevision: 0,
  });
  assert.equal(cache.list().length, 2);
  assert.equal(cache.get("cache-beta"), null);
  assert.equal(cache.get("cache-alpha")?.status, "active");
  assert.equal(cache.get("cache-gamma")?.error?.code, "compile_failed");
  const snapshot = cache.snapshot();
  assert.equal(snapshot.version, "zyra.plugin-cache/v2");
  assert.equal(snapshot.revision, 4);
  assert.equal(snapshot.entries.length, 2);
  assert.equal(snapshot.digest.length, 64);
  const restored = new PluginCacheRuntime({
    now,
    maximumEntries: 4,
    snapshot,
  });
  assert.equal(restored.list().length, 2);
  assert.equal(restored.get("cache-alpha")?.revision, 2);
  assert.equal(restored.invalidate("cache-alpha"), true);
  assert.equal(restored.invalidate("cache-alpha"), false);
  assert.equal(restored.get("cache-alpha"), null);
  assert.throws(() => new PluginCacheRuntime({
    snapshot: { ...snapshot, digest: "0".repeat(64) },
  }), /digest/i);
});

test("plugin dependency resolution orders valid graphs and fences missing, mismatched, cyclic, stale, and restored states", () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const core = pluginManifest("dependency-core", {
    version: "1.5.0",
    dependencies: [],
  });
  const feature = pluginManifest("dependency-feature", {
    dependencies: [{
      pluginId: "dependency-core",
      versionRange: "^1.2.0",
      optional: false,
      capabilities: ["skill", "command"],
    }, {
      pluginId: "dependency-observer",
      versionRange: "~3.0.0",
      optional: true,
      capabilities: ["hook"],
    }],
  });
  const runtime = new PluginDependencyRuntime({
    now,
    maximumResolutions: 4,
  });
  const valid = runtime.resolve([feature, core], 0, {
    execution_id: "E02",
  });
  assert.match(valid.resolutionId, /^plugin-dependency-resolution-/);
  assert.equal(valid.revision, 1);
  assert.equal(valid.valid, true);
  assert.deepEqual(valid.loadOrder, ["dependency-core", "dependency-feature"]);
  assert.deepEqual(valid.blocked, []);
  assert.deepEqual(valid.cycles, []);
  assert.equal(valid.nodes.length, 2);
  assert.equal(valid.edges.length, 2);
  assert.equal(valid.metadata.execution_id, "E02");
  assert.equal(valid.digest.length, 64);
  const coreNode = valid.nodes.find((node) => node.pluginId === "dependency-core")!;
  const featureNode = valid.nodes.find((node) => node.pluginId === "dependency-feature")!;
  assert.equal(coreNode.status, "ready");
  assert.deepEqual(coreNode.dependents, ["dependency-feature"]);
  assert.equal(coreNode.depth, 0);
  assert.equal(coreNode.loadOrder, 0);
  assert.equal(featureNode.status, "ready");
  assert.equal(featureNode.depth, 1);
  assert.equal(featureNode.loadOrder, 1);
  const requiredEdge = valid.edges.find((edge) => edge.toPluginId === "dependency-core")!;
  assert.equal(requiredEdge.satisfied, true);
  assert.equal(requiredEdge.selectedVersion, "1.5.0");
  assert.equal(requiredEdge.failureCode, null);
  const optionalEdge = valid.edges.find((edge) => edge.toPluginId === "dependency-observer")!;
  assert.equal(optionalEdge.satisfied, false);
  assert.equal(optionalEdge.optional, true);
  assert.equal(optionalEdge.failureCode, null);
  assert.equal(runtime.requireLoadable("dependency-feature").status, "ready");
  assert.equal(runtime.head()?.resolutionId, valid.resolutionId);
  assert.throws(() => runtime.resolve([core], 0), /revision/i);
  assert.throws(() => runtime.requireLoadable("missing-plugin"), /not resolved/i);
  const snapshot = runtime.snapshot();
  assert.equal(snapshot.version, "zyra.plugin-dependency-runtime/v1");
  assert.equal(snapshot.revision, 1);
  assert.equal(snapshot.resolutions.length, 1);
  assert.equal(snapshot.head?.resolutionId, valid.resolutionId);
  const restored = new PluginDependencyRuntime({ now, snapshot });
  assert.equal(restored.head()?.digest, valid.digest);
  assert.equal(restored.requireLoadable("dependency-core").pluginId, "dependency-core");
  assert.throws(() => new PluginDependencyRuntime({
    snapshot: { ...snapshot, digest: "f".repeat(64) },
  }), /digest/i);
  const missingRuntime = new PluginDependencyRuntime({ now });
  const missing = missingRuntime.resolve([feature]);
  assert.equal(missing.valid, false);
  assert.deepEqual(missing.blocked, ["dependency-feature"]);
  assert.equal(missing.nodes[0]?.status, "missing_dependency");
  assert.throws(() => missingRuntime.requireLoadable("dependency-feature"), /blocked|missing_dependency/i);
  const incompatibleCore = pluginManifest("dependency-core", {
    version: "2.0.0",
    dependencies: [],
  });
  const mismatchRuntime = new PluginDependencyRuntime({ now });
  const mismatch = mismatchRuntime.resolve([incompatibleCore, feature]);
  assert.equal(mismatch.valid, false);
  assert.equal(mismatch.nodes.find((node) => node.pluginId === "dependency-feature")?.status, "version_mismatch");
  assert.equal(mismatch.edges.find((edge) => edge.toPluginId === "dependency-core")?.failureCode, "dependency_version_mismatch");
  const cycleA = pluginManifest("cycle-a", {
    dependencies: [{ pluginId: "cycle-b", versionRange: "*", optional: false, capabilities: [] }],
  });
  const cycleB = pluginManifest("cycle-b", {
    dependencies: [{ pluginId: "cycle-a", versionRange: "*", optional: false, capabilities: [] }],
  });
  const cycleRuntime = new PluginDependencyRuntime({ now });
  const cyclic = cycleRuntime.resolve([cycleA, cycleB]);
  assert.equal(cyclic.valid, false);
  assert.deepEqual(cyclic.blocked, ["cycle-a", "cycle-b"]);
  assert.equal(cyclic.cycles.length, 1);
  assert.ok(cyclic.nodes.every((node) => node.status === "cycle"));
  assert.throws(() => cycleRuntime.requireLoadable("cycle-a"), /blocked|cycle/i);
  assert.throws(() => new PluginDependencyRuntime().resolve([core, core]), /duplicate/i);
});

test("plugin capability registry owns claims, permission scopes, leases, conflicts, pins, removal, and restore", () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const manifest = pluginManifest("capability-plugin");
  const runtime = new PluginCapabilityRuntime({
    now,
    maximumRevisions: 6,
  });
  const committed = runtime.commit([manifest], {
    "capability-plugin": 11,
  }, 0, {
    execution_id: "E02",
  });
  assert.match(committed.revisionId, /^plugin-capability-revision-/);
  assert.equal(committed.revisionBefore, 0);
  assert.equal(committed.revisionAfter, 1);
  assert.equal(committed.claims.length, 5);
  assert.equal(committed.added.length, 5);
  assert.deepEqual(committed.updated, []);
  assert.deepEqual(committed.removed, []);
  assert.equal(committed.metadata.execution_id, "E02");
  assert.equal(committed.digest.length, 64);
  assert.equal(runtime.list().length, 5);
  assert.equal(runtime.list({ pluginId: "capability-plugin" }).length, 5);
  assert.equal(runtime.list({ kind: "skill" }).length, 1);
  assert.equal(runtime.list({ kind: "command" }).length, 1);
  assert.equal(runtime.list({ kind: "hook" }).length, 1);
  assert.equal(runtime.list({ kind: "agent" }).length, 1);
  assert.equal(runtime.list({ kind: "mcp_server" }).length, 1);
  const commandClaim = runtime.resolve("command", "capability-plugin-inspect");
  assert.match(commandClaim.claimId, /^plugin-capability-claim-/);
  assert.equal(commandClaim.pluginId, "capability-plugin");
  assert.equal(commandClaim.pluginRevision, 11);
  assert.equal(commandClaim.manifestDigest, manifest.manifestDigest);
  assert.equal(commandClaim.kind, "command");
  assert.equal(commandClaim.capabilityId, "capability-plugin-inspect");
  assert.equal(commandClaim.enabled, true);
  assert.ok(commandClaim.permissionScope.includes("filesystem:read"));
  assert.ok(commandClaim.permissionScope.includes("read"));
  assert.equal(commandClaim.contentDigest.length, 64);
  assert.equal(commandClaim.metadata.plugin_version, "1.4.0");
  const lease = runtime.acquire(
    "command",
    "capability-plugin-inspect",
    "invocation-capability-17",
    1,
    { task_id: "task-17" },
  );
  assert.equal(lease.claim.claimId, commandClaim.claimId);
  assert.match(lease.lease.leaseId, /^plugin-capability-lease-/);
  assert.equal(lease.lease.pluginId, "capability-plugin");
  assert.equal(lease.lease.pluginRevision, 11);
  assert.equal(lease.lease.registryRevision, 1);
  assert.equal(lease.lease.invocationId, "invocation-capability-17");
  assert.equal(lease.lease.releasedAt, null);
  assert.equal(lease.lease.metadata.task_id, "task-17");
  const leasedSnapshot = runtime.snapshot();
  assert.equal(leasedSnapshot.version, "zyra.plugin-capability-runtime/v1");
  assert.equal(leasedSnapshot.revision, 1);
  assert.equal(leasedSnapshot.claims.length, 5);
  assert.equal(leasedSnapshot.revisions.length, 1);
  assert.equal(leasedSnapshot.leases.length, 1);
  assert.equal(leasedSnapshot.leases[0]?.releasedAt, null);
  assert.equal(leasedSnapshot.digest.length, 64);
  const restored = new PluginCapabilityRuntime({ now, snapshot: leasedSnapshot });
  assert.equal(restored.list().length, 5);
  assert.equal(restored.resolve("command", "capability-plugin-inspect").claimId, commandClaim.claimId);
  const restoredSnapshot = restored.snapshot();
  assert.ok(restoredSnapshot.leases[0]?.releasedAt);
  assert.equal(restoredSnapshot.leases[0]?.metadata.restore_release, "process invocation no longer active");
  assert.throws(() => new PluginCapabilityRuntime({
    snapshot: { ...leasedSnapshot, digest: "0".repeat(64) },
  }), /digest/i);
  const pinnedRemoval = runtime.commit([], {}, 1, {
    reason: "remove-while-command-leased",
  });
  assert.equal(pinnedRemoval.revisionAfter, 2);
  assert.equal(pinnedRemoval.claims.length, 1);
  assert.equal(pinnedRemoval.claims[0]?.claimId, commandClaim.claimId);
  assert.equal(pinnedRemoval.removed.length, 4);
  assert.equal(runtime.resolve("command", "capability-plugin-inspect", 2).claimId, commandClaim.claimId);
  assert.throws(() => runtime.resolve("skill", "capability-plugin:capability-plugin-skills", 2), /not found/i);
  lease.release();
  lease.release();
  const removed = runtime.commit([], {}, 2, {
    reason: "lease-released",
  });
  assert.equal(removed.revisionAfter, 3);
  assert.equal(removed.claims.length, 0);
  assert.deepEqual(removed.removed, [commandClaim.claimId]);
  assert.equal(runtime.list().length, 0);
  assert.throws(() => runtime.resolve("command", "capability-plugin-inspect"), /not found/i);
  assert.throws(() => runtime.commit([manifest], {}, 3), /lacks committed revision/i);
  assert.throws(() => runtime.commit([manifest], { "capability-plugin": 12 }, 2), /revision/i);
  const conflictCommand = manifest.commands[0]!;
  const conflictManifest = pluginManifest("capability-conflict", {
    commands: [{
      ...conflictCommand,
      aliases: ["conflicting-alias"],
    }],
  });
  const conflictRuntime = new PluginCapabilityRuntime({ now });
  assert.throws(() => conflictRuntime.commit([manifest, conflictManifest], {
    "capability-plugin": 1,
    "capability-conflict": 1,
  }), /claimed by|conflict/i);
  const disabledManifest = pluginManifest("capability-disabled", {
    enabled: false,
  });
  const disabledRuntime = new PluginCapabilityRuntime({ now });
  disabledRuntime.commit([disabledManifest], { "capability-disabled": 1 });
  assert.equal(disabledRuntime.list({ enabled: false }).length, 5);
  assert.throws(() => disabledRuntime.resolve("command", "capability-disabled-inspect"), /disabled/i);
});

test("plugin hook pipeline orders mutations, ask and deny effects and fails closed on invalid or failed hooks", async () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const hooks = [{
    hookId: "mutate-target",
    event: "before_tool",
    path: "G:/workspace/hooks/mutate.ts",
    command: null,
    priority: 100,
    timeoutMs: 2_000,
    failClosed: true,
    canMutate: true,
    toolPatterns: ["file_*"],
    metadata: { phase: "mutation" },
  }, {
    hookId: "ask-sensitive",
    event: "before_tool",
    path: "G:/workspace/hooks/ask.ts",
    command: null,
    priority: 50,
    timeoutMs: 2_000,
    failClosed: true,
    canMutate: false,
    toolPatterns: ["file_read"],
    metadata: { phase: "approval" },
  }, {
    hookId: "deny-secret",
    event: "before_tool",
    path: "G:/workspace/hooks/deny.ts",
    command: null,
    priority: 10,
    timeoutMs: 2_000,
    failClosed: true,
    canMutate: false,
    toolPatterns: ["file_read"],
    metadata: { phase: "deny" },
  }];
  const manifest = pluginManifest("hook-plugin", { hooks });
  const registrations = hooks.map((hook) => ({
    pluginId: manifest.pluginId,
    pluginRevision: 7,
    manifestDigest: manifest.manifestDigest,
    manifest,
    hook,
  }));
  const executed: string[] = [];
  const runtime = new PluginHookRuntime({
    now,
    maximumResults: 20,
    executor: async ({ hook, context }): Promise<PluginHookExecutorOutput> => {
      executed.push(hook.hookId);
      if (hook.hookId === "mutate-target") {
        return {
          effect: "continue",
          reason: "canonicalized path",
          arguments: {
            ...context.arguments,
            path: String(context.arguments.path).replace(/\\/g, "/"),
            verified: true,
          },
          metadata: { mutation_owner: "typescript" },
        };
      }
      if (hook.hookId === "ask-sensitive") {
        return {
          effect: "ask",
          reason: "sensitive artifact requires approval",
          metadata: { approval_scope: "artifact" },
        };
      }
      return {
        effect: "deny",
        reason: "secret path is denied",
        metadata: { rule: "deny-secret" },
      };
    },
  });
  assert.equal(runtime.replace(registrations, 0), 1);
  assert.throws(() => runtime.replace(registrations, 0), /revision/i);
  const argumentsValue = { path: "docs\\evidence.md", intent: "read" };
  const context = {
    runId: "run-hook-17",
    taskId: "task-hook-17",
    sessionId: "session-hook-17",
    sessionRevision: 4,
    workerRequestId: "worker-hook-17",
    toolCallId: "tool-call-hook-17",
    toolName: "file_read",
    namespace: "builtin",
    serverId: "",
    operation: "filesystem/read",
    arguments: argumentsValue,
    argumentsDigest: digest(argumentsValue),
    metadata: { execution_id: "E02" },
  };
  const denied = await runtime.beforeTool(context);
  assert.equal(denied.effect, "deny");
  assert.equal(denied.reason, "secret path is denied");
  assert.equal(denied.originalArgumentsDigest, digest(argumentsValue));
  assert.notEqual(denied.finalArgumentsDigest, denied.originalArgumentsDigest);
  assert.equal(denied.finalArgumentsDigest, digest(denied.arguments));
  assert.equal(denied.arguments.path, "docs/evidence.md");
  assert.equal(denied.arguments.verified, true);
  assert.equal(denied.mutated, true);
  assert.equal(denied.results.length, 3);
  assert.equal(denied.failedClosed, false);
  assert.equal(denied.revision, 1);
  assert.equal(denied.digest.length, 64);
  assert.deepEqual(executed, ["mutate-target", "ask-sensitive", "deny-secret"]);
  assert.equal(denied.results[0]?.mutated, true);
  assert.equal(denied.results[0]?.metadata.mutation_owner, "typescript");
  assert.equal(denied.results[1]?.effect, "ask");
  assert.equal(denied.results[2]?.effect, "deny");
  assert.equal(runtime.listResults().length, 3);
  const ignored = await runtime.beforeTool({
    ...context,
    toolCallId: "tool-call-hook-18",
    toolName: "shell",
  });
  assert.equal(ignored.effect, "continue");
  assert.equal(ignored.results.length, 0);
  assert.equal(ignored.mutated, false);
  await assert.rejects(runtime.beforeTool({
    ...context,
    argumentsDigest: "0".repeat(64),
  }), /digest/i);
  const askRuntime = new PluginHookRuntime({
    now,
    executor: async ({ hook, context: hookContext }) => hook.hookId === "mutate-target"
      ? { arguments: { ...hookContext.arguments, verified: true } }
      : { effect: "ask", reason: "approval required" },
  });
  askRuntime.replace(registrations.slice(0, 2));
  const asked = await askRuntime.beforeTool(context);
  assert.equal(asked.effect, "ask");
  assert.equal(asked.reason, "approval required");
  assert.equal(asked.mutated, true);
  assert.equal(asked.results.length, 2);
  const unauthorizedManifest = pluginManifest("hook-unauthorized", {
    hooks: [{
      ...hooks[0]!,
      hookId: "unauthorized-mutation",
      canMutate: false,
    }],
  });
  const unauthorized = new PluginHookRuntime({
    now,
    executor: async ({ context: hookContext }) => ({
      arguments: { ...hookContext.arguments, injected: true },
    }),
  });
  unauthorized.replace([{
    pluginId: unauthorizedManifest.pluginId,
    pluginRevision: 1,
    manifestDigest: unauthorizedManifest.manifestDigest,
    manifest: unauthorizedManifest,
    hook: unauthorizedManifest.hooks[0]!,
  }]);
  const unauthorizedResult = await unauthorized.beforeTool(context);
  assert.equal(unauthorizedResult.effect, "deny");
  assert.equal(unauthorizedResult.failedClosed, true);
  assert.match(unauthorizedResult.reason, /unauthorized mutation/i);
  const failingManifest = pluginManifest("hook-failing", {
    hooks: [{
      ...hooks[0]!,
      hookId: "failing-closed",
      canMutate: false,
      failClosed: true,
    }],
  });
  const failing = new PluginHookRuntime({
    now,
    executor: async () => {
      throw new Error("hook process failed");
    },
  });
  failing.replace([{
    pluginId: failingManifest.pluginId,
    pluginRevision: 1,
    manifestDigest: failingManifest.manifestDigest,
    manifest: failingManifest,
    hook: failingManifest.hooks[0]!,
  }]);
  const failedClosed = await failing.beforeTool(context);
  assert.equal(failedClosed.effect, "deny");
  assert.equal(failedClosed.failedClosed, true);
  assert.match(failedClosed.reason, /failed closed/i);
  assert.equal(failedClosed.results[0]?.failure?.message, "hook process failed");
  const openManifest = pluginManifest("hook-open", {
    hooks: [{
      ...hooks[0]!,
      hookId: "failing-open",
      canMutate: false,
      failClosed: false,
    }],
  });
  const open = new PluginHookRuntime({
    now,
    executor: async () => {
      throw new Error("optional hook failed");
    },
  });
  open.replace([{
    pluginId: openManifest.pluginId,
    pluginRevision: 1,
    manifestDigest: openManifest.manifestDigest,
    manifest: openManifest,
    hook: openManifest.hooks[0]!,
  }]);
  const failedOpen = await open.beforeTool(context);
  assert.equal(failedOpen.effect, "continue");
  assert.equal(failedOpen.failedClosed, false);
  assert.equal(failedOpen.results[0]?.failure?.message, "optional hook failed");
  assert.throws(() => open.replace([{
    pluginId: "wrong-plugin",
    pluginRevision: 1,
    manifestDigest: openManifest.manifestDigest,
    manifest: openManifest,
    hook: openManifest.hooks[0]!,
  }], 1), /plugin id mismatch/i);
});

test("plugin runtime loads, deduplicates, reloads, restores, rolls back failed integration, and unloads cleanly", async () => {
  const temporaryRoot = join(process.cwd(), ".tmp");
  await mkdir(temporaryRoot, { recursive: true });
  const workspace = await mkdtemp(join(temporaryRoot, "zyra-e02-plugin-runtime-"));
  const activeRoot = join(workspace, "active-plugin");
  const failedRoot = join(workspace, "failed-plugin");
  await mkdir(activeRoot, { recursive: true });
  await mkdir(failedRoot, { recursive: true });
  const activeManifestPath = join(activeRoot, "plugin.json");
  const failedManifestPath = join(failedRoot, "plugin.json");
  const manifestValue = (id: string, version: string) => ({
    manifest_version: 1,
    id,
    name: `${id} display`,
    version,
    description: `Runtime plugin ${id}`,
    enabled: true,
    skills: [],
    commands: [],
    hooks: [],
    agents: [],
    mcp: {},
    permissions: { filesystem: ["read"], network: false },
    metadata: { execution_id: "E02" },
  });
  await writeFile(activeManifestPath, JSON.stringify(manifestValue("runtime-active", "1.0.0")), "utf8");
  await writeFile(failedManifestPath, JSON.stringify(manifestValue("runtime-failed", "1.0.0")), "utf8");
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const calls: string[] = [];
  const removals: JsonObject[] = [];
  const integration = {
    addSkills: async (manifest: PluginManifest) => {
      calls.push(`${manifest.pluginId}:skills`);
      return { count: manifest.skillRoots.length };
    },
    addCommands: async (manifest: PluginManifest) => {
      calls.push(`${manifest.pluginId}:commands`);
      return { count: manifest.commands.length };
    },
    addHooks: async (manifest: PluginManifest) => {
      calls.push(`${manifest.pluginId}:hooks`);
      if (manifest.pluginId === "runtime-failed") throw new Error("hook integration failed");
      return { count: manifest.hooks.length };
    },
    addAgents: async (manifest: PluginManifest) => {
      calls.push(`${manifest.pluginId}:agents`);
      return { count: manifest.agents.length };
    },
    addMcpServers: async (manifest: PluginManifest) => {
      calls.push(`${manifest.pluginId}:mcp`);
      return { count: manifest.mcpServers.length };
    },
    remove: async (pluginId: string, revision: number) => {
      removals.push({ plugin_id: pluginId, revision });
    },
  };
  const parser = new PluginManifestRuntime({
    workspaceRoot: workspace,
    allowOutsideWorkspace: false,
  });
  const cache = new PluginCacheRuntime({ now });
  const runtime = new PluginRuntime({
    manifests: parser,
    cache,
    integration,
    now,
  });
  try {
    const loaded = await runtime.load(activeManifestPath, "project");
    assert.equal(loaded.pluginId, "runtime-active");
    assert.equal(loaded.revision, 1);
    assert.match(loaded.revisionId, /^plugin-revision-/);
    assert.equal(loaded.status, "active");
    assert.equal(loaded.manifestDigest.length, 64);
    assert.equal(loaded.capabilityDigest.length, 64);
    assert.deepEqual(loaded.skillRoots, []);
    assert.deepEqual(loaded.commands, []);
    assert.deepEqual(loaded.hooks, []);
    assert.deepEqual(loaded.agents, []);
    assert.deepEqual(loaded.mcpServers, []);
    assert.equal((loaded.metadata.compiled_capabilities as JsonObject).skills !== undefined, true);
    assert.deepEqual(calls, [
      "runtime-active:skills",
      "runtime-active:commands",
      "runtime-active:hooks",
      "runtime-active:agents",
      "runtime-active:mcp",
    ]);
    assert.equal(runtime.list().length, 1);
    assert.equal(runtime.get("runtime-active")?.status, "active");
    assert.equal(runtime.get("runtime-active")?.activeInvocations, 0);
    assert.equal(cache.get("runtime-active")?.status, "active");
    const repeated = await runtime.load(activeManifestPath, "project");
    assert.equal(repeated.revisionId, loaded.revisionId);
    assert.equal(calls.length, 5);
    const acquired = runtime.acquire("runtime-active");
    assert.equal(acquired.record.activeInvocations, 1);
    assert.equal(runtime.get("runtime-active")?.activeInvocations, 1);
    acquired.release();
    acquired.release();
    assert.equal(runtime.get("runtime-active")?.activeInvocations, 0);
    await writeFile(activeManifestPath, JSON.stringify(manifestValue("runtime-active", "1.1.0")), "utf8");
    const reloaded = await runtime.reload("runtime-active", activeManifestPath, "project");
    assert.equal(reloaded.pluginId, "runtime-active");
    assert.equal(reloaded.revision, 2);
    assert.notEqual(reloaded.revisionId, loaded.revisionId);
    assert.notEqual(reloaded.manifestDigest, loaded.manifestDigest);
    assert.equal(reloaded.status, "active");
    assert.equal(calls.length, 10);
    assert.equal(runtime.get("runtime-active")?.manifest.version, "1.1.0");
    assert.equal(runtime.get("runtime-active")?.metadata.replaced_revision, 1);
    assert.equal(cache.get("runtime-active")?.pluginVersion, "1.1.0");
    const snapshot = runtime.snapshot();
    assert.equal(snapshot.version, "zyra.plugin-runtime/v2");
    assert.equal(snapshot.revision, 2);
    assert.equal(snapshot.records.length, 1);
    assert.equal(snapshot.revisions.length, 2);
    assert.equal(snapshot.cache.length, 1);
    assert.equal(snapshot.digest.length, 64);
    const restored = new PluginRuntime({
      manifests: parser,
      cache: new PluginCacheRuntime({ now }),
      integration,
      now,
      snapshot,
    });
    assert.equal(restored.list().length, 1);
    assert.equal(restored.get("runtime-active")?.status, "active");
    assert.equal(restored.get("runtime-active")?.activeInvocations, 0);
    assert.throws(() => new PluginRuntime({
      manifests: parser,
      cache: new PluginCacheRuntime({ now }),
      integration,
      snapshot: { ...snapshot, digest: "0".repeat(64) },
    }), /digest/i);
    await assert.rejects(runtime.load(failedManifestPath, "project"), /hook integration failed/i);
    assert.equal(runtime.get("runtime-failed")?.status, "failed");
    assert.equal(runtime.get("runtime-failed")?.failure?.message, "hook integration failed");
    assert.equal(cache.get("runtime-failed")?.status, "failed");
    assert.equal(cache.get("runtime-failed")?.error?.message, "hook integration failed");
    assert.ok(removals.some((entry) => entry.plugin_id === "runtime-failed" && entry.revision === 0));
    assert.throws(() => runtime.acquire("runtime-failed"), /not active/i);
    assert.equal(await runtime.unload("missing-plugin"), false);
    const unloaded = await runtime.unload("runtime-active", "test-complete");
    assert.equal(unloaded, true);
    assert.equal(runtime.get("runtime-active"), null);
    assert.equal(cache.get("runtime-active"), null);
    assert.ok(removals.some((entry) => entry.plugin_id === "runtime-active" && entry.revision === 2));
    assert.equal(runtime.snapshot().revisions.at(-1)?.removed[0], "runtime-active");
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }
});

test("command descriptors and registry enforce precedence, alias ownership, revisions, pins, filters, and restore", () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const inspectLow = commandDescriptor({
    name: "inspect",
    sourceId: "user-inspect",
    priority: 100,
    aliases: ["inspect-low", "shared-inspect"],
  });
  const inspectHigh = commandDescriptor({
    name: "inspect",
    sourceId: "project-inspect",
    priority: 500,
    aliases: ["inspect-high"],
  });
  const alpha = commandDescriptor({
    name: "alpha",
    sourceId: "alpha-source",
    priority: 200,
    aliases: ["shared-alias", "alpha-short"],
  });
  const beta = commandDescriptor({
    name: "beta",
    sourceId: "beta-source",
    priority: 600,
    aliases: ["shared-alias", "beta-short"],
  });
  const hidden = commandDescriptor({
    name: "internal-status",
    sourceId: "hidden-source",
    priority: 300,
    aliases: ["internal"],
    hidden: true,
  });
  const disabled = commandDescriptor({
    name: "disabled-command",
    sourceId: "disabled-source",
    priority: 300,
    enabled: false,
  });
  assert.equal(inspectHigh.commandId, "project-inspect:inspect");
  assert.equal(inspectHigh.name, "inspect");
  assert.deepEqual(inspectHigh.aliases, ["inspect-high"]);
  assert.equal(inspectHigh.displayName, "inspect display");
  assert.equal(inspectHigh.description, "Execute inspect");
  assert.equal(inspectHigh.usage, "/inspect <target>");
  assert.deepEqual(inspectHigh.examples, ["/inspect task-17"]);
  assert.equal(inspectHigh.category, "runtime");
  assert.equal(inspectHigh.sourceKind, "project");
  assert.equal(inspectHigh.sourceId, "project-inspect");
  assert.equal(inspectHigh.sourcePriority, 500);
  assert.equal(inspectHigh.hidden, false);
  assert.equal(inspectHigh.enabled, true);
  assert.equal(inspectHigh.arguments.length, 1);
  assert.equal(inspectHigh.arguments[0]?.name, "target");
  assert.equal(inspectHigh.arguments[0]?.required, true);
  assert.equal(inspectHigh.arguments[0]?.positional, true);
  assert.equal(inspectHigh.options[0]?.name, "verbose");
  assert.equal(inspectHigh.options[0]?.short, "v");
  assert.equal(inspectHigh.options[0]?.type, "boolean");
  assert.equal(inspectHigh.handler.kind, "local");
  assert.equal(inspectHigh.handler.handlerId, "local:inspect");
  assert.equal(inspectHigh.permission.operation, "command/inspect");
  assert.equal(inspectHigh.permission.risk, "low");
  assert.equal(inspectHigh.permission.processExecution, true);
  assert.equal(inspectHigh.permission.denyInSealed, false);
  assert.equal(inspectHigh.metadata.execution_id, "E02");
  assert.equal(inspectHigh.descriptorDigest.length, 64);
  const parser = new CommandDescriptorRuntime();
  assert.throws(() => parser.parseText({
    text: "---\nname: invalid-handler\nhandler: {\"kind\":\"invalid\"}\n---\nbody",
    path: null,
    sourceKind: "project",
    sourceId: "invalid",
    sourcePriority: 1,
  }), /handler kind/i);
  assert.throws(() => parser.parseText({
    text: "---\nname: duplicate-option\noptions: [{\"name\":\"mode\"},{\"name\":\"mode\"}]\n---\nbody",
    path: null,
    sourceKind: "project",
    sourceId: "invalid",
    sourcePriority: 1,
  }), /duplicate command option/i);
  assert.throws(() => parser.parseText({
    text: "---\nname: bad-positionals\narguments: [{\"name\":\"optional\",\"required\":false},{\"name\":\"required\",\"required\":true}]\n---\nbody",
    path: null,
    sourceKind: "project",
    sourceId: "invalid",
    sourcePriority: 1,
  }), /required argument.*after optional/i);
  const registry = new CommandRegistryRuntime({
    now,
    maximumRevisions: 1,
  });
  const first = registry.register([
    inspectLow,
    inspectHigh,
    alpha,
    beta,
    hidden,
    disabled,
  ], 0, {
    execution_id: "E02",
  });
  assert.equal(first.revision.revision, 1);
  assert.equal(first.revision.parentRevisionId, null);
  assert.equal(first.revision.commands.length, 5);
  assert.equal(first.revision.added.length, 5);
  assert.deepEqual(first.revision.updated, []);
  assert.deepEqual(first.revision.removed, []);
  assert.deepEqual(first.revision.shadowed, [inspectLow.commandId]);
  assert.equal(first.revision.metadata.execution_id, "E02");
  assert.equal(first.revision.digest.length, 64);
  assert.equal(first.active.length, 5);
  assert.ok(first.conflicts.some((value) => value.type === "name" && value.winner === inspectHigh.commandId));
  assert.ok(first.conflicts.some((value) => value.type === "alias" && value.winner === beta.commandId));
  assert.equal(registry.currentRevision, 1);
  assert.equal(registry.resolve("inspect").commandId, inspectHigh.commandId);
  assert.equal(registry.resolve("/inspect-high").commandId, inspectHigh.commandId);
  assert.equal(registry.resolve("shared-alias").commandId, beta.commandId);
  assert.equal(registry.resolve("beta-short").commandId, beta.commandId);
  assert.throws(() => registry.resolve("inspect-low"), /not found/i);
  assert.throws(() => registry.resolve("disabled-command"), /disabled/i);
  assert.equal(registry.list().length, 5);
  assert.equal(registry.list({ hidden: true }).length, 1);
  assert.equal(registry.list({ hidden: true })[0]?.name, "internal-status");
  assert.equal(registry.list({ hidden: false }).length, 4);
  assert.equal(registry.list({ category: "runtime" }).length, 5);
  assert.equal(registry.list({ sourceId: "project-inspect" })[0]?.commandId, inspectHigh.commandId);
  assert.throws(() => registry.register([inspectHigh], 0), /revision/i);
  const release = registry.pin(1);
  const second = registry.register([inspectHigh, beta], 1, {
    reason: "prune-command-set",
  });
  assert.equal(second.revision.revision, 2);
  assert.equal(second.revision.parentRevisionId, first.revision.revisionId);
  assert.deepEqual(second.revision.added, []);
  assert.deepEqual(second.revision.updated, []);
  assert.deepEqual(second.revision.removed.sort(), [alpha.commandId, disabled.commandId, hidden.commandId].sort());
  assert.equal(registry.resolve("alpha", 1).commandId, alpha.commandId);
  assert.equal(registry.resolve("inspect", 2).commandId, inspectHigh.commandId);
  release();
  release();
  assert.throws(() => registry.resolve("alpha", 1), /revision.*not found/i);
  const snapshot = registry.snapshot();
  assert.equal(snapshot.version, "zyra.command-registry/v2");
  assert.equal(snapshot.revision, 2);
  assert.equal(snapshot.revisions.length, 1);
  assert.equal(snapshot.active.length, 2);
  assert.equal(snapshot.aliasIndex["inspect-high"], inspectHigh.commandId);
  assert.equal(snapshot.aliasIndex["shared-alias"], beta.commandId);
  assert.equal(snapshot.digest.length, 64);
  const restored = new CommandRegistryRuntime({ now, snapshot });
  assert.equal(restored.currentRevision, 2);
  assert.equal(restored.resolve("inspect-high").commandId, inspectHigh.commandId);
  assert.equal(restored.resolve("shared-alias").commandId, beta.commandId);
  assert.equal(restored.list().length, 2);
  assert.throws(() => new CommandRegistryRuntime({
    snapshot: { ...snapshot, digest: "0".repeat(64) },
  }), /digest/i);
});

test("command help and completion expose visible commands, options, enums, paths, dynamic values, and stable documents", () => {
  const parser = new CommandDescriptorRuntime();
  const deploy = parser.parseText({
    text: `---
name: deploy
aliases: ["release","ship"]
display_name: Deploy Workload
description: Deploy a workload through the canonical runtime
usage: /deploy <environment> [--mode MODE] [--path PATH]
examples: ["/deploy staging --mode safe","/deploy production --path apps/api"]
category: delivery
handler: {"kind":"control","id":"control:deploy","control":"deploy"}
permission: {"operation":"command/deploy","risk":"medium","ask_in_interactive":true,"deny_in_sealed":true,"workspace_mutation":true,"network_access":false,"process_execution":false}
arguments: [{"name":"environment","description":"Deployment environment","required":true,"positional":true,"type":"enum","enum":["staging","production"]}]
options: [{"name":"mode","short":"m","description":"Deployment mode","type":"enum","enum":["safe","fast"]},{"name":"path","short":"p","description":"Workspace path","type":"path"},{"name":"force","short":"f","description":"Force deployment","type":"boolean","default":false}]
metadata: {"execution_id":"E02"}
---
Deploy the selected workload after permission validation.`,
    path: "G:/workspace/.zyra/commands/deploy.md",
    sourceKind: "project",
    sourceId: "project-deploy",
    sourcePriority: 500,
  });
  const hidden = commandDescriptor({
    name: "deploy-internal",
    hidden: true,
    priority: 400,
  });
  const registry = new CommandRegistryRuntime({
    now: () => new Date(instant),
  });
  registry.register([deploy, hidden]);
  const completion = new CommandCompletionRuntime(registry);
  const commandItems = completion.complete({
    input: "/dep",
    cursor: 4,
    registryRevision: 1,
    metadata: { phase: "command" },
  });
  assert.match(commandItems.completionId, /^command-completion-/);
  assert.equal(commandItems.commandName, null);
  assert.equal(commandItems.registryRevision, 1);
  assert.equal(commandItems.tokenIndex, 0);
  assert.equal(commandItems.metadata.phase, "command");
  assert.equal(commandItems.digest.length, 64);
  assert.ok(commandItems.items.some((item) => item.value === "deploy" && item.kind === "command"));
  assert.ok(commandItems.items.every((item) => item.value !== "deploy-internal"));
  const hiddenItems = completion.complete({
    input: "/deploy-",
    cursor: 8,
    registryRevision: 1,
    includeHidden: true,
  });
  assert.ok(hiddenItems.items.some((item) => item.value === "deploy-internal"));
  const aliasItems = completion.complete({
    input: "/rel",
    cursor: 4,
    registryRevision: 1,
  });
  const alias = aliasItems.items.find((item) => item.value === "release")!;
  assert.equal(alias.kind, "alias");
  assert.equal(alias.commandId, deploy.commandId);
  assert.equal(alias.replacementStart, 1);
  assert.equal(alias.replacementEnd, 4);
  const optionItems = completion.complete({
    input: "/deploy --m",
    cursor: 11,
    registryRevision: 1,
  });
  assert.equal(optionItems.commandName, "deploy");
  assert.ok(optionItems.items.some((item) => item.value === "--mode" && item.kind === "option"));
  const shortOptionItems = completion.complete({
    input: "/deploy -m",
    cursor: 10,
    registryRevision: 1,
  });
  assert.ok(shortOptionItems.items.some((item) => item.value === "-m" && item.kind === "option"));
  const enumItems = completion.complete({
    input: "/deploy st",
    cursor: 10,
    registryRevision: 1,
    dynamicValues: { environment: ["staging-canary", "custom"] },
  });
  assert.ok(enumItems.items.some((item) => item.value === "staging" && item.kind === "enum"));
  assert.ok(enumItems.items.some((item) => item.value === "staging-canary" && item.kind === "dynamic"));
  assert.ok(enumItems.items.every((item) => item.value.startsWith("st")));
  const dynamicItems = completion.complete({
    input: "/deploy cu",
    cursor: 10,
    registryRevision: 1,
    dynamicValues: { environment: ["custom", "customer-preview"] },
  });
  assert.deepEqual(dynamicItems.items.map((item) => item.value), ["custom", "customer-preview"]);
  const pathItems = completion.complete({
    input: "/deploy staging --path docs",
    cursor: 27,
    registryRevision: 1,
    workspacePaths: ["docs", "docs/evidence", "apps/api"],
  });
  assert.ok(pathItems.items.some((item) => item.value === "docs" && item.kind === "path"));
  assert.ok(pathItems.items.some((item) => item.value === "docs/evidence" && item.kind === "path"));
  assert.ok(pathItems.items.every((item) => item.value.startsWith("docs")));
  const capped = completion.complete({
    input: "/deploy ",
    cursor: 8,
    registryRevision: 1,
    dynamicValues: { environment: ["a", "b", "c"] },
    maximumResults: 2,
  });
  assert.equal(capped.items.length, 2);
  const help = new CommandHelpRuntime(registry);
  const document = help.render("deploy");
  assert.match(document.documentId, /^command-help-document-/);
  assert.equal(document.commandId, deploy.commandId);
  assert.equal(document.commandName, "deploy");
  assert.equal(document.registryRevision, 1);
  assert.equal(document.title, "Deploy Workload");
  assert.equal(document.synopsis, "/deploy <environment> [--mode MODE] [--path PATH]");
  assert.equal(document.description, "Deploy a workload through the canonical runtime");
  assert.ok(document.sections.some((section) => section.heading === "Arguments"));
  assert.ok(document.sections.some((section) => section.heading === "Options"));
  assert.ok(document.sections.some((section) => section.heading === "Aliases"));
  assert.ok(document.sections.some((section) => section.heading === "Examples"));
  assert.ok(document.sections.some((section) => section.heading === "Permission"));
  assert.ok(document.sections.some((section) => section.heading === "Handler"));
  assert.match(document.plainText, /interactive approval: required/i);
  assert.match(document.plainText, /workspace mutation/i);
  assert.match(document.markdown, /# Deploy Workload/);
  assert.match(document.markdown, /## Options/);
  assert.equal(document.metadata.category, "delivery");
  assert.equal(document.metadata.source_kind, "project");
  assert.equal(document.digest.length, 64);
  const cached = help.render("release");
  assert.equal(cached.documentId, document.documentId);
  assert.equal(cached.digest, document.digest);
  const visibleIndex = help.index();
  assert.equal((visibleIndex.commands as unknown[]).length, 1);
  assert.equal(((visibleIndex.commands as JsonObject[])[0]?.name), "deploy");
  const fullIndex = help.index({ includeHidden: true });
  assert.equal((fullIndex.commands as unknown[]).length, 2);
  assert.equal(String(fullIndex.digest).length, 64);
  const categoryIndex = help.index({ category: "delivery" });
  assert.equal((categoryIndex.commands as unknown[]).length, 1);
  help.clear();
  assert.equal(help.render("deploy").digest, document.digest);
  assert.throws(() => help.render("missing-command"), /not found/i);
});

test("local command execution deduplicates calls and preserves emitted artifacts, events, failures, cancellation, and retention", async () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const runtime = new LocalCommandRuntime({
    now,
    maximumResults: 2,
  });
  const inspect = commandDescriptor({
    name: "local-inspect",
    kind: "local",
  });
  let handlerCalls = 0;
  runtime.register(inspect.handler.handlerId, async (context) => {
    handlerCalls += 1;
    assert.equal(context.descriptor.commandId, inspect.commandId);
    assert.equal(context.request.registryRevision, 1);
    assert.equal(context.arguments.target, context.request.arguments[0]);
    assert.equal(context.arguments.verbose, true);
    context.emitArtifact({
      artifact_id: `artifact-${context.invocationId}`,
      kind: "inspection",
    });
    context.emitEvent({
      event: "command.inspected",
      target: context.arguments.target,
    });
    return {
      output: {
        ok: true,
        target: context.arguments.target,
      },
      artifacts: [{
        artifact_id: `summary-${context.invocationId}`,
        kind: "summary",
      }],
      metadata: {
        handler: "typescript",
      },
    };
  });
  assert.throws(() => runtime.register(inspect.handler.handlerId, async () => ({ output: null })), /already registered/i);
  const firstRequest = commandRequest("/local-inspect task-17 --verbose", 1, {
    commandName: "local-inspect",
    arguments: ["task-17"],
    options: { verbose: true },
  });
  const first = await runtime.execute(inspect, firstRequest, {
    target: "task-17",
    verbose: true,
  });
  assert.match(first.invocationId, /^local-command-/);
  assert.equal(first.handlerId, inspect.handler.handlerId);
  assert.equal(first.commandId, inspect.commandId);
  assert.equal(first.status, "completed");
  assert.deepEqual(first.output, { ok: true, target: "task-17" });
  assert.equal(first.artifacts.length, 2);
  assert.equal(first.artifacts[0]?.kind, "inspection");
  assert.equal(first.artifacts[1]?.kind, "summary");
  assert.equal(first.events.length, 1);
  assert.equal(first.events[0]?.event, "command.inspected");
  assert.ok(first.startedAt);
  assert.ok(first.completedAt);
  assert.ok(first.durationMs >= 0);
  assert.equal(first.failure, null);
  assert.equal(first.metadata.handler, "typescript");
  assert.equal(runtime.get(first.invocationId)?.invocationId, first.invocationId);
  const replay = await runtime.execute(inspect, firstRequest, {
    target: "task-17",
    verbose: true,
  });
  assert.equal(replay.invocationId, first.invocationId);
  assert.equal(replay.completedAt, first.completedAt);
  assert.equal(handlerCalls, 1);
  for (const target of ["task-18", "task-19"]) {
    const request = commandRequest(`/local-inspect ${target} --verbose`, 1, {
      commandName: "local-inspect",
      arguments: [target],
      options: { verbose: true },
    });
    const result = await runtime.execute(inspect, request, {
      target,
      verbose: true,
    });
    assert.equal(result.status, "completed");
    assert.equal((result.output as JsonObject).target, target);
  }
  assert.equal(handlerCalls, 3);
  assert.equal(runtime.get(first.invocationId), null);
  const failureDescriptor = commandDescriptor({
    name: "local-failure",
    kind: "local",
  });
  runtime.register(failureDescriptor.handler.handlerId, async (context) => {
    context.emitEvent({ event: "command.failure.started" });
    throw new Error("local handler failed");
  });
  await assert.rejects(runtime.execute(
    failureDescriptor,
    commandRequest("/local-failure task-17", 1),
    { target: "task-17", verbose: false },
  ), /local handler failed/i);
  const cancelledDescriptor = commandDescriptor({
    name: "local-cancelled",
    kind: "local",
  });
  let cancelledCalled = false;
  runtime.register(cancelledDescriptor.handler.handlerId, async () => {
    cancelledCalled = true;
    return { output: null };
  });
  const controller = new AbortController();
  controller.abort(new Error("cancel test"));
  await assert.rejects(runtime.execute(
    cancelledDescriptor,
    commandRequest("/local-cancelled task-17", 1),
    { target: "task-17", verbose: false },
    controller.signal,
  ), /cancelled/i);
  assert.equal(cancelledCalled, false);
  assert.equal(runtime.unregister("missing-handler"), false);
  assert.equal(runtime.unregister(cancelledDescriptor.handler.handlerId), true);
  await assert.rejects(runtime.execute(
    cancelledDescriptor,
    commandRequest("/local-cancelled task-18", 1),
    { target: "task-18", verbose: false },
  ), /not registered/i);
  runtime.register(inspect.handler.handlerId, async () => ({ output: { replaced: true } }), true);
  const replaced = await runtime.execute(
    inspect,
    commandRequest("/local-inspect replacement --verbose", 1),
    { target: "replacement", verbose: true },
  );
  assert.deepEqual(replaced.output, { replaced: true });
});

test("command dispatch and history enforce allow, deny, ask, sealed recovery, exact digests, external routing, and hash restore", async () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const localDescriptor = commandDescriptor({
    name: "dispatch-local",
    kind: "local",
  });
  const skillDescriptor = commandDescriptor({
    name: "dispatch-skill",
    kind: "skill",
  });
  const registry = new CommandRegistryRuntime({ now });
  registry.register([localDescriptor, skillDescriptor]);
  const local = new LocalCommandRuntime({ now });
  const authorizedArguments: JsonObject[] = [];
  local.register(localDescriptor.handler.handlerId, async (context) => {
    authorizedArguments.push(context.arguments);
    return {
      output: {
        handler: "local",
        target: context.arguments.target,
        verbose: context.arguments.verbose,
      },
      metadata: { runtime: "typescript" },
    };
  });
  const dispatcherCalls: JsonObject[] = [];
  const external = {
    skill: async (descriptorValue: CommandDescriptor, request: CommandInvocationRequest, argumentsValue: JsonObject) => {
      dispatcherCalls.push({ kind: "skill", command_id: descriptorValue.commandId, call_id: request.identity.commandCallId, arguments: argumentsValue });
      return { handler: "skill", target: argumentsValue.target };
    },
    mcpPrompt: async () => ({ handler: "mcp_prompt" }),
    plugin: async () => ({ handler: "plugin" }),
    control: async () => ({ handler: "control" }),
    builtin: async () => ({ handler: "builtin" }),
  };
  let permissionCalls = 0;
  const permission = async (descriptorValue: CommandDescriptor, request: CommandInvocationRequest, argumentsValue: JsonObject) => {
    permissionCalls += 1;
    const requestedEffect = String(request.metadata.permission_effect ?? "allow") as "allow" | "deny" | "ask";
    const requestDigest = digest({
      command_id: descriptorValue.commandId,
      descriptor_digest: descriptorValue.descriptorDigest,
      request_identity: request.identity,
      arguments: argumentsValue,
      permission: descriptorValue.permission,
    });
    const finalArguments = request.metadata.authorized_target
      ? { ...argumentsValue, target: request.metadata.authorized_target }
      : argumentsValue;
    return {
      effect: requestedEffect,
      decisionId: `command-decision-${permissionCalls}`,
      reasonCode: `test_${requestedEffect}`,
      reason: `test permission ${requestedEffect}`,
      requestDigest: request.metadata.invalid_digest === true ? "0".repeat(64) : requestDigest,
      continuationId: requestedEffect === "ask" ? `continuation-${permissionCalls}` : null,
      replanRequired: requestedEffect === "deny",
      recoveryInput: requestedEffect === "allow" ? null : {
        kind: "command_permission",
        effect: requestedEffect,
        command_id: descriptorValue.commandId,
      },
      metadata: {
        descriptor_digest: descriptorValue.descriptorDigest,
        arguments_digest: digest(argumentsValue),
        final_arguments: finalArguments,
        final_arguments_digest: digest(finalArguments),
      },
    };
  };
  const dispatch = new CommandDispatchRuntime({
    registry,
    local,
    permission,
    dispatchers: external,
    now,
    maximumResults: 20,
  });
  const allowRequest = commandRequest("/dispatch-local task-17 --verbose", 1, {
    metadata: {
      execution_id: "E02",
      permission_effect: "allow",
      authorized_target: "task-17-authorized",
    },
  });
  const allowed = await dispatch.dispatch(allowRequest);
  assert.match(allowed.invocationId, /^command-invocation-/);
  assert.equal(allowed.commandId, localDescriptor.commandId);
  assert.equal(allowed.commandName, "dispatch-local");
  assert.equal(allowed.registryRevision, 1);
  assert.equal(allowed.status, "completed");
  assert.equal((allowed.output as JsonObject).handler, "local");
  assert.equal((allowed.output as JsonObject).target, "task-17-authorized");
  assert.equal((allowed.output as JsonObject).verbose, true);
  assert.deepEqual(allowed.artifacts, []);
  assert.equal(allowed.permission.effect, "allow");
  assert.equal(allowed.permission.reasonCode, "test_allow");
  assert.equal(allowed.permission.requestDigest.length, 64);
  assert.ok(allowed.startedAt);
  assert.ok(allowed.completedAt);
  assert.equal(allowed.failure, null);
  assert.equal(allowed.metadata.handler_kind, "local");
  assert.equal(authorizedArguments.length, 1);
  assert.equal(authorizedArguments[0]?.target, "task-17-authorized");
  assert.equal(dispatch.get(allowed.invocationId)?.status, "completed");
  const replay = await dispatch.dispatch(allowRequest);
  assert.equal(replay.invocationId, allowed.invocationId);
  assert.equal(replay.completedAt, allowed.completedAt);
  assert.equal(permissionCalls, 1);
  assert.equal(authorizedArguments.length, 1);
  const askRequest = commandRequest("/dispatch-local task-18", 1, {
    metadata: { permission_effect: "ask" },
  });
  const asked = await dispatch.dispatch(askRequest);
  assert.equal(asked.status, "pending_approval");
  assert.equal(asked.permission.effect, "ask");
  assert.match(asked.permission.continuationId ?? "", /^continuation-/);
  assert.equal(asked.completedAt, null);
  assert.equal((asked.output as JsonObject).effect, "ask");
  assert.equal(authorizedArguments.length, 1);
  const sealedAskRequest = commandRequest("/dispatch-local task-19", 1, {
    interactive: false,
    sealedAutonomous: true,
    metadata: { permission_effect: "ask" },
  });
  const sealed = await dispatch.dispatch(sealedAskRequest);
  assert.equal(sealed.status, "denied");
  assert.equal(sealed.permission.effect, "deny");
  assert.equal(sealed.permission.reasonCode, "sealed_command_ask_denied");
  assert.equal(sealed.permission.continuationId, null);
  assert.equal(sealed.permission.replanRequired, true);
  assert.equal(sealed.permission.recoveryInput?.kind, "command_permission_denial");
  assert.equal(sealed.permission.recoveryInput?.e02_plans_or_routes, false);
  assert.ok(sealed.completedAt);
  const denyRequest = commandRequest("/dispatch-local task-20", 1, {
    metadata: { permission_effect: "deny" },
  });
  const denied = await dispatch.dispatch(denyRequest);
  assert.equal(denied.status, "denied");
  assert.equal(denied.permission.effect, "deny");
  assert.equal(denied.permission.replanRequired, true);
  assert.equal((denied.output as JsonObject).effect, "deny");
  const skillRequest = commandRequest("/dispatch-skill task-21 --verbose", 1, {
    metadata: { permission_effect: "allow" },
  });
  const skillResult = await dispatch.dispatch(skillRequest);
  assert.equal(skillResult.status, "completed");
  assert.equal(skillResult.metadata.handler_kind, "skill");
  assert.equal((skillResult.output as JsonObject).handler, "skill");
  assert.equal((skillResult.output as JsonObject).target, "task-21");
  assert.equal(dispatcherCalls.length, 1);
  assert.equal(dispatcherCalls[0]?.kind, "skill");
  const invalidDigestRequest = commandRequest("/dispatch-local task-22", 1, {
    metadata: {
      permission_effect: "allow",
      invalid_digest: true,
    },
  });
  await assert.rejects(dispatch.dispatch(invalidDigestRequest), /request digest mismatch/i);
  const history = new CommandHistoryRuntime({
    now,
    maximumRecords: 10,
  });
  const allowedRecord = history.record(allowRequest, allowed);
  assert.match(allowedRecord.historyId, /^command-history-record-/);
  assert.equal(allowedRecord.sequence, 1);
  assert.equal(allowedRecord.invocationId, allowed.invocationId);
  assert.equal(allowedRecord.commandId, allowed.commandId);
  assert.equal(allowedRecord.commandName, "dispatch-local");
  assert.equal(allowedRecord.sessionId, allowRequest.identity.sessionId);
  assert.equal(allowedRecord.sessionRevision, 4);
  assert.equal(allowedRecord.commandCallId, allowRequest.identity.commandCallId);
  assert.equal(allowedRecord.requestDigest, digest(allowRequest));
  assert.equal(allowedRecord.resultDigest, digest(allowed));
  assert.equal(allowedRecord.status, "completed");
  assert.equal(allowedRecord.permissionEffect, "allow");
  assert.equal(allowedRecord.metadata.registry_revision, 1);
  assert.equal(allowedRecord.metadata.decision_id, allowed.permission.decisionId);
  assert.equal(allowedRecord.previousHash, "sha256:zyra-command-history-genesis");
  assert.equal(allowedRecord.recordHash.length, 64);
  assert.equal(history.record(allowRequest, allowed).historyId, allowedRecord.historyId);
  assert.throws(() => history.record(allowRequest, {
    ...allowed,
    status: "failed",
  }), /result conflict/i);
  const deniedRecord = history.record(denyRequest, denied);
  assert.equal(deniedRecord.sequence, 2);
  assert.equal(deniedRecord.previousHash, allowedRecord.recordHash);
  assert.equal(history.get(allowed.invocationId)?.historyId, allowedRecord.historyId);
  assert.equal(history.get("missing-invocation"), null);
  assert.equal(history.query().length, 2);
  assert.equal(history.query({ status: "denied" }).length, 1);
  assert.equal(history.query({ commandName: "dispatch-local" }).length, 2);
  assert.equal(history.query({ sessionId: allowRequest.identity.sessionId }).length, 2);
  assert.deepEqual(history.query({ afterSequence: 1 }).map((record) => record.sequence), [2]);
  assert.equal(history.query({ limit: 1 }).length, 1);
  const snapshot = history.snapshot();
  assert.equal(snapshot.version, "zyra.command-history-runtime/v1");
  assert.equal(snapshot.sequence, 2);
  assert.equal(snapshot.headHash, deniedRecord.recordHash);
  assert.equal(snapshot.records.length, 2);
  assert.equal(snapshot.digest.length, 64);
  const restored = new CommandHistoryRuntime({ now, snapshot });
  assert.equal(restored.query().length, 2);
  assert.equal(restored.get(allowed.invocationId)?.recordHash, allowedRecord.recordHash);
  assert.equal(restored.get(denied.invocationId)?.recordHash, deniedRecord.recordHash);
  assert.throws(() => new CommandHistoryRuntime({
    snapshot: { ...snapshot, digest: "0".repeat(64) },
  }), /digest/i);
  const brokenRecords = snapshot.records.map((record) => ({ ...record }));
  brokenRecords[1]!.previousHash = "broken";
  const brokenPayload = {
    ...snapshot,
    records: brokenRecords,
  };
  const { digest: _snapshotDigest, ...withoutDigest } = brokenPayload;
  assert.throws(() => new CommandHistoryRuntime({
    snapshot: {
      ...withoutDigest,
      digest: digest(withoutDigest),
    },
  }), /chain is broken/i);
});
