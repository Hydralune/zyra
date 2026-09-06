import { createHash } from "node:crypto";
import { readdir, readFile, realpath, stat } from "node:fs/promises";
import { basename, dirname, extname, isAbsolute, relative, resolve } from "node:path";

import {
  asObject,
  asString,
  cloneJson,
  type JsonObject,
  type JsonValue,
  type RuntimeRunInput,
  type RuntimeRunResult,
  type ToolSpecContract,
} from "../contracts.ts";
import type { CommandDescriptor, SkillDescriptor, SkillExecutionResult } from "./contracts.ts";
import type { SkillToolScope } from "./contracts-v2.ts";
import { parseMarkdownDocument } from "./frontmatter.ts";
import { filterToolSpecsForSkillScope } from "./invocation-runtime.ts";

interface RuntimeRoot {
  path: string;
  kind: string;
  precedence: number;
}

export interface ForkedSkillExecutionInput {
  parentInput: RuntimeRunInput;
  childTaskId: string;
  workerRequestId: string;
  invocationId: string;
  skillId: string;
  skillName: string;
  descriptorDigest: string;
  renderedBody: string;
  skillContext: JsonObject;
  skillArguments: JsonObject;
  skillResources: JsonValue;
  effectiveToolScope: SkillToolScope;
  maximumTurns: number;
  timeoutMs: number;
  skillAncestry?: string[];
  remainingSkillDepth?: number;
  sandbox: string;
  allowNetwork: boolean;
}

const SKILL_TOOLS = new Set([
  "list_skills",
  "skill",
  "read_skill_resource",
  "list_commands",
  "command",
  "list_plugins",
  "plugin_command",
]);

// Keep the no-parent-override fallback aligned with the provider runtime's
// ordinary request envelope. A fork still caps this value to its own active
// execution window; this is not a replacement for the skill deadline.
const DEFAULT_FORKED_SKILL_MODEL_TIMEOUT_MILLISECONDS = 120_000;

export class TypeScriptSkillRuntime {
  static assertSourceRuntimeEnabled(): void {
    if (process.env.ZYRA_DISABLE_E04_SKILL_SOURCE_RUNTIME === "1") {
      throw Object.assign(
        new Error(
          "The migrated TypeScript skill/plugin/command source runtime is disabled; no legacy or Python fallback is permitted",
        ),
        {
          name: "SkillSourceRuntimeDisabledError",
          code: "e04_skill_source_runtime_disabled",
        },
      );
    }
  }

  static executeForkedSkill(
    input: ForkedSkillExecutionInput,
    runChild: (input: RuntimeRunInput) => Promise<RuntimeRunResult>,
    signal?: AbortSignal,
  ): Promise<RuntimeRunResult> {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    if (signal?.aborted) {
      throw signal.reason instanceof Error
        ? signal.reason
        : new Error("forked skill execution was aborted");
    }
    const parent = input.parentInput;
    const parentConstraints = asObject(parent.config.runtimeConstraints);
    const childBudget = forkedSkillResourceBudget(parentConstraints, input.timeoutMs);
    const childInput: RuntimeRunInput = {
      ...parent,
      taskId: input.childTaskId,
      // Checkpoints are keyed by physical session identity. A fork therefore
      // needs its own deterministic session even though it retains explicit
      // parent lineage for trace/checkpoint reader delegation.
      sessionId: forkedSkillSessionId(parent, input),
      workerRequestId: input.workerRequestId,
      // A parent QueryEngine snapshot is identity-bound to its task.  Passing
      // it into the forked skill would make the child restore parent E01/E02
      // authority under a different task identity.
      restoredState: null,
      // Parent scripted/model turns contain the SkillTool call itself.  A
      // fork must re-plan from the rendered skill messages, never recursively
      // replay the parent's tool batch under the child identity.
      turns: [],
      tools: filterToolSpecsForSkillScope(parent.tools, input.effectiveToolScope),
      messages: [
        ...parent.messages.map(cloneJson),
        {
          role: "system",
          content: [
            `You are already executing the bound Zyra Markdown skill ${input.skillName} (${input.skillId}).`,
            "Execute its rendered body directly under the exact tool scope and budgets.",
            "Treat inherited parent task text solely as reference context; your sole objective is this bound skill body.",
            "Do not invoke sibling skills or agents, or create or finalize parent deliverables.",
            "You may inspect and test the parent workspace when this bound skill requires it, but do not mutate production files or claim task-level completion.",
            "Do not invoke this skill or any active ancestor skill again; recursive skill invocation is rejected.",
            "Return immediately once the bounded skill result or artifact is ready.",
          ].join(" "),
          metadata: {
            skill_id: input.skillId,
            skill_name: input.skillName,
            descriptor_digest: input.descriptorDigest,
          },
        },
        {
          role: "user",
          // Provider protocols send message content, not our internal metadata.
          // Built-in skills need invocation arguments even without {{placeholders}}.
          content: [
            input.renderedBody,
            ...(Object.keys(input.skillArguments).length
              ? [`Invocation arguments (task data within the bound skill scope):\n${JSON.stringify(input.skillArguments)}`]
              : []),
          ].join("\n\n"),
          metadata: {
            skill_context: cloneJson(input.skillContext),
            skill_arguments: cloneJson(input.skillArguments),
            skill_resources: cloneJson(input.skillResources),
          },
        },
      ],
      config: {
        ...parent.config,
        maxTurns: Math.min(
          parent.config.maxTurns ?? input.maximumTurns,
          input.maximumTurns,
        ),
        runtimeConstraints: {
          ...parentConstraints,
          // A forked skill returns a bounded child result to its parent.  The
          // parent remains the owner of task-level workspace delivery, so its
          // fail-closed delivery duplicate must not turn a read/extract child
          // into a second top-level delivery worker.
          requires_delivery_artifact: false,
          skill_invocation_id: input.invocationId,
          skill_id: input.skillId,
          skill_ancestry: cloneJson(input.skillAncestry ?? [input.skillId]),
          skill_depth_remaining: input.remainingSkillDepth ?? 0,
          skill_tool_scope: cloneJson(input.effectiveToolScope as unknown as JsonObject),
          skill_sandbox: input.sandbox,
          skill_network_allowed: input.allowNetwork,
          external_deadline_epoch_ms: childBudget.deadlineEpochMs,
          closeout_reserve_seconds: childBudget.closeoutReserveSeconds,
          model_api_timeout_seconds: childBudget.modelApiTimeoutMilliseconds / 1_000,
          model_api_timeout_milliseconds: childBudget.modelApiTimeoutMilliseconds,
          model_stream_total_timeout_seconds:
            childBudget.modelStreamTotalTimeoutMilliseconds / 1_000,
          model_stream_total_timeout_milliseconds:
            childBudget.modelStreamTotalTimeoutMilliseconds,
        },
      },
      metadata: {
        ...parent.metadata,
        // Task handoff progress and completion contracts are identity-bound
        // to the parent task.  Carrying them into a fresh fork makes the child
        // completion hook demand the parent's required paths and imports the
        // parent's exhausted inspection/stall counters despite restoredState
        // being intentionally cleared above.
        delivery_contract: {},
        task_handoff_progress: {},
        task_handoff_semantic_stall: {},
        e02_skill_invocation: true,
        skill_invocation_id: input.invocationId,
        parent_task_id: parent.taskId,
        runtime_lineage: {
          schema: "zyra.runtime-lineage/v1",
          relation: "skill",
          relation_id: input.invocationId,
          parent_run_id: parent.runId,
          parent_task_id: parent.taskId,
          parent_session_id: parent.sessionId,
          parent_worker_request_id: parent.workerRequestId,
        },
      },
    };
    return runChild(childInput);
  }

  private readonly roots: RuntimeRoot[];
  private readonly skills = new Map<string, SkillDescriptor>();
  private readonly commands = new Map<string, CommandDescriptor>();
  private opened = false;

  constructor(roots: RuntimeRoot[]) {
    this.roots = roots;
  }

  async open(): Promise<void> {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    if (this.opened) {
      return;
    }
    for (const root of [...this.roots].sort((left, right) => left.precedence - right.precedence)) {
      await this.scanRoot(root);
    }
    this.opened = true;
  }

  owns(toolName: string): boolean {
    return SKILL_TOOLS.has(toolName);
  }

  toolSpecs(): ToolSpecContract[] {
    const output = { type: "object" } as JsonObject;
    const common = {
      source: "typescript-skill-runtime",
      output_schema: output,
      execution_provenance: {
        namespace: "skill",
        server_id: "",
        version: "1",
        source: "typescript-skill-runtime",
      } as JsonObject,
    };
    return [
      {
        ...common,
        name: "list_skills",
        purpose: "List skills discovered by the TypeScript CodeWorker runtime",
        input_schema: { type: "object" },
        metadata: { access_mode: "read", canonical_runtime_owner: "typescript" },
      },
      {
        ...common,
        name: "skill",
        purpose: "Load and invoke a Markdown skill in the active CodeWorker session",
        input_schema: {
          type: "object",
          required: ["name"],
          properties: {
            name: { type: "string" },
            skill: { type: "string" },
            arguments: { type: "object" },
          },
        },
        metadata: { access_mode: "execute", canonical_runtime_owner: "typescript" },
      },
      {
        ...common,
        name: "read_skill_resource",
        purpose: "Read a resource contained by a selected skill directory",
        input_schema: {
          type: "object",
          required: ["name", "path"],
          properties: { name: { type: "string" }, path: { type: "string" } },
        },
        metadata: { access_mode: "read", canonical_runtime_owner: "typescript" },
      },
      {
        ...common,
        name: "list_commands",
        purpose: "List Markdown commands and plugin commands available to CodeWorker",
        input_schema: { type: "object" },
        metadata: { access_mode: "read", canonical_runtime_owner: "typescript" },
      },
      {
        ...common,
        name: "command",
        purpose: "Load a Markdown command into the active CodeWorker query loop",
        input_schema: {
          type: "object",
          required: ["name"],
          properties: { name: { type: "string" }, arguments: { type: "object" } },
        },
        metadata: { access_mode: "execute", canonical_runtime_owner: "typescript" },
      },
      {
        ...common,
        name: "list_plugins",
        purpose: "List plugin namespaces that contribute commands",
        input_schema: { type: "object" },
        metadata: { access_mode: "read", canonical_runtime_owner: "typescript" },
      },
      {
        ...common,
        name: "plugin_command",
        purpose: "Load a namespaced plugin command into the active CodeWorker query loop",
        input_schema: {
          type: "object",
          required: ["name"],
          properties: { name: { type: "string" }, arguments: { type: "object" } },
        },
        metadata: { access_mode: "execute", canonical_runtime_owner: "typescript" },
      },
    ];
  }

  async execute(toolName: string, argumentsValue: JsonObject): Promise<SkillExecutionResult> {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    if (toolName === "list_skills") {
      return this.listSkills();
    }
    if (toolName === "skill") {
      return this.invokeSkill(argumentsValue);
    }
    if (toolName === "read_skill_resource") {
      return this.readSkillResource(argumentsValue);
    }
    if (toolName === "list_commands") {
      return this.listCommands();
    }
    if (toolName === "list_plugins") {
      return this.listPlugins();
    }
    if (toolName === "command" || toolName === "plugin_command") {
      return this.invokeCommand(argumentsValue);
    }
    throw new Error(`TypeScript skill runtime does not own ${toolName}`);
  }

  snapshot(): JsonObject {
    const skills = [...this.skills.values()].map((item) => ({
      name: item.name,
      digest: item.contentDigest,
      root_kind: item.rootKind,
      precedence: item.precedence,
    }));
    const commands = [...this.commands.values()].map((item) => ({
      name: item.name,
      digest: item.contentDigest,
      namespace: item.namespace,
    }));
    return {
      version: "zyra.typescript-skill-runtime.v1",
      canonical_owner: "typescript",
      skills,
      commands,
      catalog_digest: digest(JSON.stringify({ skills, commands })),
      roots: this.roots.map((root) => ({ path: root.path, kind: root.kind })),
    };
  }

  private async scanRoot(root: RuntimeRoot): Promise<void> {
    let rootReal: string;
    try {
      rootReal = await realpath(root.path);
    } catch {
      return;
    }
    await this.walk(rootReal, root, rootReal, 0);
  }

  private async walk(directory: string, root: RuntimeRoot, rootReal: string, depth: number): Promise<void> {
    if (depth > 8) {
      return;
    }
    let entries;
    try {
      entries = await readdir(directory, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
      if (entry.name.startsWith(".") && ![".claude", ".zyra"].includes(entry.name)) {
        continue;
      }
      const candidate = resolve(directory, entry.name);
      if (entry.isSymbolicLink()) {
        continue;
      }
      if (entry.isDirectory()) {
        await this.walk(candidate, root, rootReal, depth + 1);
        continue;
      }
      if (!entry.isFile() || extname(entry.name).toLowerCase() !== ".md") {
        continue;
      }
      const safePath = await containedRealPath(rootReal, candidate);
      if (!safePath) {
        continue;
      }
      if (entry.name.toLowerCase() === "skill.md") {
        await this.registerSkill(safePath, root);
      } else if (isCommandPath(safePath)) {
        await this.registerCommand(safePath, root);
      }
    }
  }

  private async registerSkill(path: string, root: RuntimeRoot): Promise<void> {
    const document = parseMarkdownDocument(await readFile(path, "utf8"));
    const directoryName = basename(dirname(path));
    const name = asString(document.attributes.name) || directoryName;
    if (!validName(name) || this.skills.has(name)) {
      return;
    }
    const allowedRaw = document.attributes["allowed-tools"] ?? document.attributes.allowed_tools;
    const allowedTools = Array.isArray(allowedRaw)
      ? allowedRaw.filter((item): item is string => typeof item === "string")
      : asString(allowedRaw).split(/[ ,]+/).filter(Boolean);
    this.skills.set(name, {
      name,
      description: asString(document.attributes.description) || firstLine(document.body),
      path,
      root: root.path,
      rootKind: root.kind,
      precedence: root.precedence,
      allowedTools,
      contentDigest: document.digest,
      metadata: {
        canonical_runtime_owner: "typescript",
        source: asString(document.attributes.source) || root.kind,
      },
    });
  }

  private async registerCommand(path: string, root: RuntimeRoot): Promise<void> {
    const document = parseMarkdownDocument(await readFile(path, "utf8"));
    const relativeName = relative(root.path, path).replaceAll("\\", "/").replace(/\.md$/i, "");
    const namespace = pluginNamespace(relativeName);
    const name = asString(document.attributes.name) || relativeName
      .replace(/^.*(?:commands|command)\//, "")
      .replaceAll("/", ":");
    if (!validCommandName(name) || this.commands.has(name)) {
      return;
    }
    this.commands.set(name, {
      name,
      description: asString(document.attributes.description) || firstLine(document.body),
      path,
      root: root.path,
      namespace,
      contentDigest: document.digest,
      metadata: {
        canonical_runtime_owner: "typescript",
        source: root.kind,
      },
    });
  }

  private listSkills(): SkillExecutionResult {
    const skills = [...this.skills.values()].sort((left, right) => left.name.localeCompare(right.name));
    return {
      summary: `Listed ${skills.length} TypeScript-owned skills`,
      output: {
        skills: skills.map((skill) => ({
          name: skill.name,
          description: skill.description,
          allowed_tools: skill.allowedTools,
          source: skill.rootKind,
          digest: skill.contentDigest,
        })),
      },
      metadata: ownerMetadata("typescript-skill"),
    };
  }

  private async invokeSkill(argumentsValue: JsonObject): Promise<SkillExecutionResult> {
    const name = asString(argumentsValue.name) || asString(argumentsValue.skill);
    const descriptor = this.skills.get(name);
    if (!descriptor) {
      throw new Error(`Unknown TypeScript skill: ${name}`);
    }
    const document = parseMarkdownDocument(await readFile(descriptor.path, "utf8"));
    return {
      summary: `Loaded skill ${name} into the active query loop`,
      output: {
        name,
        instructions: document.body,
        arguments: asObject(argumentsValue.arguments),
        allowed_tools: descriptor.allowedTools,
        content_digest: descriptor.contentDigest,
        provenance: descriptor.metadata,
      },
      metadata: {
        ...ownerMetadata("typescript-skill"),
        skill_name: name,
        skill_digest: descriptor.contentDigest,
      },
    };
  }

  private async readSkillResource(argumentsValue: JsonObject): Promise<SkillExecutionResult> {
    const name = asString(argumentsValue.name) || asString(argumentsValue.skill);
    const descriptor = this.skills.get(name);
    if (!descriptor) {
      throw new Error(`Unknown TypeScript skill: ${name}`);
    }
    const rawPath = asString(argumentsValue.path);
    if (!rawPath || isAbsolute(rawPath)) {
      throw new Error("Skill resource path must be a non-empty relative path");
    }
    const skillDirectory = dirname(descriptor.path);
    const candidate = await containedRealPath(skillDirectory, resolve(skillDirectory, rawPath));
    if (!candidate) {
      throw new Error("Skill resource escapes its owning skill directory");
    }
    const info = await stat(candidate);
    if (!info.isFile() || info.size > 1_000_000) {
      throw new Error("Skill resource must be a file smaller than 1 MB");
    }
    const content = await readFile(candidate, "utf8");
    return {
      summary: `Read resource ${rawPath} from skill ${name}`,
      output: { name, path: rawPath, content, content_digest: digest(content) },
      metadata: { ...ownerMetadata("typescript-skill"), skill_name: name },
    };
  }

  private listCommands(): SkillExecutionResult {
    const commands = [...this.commands.values()].sort((left, right) => left.name.localeCompare(right.name));
    return {
      summary: `Listed ${commands.length} TypeScript-owned commands`,
      output: {
        commands: commands.map((command) => ({
          name: command.name,
          description: command.description,
          namespace: command.namespace,
          digest: command.contentDigest,
        })),
      },
      metadata: ownerMetadata("typescript-command"),
    };
  }

  private listPlugins(): SkillExecutionResult {
    const plugins = [...new Set([...this.commands.values()].map((item) => item.namespace).filter(Boolean))]
      .sort();
    return {
      summary: `Listed ${plugins.length} command plugin namespaces`,
      output: { plugins },
      metadata: ownerMetadata("typescript-plugin"),
    };
  }

  private async invokeCommand(argumentsValue: JsonObject): Promise<SkillExecutionResult> {
    const name = asString(argumentsValue.name);
    const descriptor = this.commands.get(name);
    if (!descriptor) {
      throw new Error(`Unknown TypeScript command: ${name}`);
    }
    const document = parseMarkdownDocument(await readFile(descriptor.path, "utf8"));
    return {
      summary: `Loaded command ${name} into the active query loop`,
      output: {
        name,
        namespace: descriptor.namespace,
        instructions: document.body,
        arguments: asObject(argumentsValue.arguments),
        content_digest: descriptor.contentDigest,
      },
      metadata: {
        ...ownerMetadata(descriptor.namespace ? "typescript-plugin" : "typescript-command"),
        command_name: name,
        command_digest: descriptor.contentDigest,
      },
    };
  }
}

function forkedSkillSessionId(
  parent: RuntimeRunInput,
  input: ForkedSkillExecutionInput,
): string {
  const identity = [
    parent.runId,
    parent.taskId,
    parent.sessionId,
    input.childTaskId,
    input.invocationId,
  ].join("\0");
  return `skill-session-${createHash("sha256").update(identity).digest("hex").slice(0, 32)}`;
}

export interface ForkedSkillResourceBudget {
  deadlineEpochMs: number;
  closeoutReserveSeconds: number;
  modelApiTimeoutMilliseconds: number;
  modelStreamTotalTimeoutMilliseconds: number;
}

export function forkedSkillResourceBudget(
  parentConstraints: JsonObject,
  timeoutMs: number,
  nowEpochMs = Date.now(),
): ForkedSkillResourceBudget {
  const boundedTimeoutMs = Math.max(1, Math.floor(timeoutMs));
  let deadlineEpochMs = nowEpochMs + boundedTimeoutMs;
  const parentDeadline = Number(parentConstraints.external_deadline_epoch_ms);
  const parentReserveSeconds = Number(parentConstraints.closeout_reserve_seconds);
  if (
    Number.isFinite(parentDeadline)
    && parentDeadline > 0
    && Number.isFinite(parentReserveSeconds)
    && parentReserveSeconds > 0
  ) {
    deadlineEpochMs = Math.min(
      deadlineEpochMs,
      Math.floor(parentDeadline - parentReserveSeconds * 1_000),
    );
  } else if (Number.isFinite(parentDeadline) && parentDeadline > 0) {
    deadlineEpochMs = Math.min(deadlineEpochMs, Math.floor(parentDeadline));
  }
  const availableMs = deadlineEpochMs - nowEpochMs;
  if (availableMs <= 1_000) {
    throw Object.assign(
      new Error("forked skill cannot start because the parent closeout window is active"),
      { name: "SkillParentCloseoutError", code: "skill_parent_closeout_active" },
    );
  }
  const closeoutReserveMs = Math.max(
    1_000,
    Math.min(60_000, Math.floor(availableMs * 0.2), Math.floor(availableMs / 2)),
  );
  const activeExecutionMs = Math.max(100, availableMs - closeoutReserveMs);
  const configuredModelTimeoutMs = positiveMilliseconds(
    parentConstraints.model_api_timeout_milliseconds,
    Number(parentConstraints.model_api_timeout_seconds) * 1_000,
    DEFAULT_FORKED_SKILL_MODEL_TIMEOUT_MILLISECONDS,
  );
  const configuredStreamTotalTimeoutMs = positiveMilliseconds(
    parentConstraints.model_stream_total_timeout_milliseconds,
    Number(parentConstraints.model_stream_total_timeout_seconds) * 1_000,
    configuredModelTimeoutMs,
  );
  return {
    deadlineEpochMs,
    closeoutReserveSeconds: closeoutReserveMs / 1_000,
    modelApiTimeoutMilliseconds: Math.max(
      100,
      Math.min(configuredModelTimeoutMs, activeExecutionMs),
    ),
    modelStreamTotalTimeoutMilliseconds: Math.max(
      100,
      Math.min(configuredStreamTotalTimeoutMs, activeExecutionMs),
    ),
  };
}

function positiveMilliseconds(
  primary: unknown,
  secondary: unknown,
  fallback: number,
): number {
  const selected = Number(primary) || Number(secondary) || fallback;
  return Math.max(100, Math.floor(selected));
}

export function parseSkillRoots(value: unknown, defaults: string[]): RuntimeRoot[] {
  const result: RuntimeRoot[] = [];
  const seen = new Set<string>();
  const items = Array.isArray(value) ? value : defaults;
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    const object = asObject(item);
    const path = typeof item === "string" ? item : asString(object.path);
    if (!path) {
      continue;
    }
    const resolved = resolve(path);
    if (seen.has(resolved)) {
      continue;
    }
    seen.add(resolved);
    result.push({
      path: resolved,
      kind: typeof item === "string" ? `root-${index}` : asString(object.kind) || `root-${index}`,
      precedence: typeof object.precedence === "number" ? object.precedence : index,
    });
  }
  return result;
}

export function isLegacySkillTool(tool: ToolSpecContract): boolean {
  if (SKILL_TOOLS.has(tool.name)) {
    return true;
  }
  const provenance = asObject(tool.execution_provenance);
  return asString(provenance.namespace) === "skill"
    || tool.source.includes("skill-projection")
    || tool.source.includes("python-skill");
}

async function containedRealPath(root: string, candidate: string): Promise<string | null> {
  try {
    const [rootReal, candidateReal] = await Promise.all([realpath(root), realpath(candidate)]);
    const delta = relative(rootReal, candidateReal);
    if (delta === "" || (!delta.startsWith("..") && !isAbsolute(delta))) {
      return candidateReal;
    }
  } catch {
    return null;
  }
  return null;
}

function isCommandPath(path: string): boolean {
  const normalized = path.replaceAll("\\", "/").toLowerCase();
  return normalized.includes("/commands/") || normalized.includes("/command/");
}

function pluginNamespace(path: string): string {
  const match = path.replaceAll("\\", "/").match(/(?:plugins|plugin)\/([^/]+)\/(?:commands|command)\//i);
  return match?.[1] ?? "";
}

function validName(value: string): boolean {
  return /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/.test(value);
}

function validCommandName(value: string): boolean {
  return /^[a-zA-Z0-9][a-zA-Z0-9_:/.-]{0,191}$/.test(value) && !value.includes("..");
}

function firstLine(value: string): string {
  return value.split("\n").map((line) => line.trim()).find(Boolean)?.slice(0, 240) ?? "";
}

function digest(value: string): string {
  return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

function ownerMetadata(owner: string): Record<string, string> {
  return {
    canonical_runtime_owner: "typescript",
    capability_owner: owner,
    python_projection_used: "false",
  };
}
