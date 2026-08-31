import { existsSync } from "node:fs";
import {
  readdir,
  realpath,
  stat,
} from "node:fs/promises";
import {
  extname,
  isAbsolute,
  relative,
  resolve,
  sep,
} from "node:path";

import type {
  JsonObject,
  JsonValue,
  ToolSpecContract,
} from "../contracts.ts";
import {
  canonicalize,
  cloneJson,
  deterministicId,
  digest,
  monotonicNow,
} from "../e02/index.ts";
import type { PluginManifest } from "../plugins/contracts.ts";
import { TypeScriptSkillRuntime } from "../skills/runtime.ts";
import {
  CommandCompletionRuntime,
  type CommandCompletionRequest,
} from "./completion-runtime.ts";
import type {
  CommandDescriptor,
  CommandInvocationIdentity,
  CommandInvocationRequest,
  CommandInvocationResult,
  CommandPermissionDecision,
  CommandRegistrySnapshot,
  CommandSourceKind,
} from "./contracts.ts";
import { CommandDescriptorRuntime } from "./descriptor-runtime.ts";
import {
  CommandDispatchRuntime,
  type CommandExternalDispatchers,
  type CommandPermissionEvaluator,
} from "./dispatch-runtime.ts";
import { CommandHelpRuntime } from "./help-runtime.ts";
import {
  CommandHistoryRuntime,
  type CommandHistorySnapshot,
} from "./history-runtime.ts";
import {
  LocalCommandRuntime,
  type LocalCommandHandler,
} from "./local-command-runtime.ts";
import { CommandRegistryRuntime } from "./registry-runtime.ts";

export interface CommandSourceRoot {
  rootId: string;
  rootPath: string;
  sourceKind: CommandSourceKind;
  sourcePriority: number;
  required: boolean;
  recursive: boolean;
  maximumDepth: number;
  extensions: string[];
  enabled: boolean;
  metadata: JsonObject;
}

export interface CommandSourceFile {
  sourceFileId: string;
  rootId: string;
  rootPath: string;
  path: string;
  realPath: string;
  relativePath: string;
  sourceKind: CommandSourceKind;
  sourcePriority: number;
  modifiedAtMs: number;
  sizeBytes: number;
  contentIdentity: string;
  metadata: JsonObject;
}

export interface CommandLoadFailure {
  failureId: string;
  rootId: string;
  path: string;
  code: string;
  message: string;
  fatal: boolean;
  occurredAt: string;
  metadata: JsonObject;
}

export interface CommandReloadReceipt {
  receiptId: string;
  sourceRevisionBefore: number;
  sourceRevisionAfter: number;
  registryRevisionBefore: number;
  registryRevisionAfter: number;
  descriptorCount: number;
  sourceFileCount: number;
  added: string[];
  updated: string[];
  removed: string[];
  shadowed: string[];
  conflicts: JsonObject[];
  failures: CommandLoadFailure[];
  committedAt: string;
  digest: string;
  metadata: JsonObject;
}

export interface CommandCoordinatorIdentity {
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  interactive: boolean;
  sealedAutonomous: boolean;
}

export interface CommandCoordinatorExecution {
  summary: string;
  output: JsonObject;
  metadata: Record<string, string>;
}

export interface CommandCoordinatorSnapshot {
  version: "zyra.command-coordinator/v1";
  workspaceRoot: string;
  opened: boolean;
  restoredBeforeBootstrap: boolean;
  sourceRevision: number;
  roots: CommandSourceRoot[];
  sourceFiles: CommandSourceFile[];
  fileDescriptors: Array<[string, CommandDescriptor]>;
  pluginDescriptors: Array<[string, CommandDescriptor[]]>;
  builtinDescriptors: CommandDescriptor[];
  reloadReceipts: CommandReloadReceipt[];
  failures: CommandLoadFailure[];
  registry: CommandRegistrySnapshot;
  history: CommandHistorySnapshot;
  digest: string;
  capturedAt: string;
}

export interface CommandCoordinatorOptions {
  workspaceRoot: string;
  roots: CommandSourceRoot[];
  permission: CommandPermissionEvaluator;
  dispatchers: CommandExternalDispatchers;
  builtins?: CommandDescriptor[];
  localHandlers?: Record<string, LocalCommandHandler>;
  now?: () => Date;
  snapshot?: CommandCoordinatorSnapshot | null;
}

export class CommandCoordinator {
  readonly descriptor = new CommandDescriptorRuntime();
  readonly registry: CommandRegistryRuntime;
  readonly local: LocalCommandRuntime;
  readonly dispatch: CommandDispatchRuntime;
  readonly history: CommandHistoryRuntime;
  readonly help: CommandHelpRuntime;
  readonly completion: CommandCompletionRuntime;
  private readonly workspaceRoot: string;
  private readonly roots: CommandSourceRoot[];
  private readonly now: () => Date;
  private readonly sourceFiles = new Map<string, CommandSourceFile>();
  private readonly fileDescriptors = new Map<string, CommandDescriptor>();
  private readonly pluginDescriptors = new Map<string, CommandDescriptor[]>();
  private readonly builtinDescriptors: CommandDescriptor[];
  private readonly reloadReceipts = new Map<string, CommandReloadReceipt>();
  private readonly failures = new Map<string, CommandLoadFailure>();
  private opened = false;
  private restoredBeforeBootstrap = false;
  private sourceRevision = 0;
  private reloadPromise: Promise<CommandReloadReceipt> | null = null;
  private lastTimestamp: string | null = null;

  constructor(options: CommandCoordinatorOptions) {
    if (!options.workspaceRoot) {
      throw commandCoordinatorError(
        "command_workspace_missing",
        "command coordinator requires a workspace root",
      );
    }
    this.workspaceRoot = resolve(options.workspaceRoot);
    this.roots = options.roots
      .map((root) => normalizeRoot(root, this.workspaceRoot))
      .sort(compareRoots);
    this.now = options.now ?? (() => new Date());
    const snapshot = options.snapshot ?? null;
    if (snapshot) {
      this.validateSnapshot(snapshot);
    }
    this.registry = new CommandRegistryRuntime({
      now: this.now,
      snapshot: snapshot?.registry ?? null,
    });
    this.local = new LocalCommandRuntime({
      now: this.now,
    });
    for (const [handlerId, handler] of Object.entries(options.localHandlers ?? {})) {
      this.local.register(handlerId, handler);
    }
    this.dispatch = new CommandDispatchRuntime({
      registry: this.registry,
      local: this.local,
      permission: options.permission,
      dispatchers: options.dispatchers,
      now: this.now,
    });
    this.history = new CommandHistoryRuntime({
      now: this.now,
      snapshot: snapshot?.history ?? null,
    });
    this.help = new CommandHelpRuntime(this.registry);
    this.completion = new CommandCompletionRuntime(this.registry);
    this.builtinDescriptors = (options.builtins ?? snapshot?.builtinDescriptors ?? [])
      .map(validateDescriptor);
    if (snapshot) {
      this.restoreLocal(snapshot);
      this.restoredBeforeBootstrap = true;
    }
  }

  async open(): Promise<void> {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    if (this.opened) {
      return;
    }
    if (!this.restoredBeforeBootstrap || this.registry.currentRevision === 0) {
      await this.reload({
        reason: "bootstrap",
      });
    }
    this.opened = true;
  }

  owns(toolName: string): boolean {
    return [
      "list_commands",
      "command",
      "command_help",
      "complete_command",
      "reload_commands",
      "command_history",
    ].includes(toolName);
  }

  toolSpecs(): ToolSpecContract[] {
    return [
      commandTool(
        "list_commands",
        "List TypeScript-owned command descriptors",
        {
          category: { type: "string" },
          include_hidden: { type: "boolean" },
        },
        "read",
      ),
      commandTool(
        "command_help",
        "Render help for a TypeScript-owned command",
        {
          command: { type: "string" },
        },
        "read",
        ["command"],
      ),
      commandTool(
        "complete_command",
        "Complete a TypeScript-owned command line",
        {
          input: { type: "string" },
          cursor: { type: "integer" },
          maximum_results: { type: "integer" },
        },
        "read",
        ["input"],
      ),
      commandTool(
        "command",
        "Invoke a Zyra slash command, not an operating-system command. Use the shell tool for executables or shell syntax.",
        {
          input: { type: "string", description: "Complete slash-command line beginning with '/', for example /skills list." },
          command: { type: "string", description: "Registered slash-command name, with or without the leading '/'." },
          arguments: { type: "array", items: { type: "string" }, description: "Slash-command arguments; never shell argv." },
          options: { type: "object", description: "Structured options accepted by the selected slash command." },
        },
        "execute",
      ),
      commandTool(
        "reload_commands",
        "Atomically rescan and commit disk and plugin command revisions",
        {},
        "execute",
      ),
      commandTool(
        "command_history",
        "Query durable TypeScript command invocation history",
        {
          command: { type: "string" },
          status: { type: "string" },
          limit: { type: "integer" },
        },
        "read",
      ),
    ];
  }

  async execute(
    toolName: string,
    argumentsValue: JsonObject,
    identityValue: Partial<CommandCoordinatorIdentity> = {},
    signal?: AbortSignal,
  ): Promise<CommandCoordinatorExecution> {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    this.requireOpen();
    if (toolName === "list_commands") {
      const documents = this.help.index({
        category: optionalString(argumentsValue, "category") || undefined,
        includeHidden: argumentsValue.include_hidden === true,
      });
      return commandExecution(
        `Listed ${this.registry.list().length} commands`,
        {
          ...documents,
          registry_revision: this.registry.currentRevision,
        },
      );
    }
    if (toolName === "command_help") {
      const help = this.help.render(
        requiredString(argumentsValue, "command"),
      );
      return commandExecution(
        `Rendered help for ${help.commandName}`,
        canonicalize(help) as JsonObject,
      );
    }
    if (toolName === "complete_command") {
      const input = requiredString(argumentsValue, "input");
      const request: CommandCompletionRequest = {
        input,
        cursor: integer(argumentsValue.cursor, input.length),
        registryRevision: this.registry.currentRevision,
        maximumResults: integer(argumentsValue.maximum_results, 50),
        includeHidden: argumentsValue.include_hidden === true,
      };
      const result = this.completion.complete(request);
      return commandExecution(
        `Completed command input with ${result.items.length} candidates`,
        canonicalize(result) as JsonObject,
      );
    }
    if (toolName === "reload_commands") {
      const receipt = await this.reload({
        reason: "tool_request",
      });
      return commandExecution(
        `Reloaded ${receipt.descriptorCount} commands`,
        canonicalize(receipt) as JsonObject,
      );
    }
    if (toolName === "command_history") {
      const records = this.history.query({
        sessionId: optionalString(argumentsValue, "session_id") || undefined,
        commandName: optionalString(argumentsValue, "command") || undefined,
        status: commandStatus(argumentsValue.status),
        afterSequence: integer(argumentsValue.after_sequence, 0),
        limit: integer(argumentsValue.limit, 100),
      });
      return commandExecution(
        `Read ${records.length} command history records`,
        {
          records: canonicalize(records),
          history_digest: this.history.snapshot().digest,
        },
      );
    }
    if (toolName !== "command") {
      throw commandCoordinatorError(
        "command_tool_not_owned",
        `command coordinator does not own ${toolName}`,
      );
    }
    const identity = this.identity(identityValue, argumentsValue);
    const request: CommandInvocationRequest = {
      identity,
      input: optionalString(argumentsValue, "input"),
      commandName: optionalString(argumentsValue, "command") || null,
      arguments: arrayValue(argumentsValue.arguments),
      options: objectValue(argumentsValue.options),
      registryRevision: this.registry.currentRevision,
      workspaceRoot: this.workspaceRoot,
      interactive: identityValue.interactive !== false,
      sealedAutonomous: identityValue.sealedAutonomous === true,
      metadata: {
        tool_call_id: identity.commandCallId,
      },
    };
    if (!request.input && !request.commandName) {
      throw commandCoordinatorError(
        "command_input_missing",
        "command execution requires input or command name",
      );
    }
    const result = await this.dispatch.dispatch(request, signal);
    this.history.record(request, result);
    return commandExecution(
      `Command ${result.commandName} ${result.status}`,
      {
        invocation: canonicalize(result),
        registry_revision: this.registry.currentRevision,
      },
      {
        command_name: result.commandName,
        command_status: result.status,
        command_invocation_id: result.invocationId,
      },
    );
  }

  async replacePlugin(
    manifest: PluginManifest,
    metadata: JsonObject = {},
  ): Promise<CommandReloadReceipt> {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    const descriptors: CommandDescriptor[] = [];
    for (const command of manifest.commands) {
      const descriptor = await this.descriptor.parse({
        path: command.path,
        sourceKind: "plugin",
        sourceId: manifest.pluginId,
        sourcePriority: sourcePriority(manifest.sourceKind) + 50,
        pluginId: manifest.pluginId,
        overrides: {
          name: command.name,
          aliases: command.aliases,
          hidden: command.hidden,
          ...(command.permission ? { permission: command.permission } : {}),
          metadata: {
            ...command.metadata,
            plugin_manifest_digest: manifest.manifestDigest,
            plugin_version: manifest.version,
          },
        },
      });
      descriptors.push(descriptor);
    }
    this.pluginDescriptors.set(manifest.pluginId, descriptors);
    return this.commitRegistry({
      ...metadata,
      reason: "plugin_commands_replaced",
      plugin_id: manifest.pluginId,
      plugin_manifest_digest: manifest.manifestDigest,
    });
  }

  async removePlugin(
    pluginId: string,
    metadata: JsonObject = {},
  ): Promise<CommandReloadReceipt> {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    this.pluginDescriptors.delete(pluginId);
    return this.commitRegistry({
      ...metadata,
      reason: "plugin_commands_removed",
      plugin_id: pluginId,
    });
  }

  registerLocal(
    handlerId: string,
    handler: LocalCommandHandler,
    replace = false,
  ): void {
    this.local.register(handlerId, handler, replace);
  }

  snapshot(): CommandCoordinatorSnapshot {
    const withoutDigest = {
      version: "zyra.command-coordinator/v1" as const,
      workspaceRoot: this.workspaceRoot,
      opened: this.opened,
      restoredBeforeBootstrap: this.restoredBeforeBootstrap,
      sourceRevision: this.sourceRevision,
      roots: this.roots.map(cloneJson),
      sourceFiles: this.listSourceFiles(),
      fileDescriptors: [...this.fileDescriptors.entries()]
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([id, descriptor]) => [id, cloneJson(descriptor)] as [string, CommandDescriptor]),
      pluginDescriptors: [...this.pluginDescriptors.entries()]
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([id, descriptors]) => [id, descriptors.map(cloneJson)] as [string, CommandDescriptor[]]),
      builtinDescriptors: this.builtinDescriptors.map(cloneJson),
      reloadReceipts: [...this.reloadReceipts.values()]
        .sort((left, right) => left.committedAt.localeCompare(right.committedAt))
        .map(cloneJson),
      failures: [...this.failures.values()]
        .sort((left, right) => left.occurredAt.localeCompare(right.occurredAt))
        .map(cloneJson),
      registry: this.registry.snapshot(),
      history: this.history.snapshot(),
      capturedAt: this.timestamp(),
    };
    return {
      ...withoutDigest,
      digest: digest(withoutDigest),
    };
  }

  health(): JsonObject {
    const snapshot = this.snapshot();
    return {
      canonical_owner: "typescript",
      opened: this.opened,
      restored_before_bootstrap: this.restoredBeforeBootstrap,
      source_revision: this.sourceRevision,
      registry_revision: this.registry.currentRevision,
      command_count: this.registry.list().length,
      plugin_command_sources: this.pluginDescriptors.size,
      source_file_count: this.sourceFiles.size,
      failure_count: this.failures.size,
      history_sequence: snapshot.history.sequence,
      python_command_fallback: false,
      snapshot_digest: snapshot.digest,
    };
  }

  close(): void {
    this.opened = false;
  }

  private async reload(metadata: JsonObject): Promise<CommandReloadReceipt> {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    if (this.reloadPromise) {
      return this.reloadPromise;
    }
    this.reloadPromise = this.performReload(metadata);
    try {
      return await this.reloadPromise;
    } finally {
      this.reloadPromise = null;
    }
  }

  private async performReload(metadata: JsonObject): Promise<CommandReloadReceipt> {
    const nextFiles = new Map<string, CommandSourceFile>();
    const nextDescriptors = new Map<string, CommandDescriptor>();
    const failures: CommandLoadFailure[] = [];
    for (const root of this.roots) {
      if (!root.enabled) {
        continue;
      }
      try {
        const files = await scanCommandRoot(root, this.workspaceRoot);
        for (const file of files) {
          try {
            const descriptor = await this.descriptor.parse({
              path: file.realPath,
              sourceKind: file.sourceKind,
              sourceId: file.rootId,
              sourcePriority: file.sourcePriority,
            });
            nextFiles.set(file.sourceFileId, file);
            nextDescriptors.set(file.sourceFileId, descriptor);
          } catch (error) {
            failures.push(this.rememberFailure({
              rootId: root.rootId,
              path: file.realPath,
              code: errorCode(error),
              message: error instanceof Error ? error.message : String(error),
              fatal: root.required,
              metadata: {
                source_file_id: file.sourceFileId,
              },
            }));
          }
        }
      } catch (error) {
        failures.push(this.rememberFailure({
          rootId: root.rootId,
          path: root.rootPath,
          code: errorCode(error),
          message: error instanceof Error ? error.message : String(error),
          fatal: root.required,
          metadata: root.metadata,
        }));
      }
    }
    if (failures.some((failure) => failure.fatal)) {
      throw commandCoordinatorError(
        "command_reload_failed",
        "required command sources failed validation",
        {
          failures: canonicalize(failures),
        },
      );
    }
    this.sourceRevision += 1;
    this.sourceFiles.clear();
    this.fileDescriptors.clear();
    for (const [id, file] of nextFiles) {
      this.sourceFiles.set(id, cloneJson(file));
    }
    for (const [id, descriptor] of nextDescriptors) {
      this.fileDescriptors.set(id, cloneJson(descriptor));
    }
    return this.commitRegistry({
      ...metadata,
      source_revision: this.sourceRevision,
      failures: canonicalize(failures),
    });
  }

  private commitRegistry(metadata: JsonObject): CommandReloadReceipt {
    const sourceRevisionBefore = Math.max(0, this.sourceRevision - 1);
    const registryRevisionBefore = this.registry.currentRevision;
    const descriptors = this.allDescriptors();
    const registration = this.registry.register(
      descriptors,
      registryRevisionBefore,
      metadata,
    );
    this.help.clear();
    const base = {
      receiptId: deterministicId("command-reload-receipt", {
        source_revision: this.sourceRevision,
        registry_revision: registration.revision.revision,
        registry_revision_id: registration.revision.revisionId,
        descriptor_digests: registration.active.map((descriptor) => descriptor.descriptorDigest),
      }, 40),
      sourceRevisionBefore,
      sourceRevisionAfter: this.sourceRevision,
      registryRevisionBefore,
      registryRevisionAfter: registration.revision.revision,
      descriptorCount: registration.active.length,
      sourceFileCount: this.sourceFiles.size,
      added: registration.revision.added,
      updated: registration.revision.updated,
      removed: registration.revision.removed,
      shadowed: registration.revision.shadowed,
      conflicts: registration.conflicts,
      failures: [...this.failures.values()].map(cloneJson),
      committedAt: this.timestamp(),
      metadata: canonicalize(metadata) as JsonObject,
    };
    const receipt: CommandReloadReceipt = {
      ...base,
      digest: digest(base),
    };
    this.reloadReceipts.set(receipt.receiptId, receipt);
    while (this.reloadReceipts.size > 1_000) {
      const first = [...this.reloadReceipts.values()]
        .sort((left, right) => left.committedAt.localeCompare(right.committedAt))[0];
      this.reloadReceipts.delete(first.receiptId);
    }
    return cloneJson(receipt);
  }

  private allDescriptors(): CommandDescriptor[] {
    return [
      ...this.builtinDescriptors,
      ...this.fileDescriptors.values(),
      ...[...this.pluginDescriptors.values()].flat(),
    ].map(validateDescriptor);
  }

  private identity(
    value: Partial<CommandCoordinatorIdentity>,
    argumentsValue: JsonObject,
  ): CommandInvocationIdentity {
    const base = {
      runId: value.runId || "command-run",
      taskId: value.taskId || "command-task",
      sessionId: value.sessionId || "command-session",
      sessionRevision: value.sessionRevision ?? 0,
      workerRequestId: value.workerRequestId || "command-worker",
    };
    if (!Number.isSafeInteger(base.sessionRevision) || base.sessionRevision < 0) {
      throw commandCoordinatorError(
        "command_session_revision_invalid",
        "command session revision must be a non-negative safe integer",
      );
    }
    return {
      ...base,
      commandCallId: value.toolCallId || deterministicId("command-call", {
        ...base,
        arguments_digest: digest(argumentsValue),
      }, 32),
    };
  }

  private listSourceFiles(): CommandSourceFile[] {
    return [...this.sourceFiles.values()]
      .sort((left, right) => left.realPath.localeCompare(right.realPath))
      .map(cloneJson);
  }

  private rememberFailure(input: Omit<CommandLoadFailure, "failureId" | "occurredAt">): CommandLoadFailure {
    const occurredAt = this.timestamp();
    const failure: CommandLoadFailure = {
      failureId: deterministicId("command-load-failure", {
        root_id: input.rootId,
        path: input.path,
        code: input.code,
        message: input.message,
        occurred_at: occurredAt,
      }, 40),
      ...cloneJson(input),
      occurredAt,
    };
    this.failures.set(failure.failureId, failure);
    while (this.failures.size > 10_000) {
      const first = [...this.failures.values()]
        .sort((left, right) => left.occurredAt.localeCompare(right.occurredAt))[0];
      this.failures.delete(first.failureId);
    }
    return cloneJson(failure);
  }

  private restoreLocal(snapshot: CommandCoordinatorSnapshot): void {
    this.sourceRevision = snapshot.sourceRevision;
    for (const file of snapshot.sourceFiles) {
      this.sourceFiles.set(file.sourceFileId, cloneJson(file));
    }
    for (const [id, descriptor] of snapshot.fileDescriptors) {
      this.fileDescriptors.set(id, cloneJson(descriptor));
    }
    for (const [pluginId, descriptors] of snapshot.pluginDescriptors) {
      this.pluginDescriptors.set(pluginId, descriptors.map(cloneJson));
    }
    for (const receipt of snapshot.reloadReceipts) {
      this.reloadReceipts.set(receipt.receiptId, cloneJson(receipt));
    }
    for (const failure of snapshot.failures) {
      this.failures.set(failure.failureId, cloneJson(failure));
    }
  }

  private validateSnapshot(snapshot: CommandCoordinatorSnapshot): void {
    if (snapshot.version !== "zyra.command-coordinator/v1") {
      throw commandCoordinatorError(
        "unsupported_command_coordinator_snapshot",
        `unsupported command coordinator snapshot ${snapshot.version}`,
      );
    }
    const { digest: expectedDigest, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expectedDigest) {
      throw commandCoordinatorError(
        "command_coordinator_snapshot_digest_mismatch",
        "command coordinator snapshot digest does not match its payload",
      );
    }
    if (resolve(snapshot.workspaceRoot) !== this.workspaceRoot) {
      throw commandCoordinatorError(
        "command_coordinator_workspace_mismatch",
        "command coordinator snapshot belongs to another workspace",
      );
    }
  }

  private requireOpen(): void {
    if (!this.opened) {
      throw commandCoordinatorError(
        "command_coordinator_not_open",
        "command coordinator must restore and open before use",
      );
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

async function scanCommandRoot(
  root: CommandSourceRoot,
  workspaceRoot: string,
): Promise<CommandSourceFile[]> {
  if (!existsSync(root.rootPath)) {
    if (root.required) {
      throw commandCoordinatorError(
        "required_command_root_missing",
        `required command root ${root.rootPath} does not exist`,
      );
    }
    return [];
  }
  const rootRealPath = await realpath(root.rootPath);
  assertContained(workspaceRoot, rootRealPath, "command root");
  const output: CommandSourceFile[] = [];
  const queue: Array<{ path: string; depth: number }> = [{
    path: rootRealPath,
    depth: 0,
  }];
  const seen = new Set<string>();
  while (queue.length) {
    const current = queue.shift()!;
    const directoryRealPath = await realpath(current.path);
    assertContained(workspaceRoot, directoryRealPath, "command directory");
    if (seen.has(directoryRealPath.toLowerCase())) {
      continue;
    }
    seen.add(directoryRealPath.toLowerCase());
    const entries = await readdir(directoryRealPath, {
      withFileTypes: true,
    });
    entries.sort((left, right) => left.name.localeCompare(right.name));
    for (const entry of entries) {
      const path = resolve(directoryRealPath, entry.name);
      if (entry.isSymbolicLink()) {
        continue;
      }
      if (entry.isDirectory()) {
        if (root.recursive && current.depth < root.maximumDepth) {
          queue.push({
            path,
            depth: current.depth + 1,
          });
        }
        continue;
      }
      if (!entry.isFile()) {
        continue;
      }
      const extension = extname(entry.name).toLowerCase();
      if (!root.extensions.includes(extension)) {
        continue;
      }
      const realPath = await realpath(path);
      assertContained(workspaceRoot, realPath, "command file");
      const metadata = await stat(realPath);
      const relativePath = relative(rootRealPath, realPath).replace(/\\/g, "/");
      const base = {
        rootId: root.rootId,
        rootPath: rootRealPath,
        path,
        realPath,
        relativePath,
        sourceKind: root.sourceKind,
        sourcePriority: root.sourcePriority,
        modifiedAtMs: metadata.mtimeMs,
        sizeBytes: metadata.size,
        contentIdentity: digest({
          real_path: realPath,
          size_bytes: metadata.size,
          modified_at_ms: metadata.mtimeMs,
        }),
        metadata: canonicalize({
          ...root.metadata,
          depth: current.depth,
        }) as JsonObject,
      };
      output.push({
        sourceFileId: deterministicId("command-source-file", {
          root_id: root.rootId,
          real_path: realPath,
        }, 40),
        ...base,
      });
    }
  }
  return output.sort((left, right) => left.realPath.localeCompare(right.realPath));
}

function normalizeRoot(
  value: CommandSourceRoot,
  workspaceRoot: string,
): CommandSourceRoot {
  if (!value.rootId) {
    throw commandCoordinatorError(
      "command_root_id_missing",
      "command source root requires an id",
    );
  }
  const rootPath = resolve(workspaceRoot, value.rootPath);
  assertContained(workspaceRoot, rootPath, "command source root");
  if (!Number.isSafeInteger(value.sourcePriority)) {
    throw commandCoordinatorError(
      "command_root_priority_invalid",
      `command root ${value.rootId} priority must be an integer`,
    );
  }
  if (
    !Number.isSafeInteger(value.maximumDepth)
    || value.maximumDepth < 0
    || value.maximumDepth > 32
  ) {
    throw commandCoordinatorError(
      "command_root_depth_invalid",
      `command root ${value.rootId} maximum depth must be between 0 and 32`,
    );
  }
  const extensions = [...new Set(
    (value.extensions.length ? value.extensions : [".md"])
      .map((extension) => extension.startsWith(".") ? extension.toLowerCase() : `.${extension.toLowerCase()}`),
  )];
  return {
    ...cloneJson(value),
    rootPath,
    extensions,
  };
}

function compareRoots(
  left: CommandSourceRoot,
  right: CommandSourceRoot,
): number {
  return right.sourcePriority - left.sourcePriority
    || left.rootId.localeCompare(right.rootId);
}

function assertContained(
  workspaceRoot: string,
  path: string,
  label: string,
): void {
  const root = resolve(workspaceRoot);
  const target = resolve(path);
  const relativePath = relative(root, target);
  if (
    relativePath.startsWith("..")
    || isAbsolute(relativePath)
    || target.toLowerCase() !== root.toLowerCase()
      && !target.toLowerCase().startsWith(`${root.toLowerCase()}${sep}`)
  ) {
    throw commandCoordinatorError(
      "command_path_outside_workspace",
      `${label} ${path} is outside workspace ${workspaceRoot}`,
    );
  }
}

function validateDescriptor(value: CommandDescriptor): CommandDescriptor {
  const descriptor = cloneJson(value);
  if (
    !descriptor.commandId
    || !descriptor.name
    || !descriptor.descriptorDigest
  ) {
    throw commandCoordinatorError(
      "command_descriptor_invalid",
      "command descriptor identity is incomplete",
    );
  }
  const expected = digest({
    commandId: descriptor.commandId,
    name: descriptor.name,
    aliases: descriptor.aliases,
    displayName: descriptor.displayName,
    description: descriptor.description,
    usage: descriptor.usage,
    examples: descriptor.examples,
    category: descriptor.category,
    sourceKind: descriptor.sourceKind,
    sourceId: descriptor.sourceId,
    sourcePath: descriptor.sourcePath,
    sourcePriority: descriptor.sourcePriority,
    hidden: descriptor.hidden,
    enabled: descriptor.enabled,
    arguments: descriptor.arguments,
    options: descriptor.options,
    handler: descriptor.handler,
    permission: descriptor.permission,
    body: descriptor.body,
    metadata: descriptor.metadata,
  });
  if (expected !== descriptor.descriptorDigest) {
    throw commandCoordinatorError(
      "command_descriptor_digest_mismatch",
      `command descriptor ${descriptor.commandId} digest is invalid`,
    );
  }
  return descriptor;
}

function sourcePriority(source: PluginManifest["sourceKind"]): number {
  return {
    managed: 900,
    session: 800,
    project: 700,
    user: 600,
    marketplace: 500,
  }[source];
}

function commandTool(
  name: string,
  purpose: string,
  properties: JsonObject,
  accessMode: string,
  required: string[] = [],
): ToolSpecContract {
  return {
    name,
    purpose,
    source: "typescript-command",
    input_schema: {
      type: "object",
      properties,
      additionalProperties: false,
      ...(required.length ? { required } : {}),
    },
    output_schema: {
      type: "object",
    },
    metadata: {
      access_mode: accessMode,
      canonical_runtime_owner: "typescript",
    },
    execution_provenance: {
      namespace: "command",
      server_id: "",
      version: "1",
      source: "zyra-e02-command-coordinator",
    },
  };
}

function commandExecution(
  summary: string,
  output: JsonObject,
  metadata: Record<string, string> = {},
): CommandCoordinatorExecution {
  return {
    summary,
    output,
    metadata: {
      canonical_runtime_owner: "typescript",
      capability_owner: "typescript-command",
      source_custody: "claude-code-best:loadSkillsFromSkillsDir+plugin-command-dispatch",
      python_command_fallback: "false",
      ...metadata,
    },
  };
}

function requiredString(value: JsonObject, key: string): string {
  const item = value[key];
  if (typeof item !== "string" || !item.trim()) {
    throw commandCoordinatorError(
      "command_argument_missing",
      `command argument ${key} is required`,
    );
  }
  return item;
}

function optionalString(value: JsonObject, key: string): string {
  const item = value[key];
  return typeof item === "string" ? item : "";
}

function objectValue(value: JsonValue | undefined): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? cloneJson(value as JsonObject)
    : {};
}

function arrayValue(value: JsonValue | undefined): JsonValue[] {
  return Array.isArray(value)
    ? cloneJson(value)
    : [];
}

function integer(value: JsonValue | undefined, fallback: number): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : fallback;
}

function commandStatus(
  value: JsonValue | undefined,
): CommandInvocationResult["status"] | undefined {
  return typeof value === "string" && new Set([
    "completed",
    "denied",
    "pending_approval",
    "failed",
    "cancelled",
  ]).has(value)
    ? value as CommandInvocationResult["status"]
    : undefined;
}

function errorCode(error: unknown): string {
  if (error && typeof error === "object") {
    const code = (error as { code?: unknown }).code;
    if (typeof code === "string" && code) {
      return code;
    }
  }
  return error instanceof Error && error.name
    ? error.name
    : "command_runtime_failed";
}

function commandCoordinatorError(
  code: string,
  message: string,
  details: JsonObject = {},
): Error {
  const error = new Error(message);
  error.name = "CommandCoordinatorError";
  Object.assign(error, {
    code,
    details: canonicalize(details),
  });
  return error;
}

export function commandPermissionDecision(
  descriptor: CommandDescriptor,
  request: CommandInvocationRequest,
  argumentsValue: JsonObject,
  input: Omit<CommandPermissionDecision, "requestDigest">,
): CommandPermissionDecision {
  return {
    ...cloneJson(input),
    requestDigest: digest({
      command_id: descriptor.commandId,
      descriptor_digest: descriptor.descriptorDigest,
      request_identity: request.identity,
      arguments: argumentsValue,
      permission: descriptor.permission,
    }),
  };
}
