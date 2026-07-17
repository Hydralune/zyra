import {
  existsSync,
  watch,
  type FSWatcher,
} from "node:fs";
import { createHmac, timingSafeEqual } from "node:crypto";
import {
  readdir,
  realpath,
  stat,
} from "node:fs/promises";
import {
  dirname,
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
import {
  PluginCacheRuntime,
  type PluginCacheSnapshot,
} from "./cache-runtime.ts";
import {
  PluginCapabilityRuntime,
  type PluginCapabilitySnapshot,
} from "./capability-runtime.ts";
import type {
  PluginHookContext,
  PluginHookExecutor,
  PluginHookResult,
  PluginLoadResult,
  PluginManifest,
  PluginRuntimeRecord,
  PluginRuntimeSnapshot,
  PluginSourceKind,
} from "./contracts.ts";
import {
  PluginDependencyRuntime,
  type PluginDependencySnapshot,
} from "./dependency-runtime.ts";
import {
  PluginHookRuntime,
  type PluginHookRegistration,
} from "./hook-runtime.ts";
import { PluginManifestRuntime } from "./manifest-runtime.ts";
import {
  PluginMarketplaceRuntime,
  type PluginMarketplaceSnapshot,
} from "./marketplace-runtime.ts";
import {
  PluginRuntime,
  type PluginRuntimeIntegration,
} from "./plugin-runtime.ts";
import {
  PluginSupplyChainRuntime,
  type PluginSupplyChainPolicy,
  type PluginSupplyChainReceipt,
  type PluginSupplyChainSnapshot,
} from "./supply-chain-runtime.ts";

export interface PluginSourceRoot {
  rootId: string;
  rootPath: string;
  sourceKind: PluginSourceKind;
  priority: number;
  required: boolean;
  recursive: boolean;
  maximumDepth: number;
  enabled: boolean;
  metadata: JsonObject;
}

export interface PluginSourceCandidate {
  candidateId: string;
  rootId: string;
  rootPath: string;
  manifestPath: string;
  realManifestPath: string;
  sourceKind: PluginSourceKind;
  priority: number;
  modifiedAtMs: number;
  sizeBytes: number;
  pathDigest: string;
  metadata: JsonObject;
}

export interface PluginDiscoveryFailure {
  failureId: string;
  rootId: string;
  path: string;
  code: string;
  message: string;
  fatal: boolean;
  occurredAt: string;
  metadata: JsonObject;
}

export interface PluginDiscoveryRevision {
  revisionId: string;
  revisionBefore: number;
  revisionAfter: number;
  candidates: PluginSourceCandidate[];
  failures: PluginDiscoveryFailure[];
  added: string[];
  updated: string[];
  removed: string[];
  unchanged: string[];
  committedAt: string;
  digest: string;
  metadata: JsonObject;
}

export interface PluginReloadReceipt {
  receiptId: string;
  discoveryRevision: number;
  dependencyRevision: number;
  capabilityRevision: number;
  loaded: PluginLoadResult[];
  unchanged: string[];
  unloaded: string[];
  failed: PluginDiscoveryFailure[];
  supplyChainReceipts: string[];
  startedAt: string;
  committedAt: string;
  digest: string;
  metadata: JsonObject;
}

export interface PluginWatchEvent {
  eventId: string;
  rootId: string;
  eventType: string;
  filename: string | null;
  observedAt: string;
  sequence: number;
  metadata: JsonObject;
}

export interface PluginCoordinatorExecution {
  summary: string;
  output: JsonObject;
  metadata: Record<string, string>;
}

export interface PluginCoordinatorSnapshot {
  version: "zyra.plugin-coordinator/v1";
  workspaceRoot: string;
  opened: boolean;
  restoredBeforeBootstrap: boolean;
  discoveryRevision: number;
  roots: PluginSourceRoot[];
  candidates: PluginSourceCandidate[];
  discoveryHistory: PluginDiscoveryRevision[];
  reloadReceipts: PluginReloadReceipt[];
  watchEvents: PluginWatchEvent[];
  watchActive: boolean;
  watchSequence: number;
  pendingReload: boolean;
  runtime: PluginRuntimeSnapshot;
  cache: PluginCacheSnapshot;
  dependencies: PluginDependencySnapshot;
  capabilities: PluginCapabilitySnapshot;
  supplyChain: PluginSupplyChainSnapshot;
  marketplace: PluginMarketplaceSnapshot;
  hookRevision: number;
  hookRegistrations: PluginHookRegistration[];
  failures: PluginDiscoveryFailure[];
  digest: string;
  capturedAt: string;
}

export interface PluginCoordinatorOptions {
  workspaceRoot: string;
  roots: PluginSourceRoot[];
  integration: PluginRuntimeIntegration;
  hookExecutor: PluginHookExecutor;
  invokeCommand: (
    record: PluginRuntimeRecord,
    commandName: string,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ) => Promise<JsonValue>;
  supplyChainPolicy?: Partial<PluginSupplyChainPolicy>;
  trustedMarketplaceKeys?: Record<string, string>;
  watch?: boolean;
  watchDebounceMs?: number;
  now?: () => Date;
  snapshot?: PluginCoordinatorSnapshot | null;
}

const manifestNames = new Set([
  "plugin.json",
  "zyra-plugin.json",
]);

const manifestDirectories = new Set([
  ".zyra-plugin",
  ".claude-plugin",
]);

const defaultSupplyPolicy = (
  workspaceRoot: string,
): PluginSupplyChainPolicy => ({
  workspaceRoot,
  allowedExtensions: [
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".cjs",
    ".json",
    ".md",
    ".mdx",
    ".yaml",
    ".yml",
    ".txt",
    ".css",
    ".html",
    ".svg",
  ],
  deniedExtensions: [
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".node",
    ".msi",
    ".deb",
    ".rpm",
    ".jar",
    ".class",
  ],
  maximumFiles: 10_000,
  maximumFileBytes: 8 * 1024 * 1024,
  maximumTotalBytes: 256 * 1024 * 1024,
  allowSymlinks: false,
  allowNativeBinaries: false,
  allowPackageScripts: false,
  requireLicense: false,
  forbiddenPathFragments: [
    "../",
    "node_modules/.cache",
    ".git/objects",
    ".env",
    "credentials",
    "private-key",
  ],
  forbiddenContentPatterns: [
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "child_process.execSync(",
  ],
  metadata: {
    owner: "typescript-plugin-coordinator",
  },
});

export class PluginCoordinator {
  readonly manifests: PluginManifestRuntime;
  readonly cache: PluginCacheRuntime;
  readonly dependencies: PluginDependencyRuntime;
  readonly capabilities: PluginCapabilityRuntime;
  readonly supplyChain: PluginSupplyChainRuntime;
  readonly marketplace: PluginMarketplaceRuntime;
  readonly hooks: PluginHookRuntime;
  readonly runtime: PluginRuntime;
  private readonly workspaceRoot: string;
  private readonly roots: PluginSourceRoot[];
  private readonly externalIntegration: PluginRuntimeIntegration;
  private readonly invokeCommand: PluginCoordinatorOptions["invokeCommand"];
  private readonly watchEnabled: boolean;
  private readonly watchDebounceMs: number;
  private readonly now: () => Date;
  private readonly candidates = new Map<string, PluginSourceCandidate>();
  private readonly discoveryHistory = new Map<number, PluginDiscoveryRevision>();
  private readonly reloadReceipts = new Map<string, PluginReloadReceipt>();
  private readonly watchers = new Map<string, FSWatcher>();
  private readonly watchEvents: PluginWatchEvent[] = [];
  private readonly failures = new Map<string, PluginDiscoveryFailure>();
  private readonly hookRegistrations = new Map<string, PluginHookRegistration>();
  private readonly pluginRevisions = new Map<string, number>();
  private opened = false;
  private restoredBeforeBootstrap = false;
  private discoveryRevision = 0;
  private hookRevision = 0;
  private watchSequence = 0;
  private pendingReload = false;
  private reloadPromise: Promise<PluginReloadReceipt> | null = null;
  private reloadTimer: ReturnType<typeof setTimeout> | null = null;
  private lastTimestamp: string | null = null;

  constructor(options: PluginCoordinatorOptions) {
    if (!options.workspaceRoot) {
      throw pluginCoordinatorError(
        "plugin_workspace_missing",
        "plugin coordinator requires a workspace root",
      );
    }
    this.workspaceRoot = resolve(options.workspaceRoot);
    this.roots = options.roots
      .map((root) => normalizeRoot(root, this.workspaceRoot))
      .sort(compareRoots);
    this.externalIntegration = options.integration;
    this.invokeCommand = options.invokeCommand;
    this.watchEnabled = options.watch !== false;
    this.watchDebounceMs = Math.max(25, options.watchDebounceMs ?? 150);
    this.now = options.now ?? (() => new Date());
    const snapshot = options.snapshot ?? null;
    if (snapshot) {
      this.validateSnapshot(snapshot);
    }
    this.manifests = new PluginManifestRuntime({
      workspaceRoot: this.workspaceRoot,
      allowOutsideWorkspace: false,
    });
    this.cache = new PluginCacheRuntime({
      now: this.now,
      snapshot: snapshot?.cache ?? null,
    });
    this.dependencies = new PluginDependencyRuntime({
      now: this.now,
      snapshot: snapshot?.dependencies ?? null,
    });
    this.capabilities = new PluginCapabilityRuntime({
      now: this.now,
      snapshot: snapshot?.capabilities ?? null,
    });
    const supplyPolicy = {
      ...defaultSupplyPolicy(this.workspaceRoot),
      ...(options.supplyChainPolicy ?? {}),
      workspaceRoot: this.workspaceRoot,
      metadata: {
        ...defaultSupplyPolicy(this.workspaceRoot).metadata,
        ...(options.supplyChainPolicy?.metadata ?? {}),
      },
    };
    this.supplyChain = new PluginSupplyChainRuntime({
      policy: supplyPolicy,
      now: this.now,
      snapshot: snapshot?.supplyChain ?? null,
    });
    this.marketplace = new PluginMarketplaceRuntime({
      verifySignature: marketplaceVerifier(options.trustedMarketplaceKeys ?? {}),
      now: this.now,
      snapshot: snapshot?.marketplace ?? null,
    });
    this.hooks = new PluginHookRuntime({
      executor: options.hookExecutor,
      now: this.now,
    });
    const internalIntegration = this.integrationBoundary();
    this.runtime = new PluginRuntime({
      manifests: this.manifests,
      cache: this.cache,
      integration: internalIntegration,
      now: this.now,
      snapshot: snapshot?.runtime ?? null,
    });
    if (snapshot) {
      this.restoreLocal(snapshot);
      this.restoredBeforeBootstrap = true;
    }
  }

  async open(): Promise<void> {
    if (this.opened) {
      return;
    }
    if (!this.restoredBeforeBootstrap || !this.runtime.list().length) {
      await this.reload({
        reason: "bootstrap",
      });
    } else {
      await this.rebindRestoredCapabilities();
    }
    if (this.watchEnabled) {
      await this.startWatchers();
    }
    this.opened = true;
  }

  owns(toolName: string): boolean {
    return [
      "list_plugins",
      "reload_plugins",
      "plugin_command",
      "plugin_status",
      "plugin_disable",
    ].includes(toolName);
  }

  toolSpecs(): ToolSpecContract[] {
    return [
      pluginTool(
        "list_plugins",
        "List TypeScript-owned plugin revisions and capabilities",
        {},
        "read",
      ),
      pluginTool(
        "plugin_status",
        "Inspect one TypeScript-owned plugin revision",
        {
          plugin_id: { type: "string" },
        },
        "read",
        ["plugin_id"],
      ),
      pluginTool(
        "reload_plugins",
        "Atomically discover, validate, and reload plugin revisions",
        {},
        "execute",
      ),
      pluginTool(
        "plugin_command",
        "Invoke a command contributed by an active plugin revision",
        {
          plugin_id: { type: "string" },
          command: { type: "string" },
          arguments: { type: "object" },
        },
        "execute",
        ["plugin_id", "command"],
      ),
      pluginTool(
        "plugin_disable",
        "Disable an active plugin without moving in-flight calls to a new revision",
        {
          plugin_id: { type: "string" },
          reason: { type: "string" },
        },
        "execute",
        ["plugin_id"],
      ),
    ];
  }

  async execute(
    toolName: string,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ): Promise<PluginCoordinatorExecution> {
    this.requireOpen();
    if (toolName === "list_plugins") {
      const plugins = this.runtime.list().map(projectPluginRecord);
      return pluginExecution(
        `Listed ${plugins.length} plugins`,
        {
          plugins: canonicalize(plugins),
          discovery_revision: this.discoveryRevision,
          dependency_revision: this.dependencies.snapshot().revision,
          capability_revision: this.capabilities.snapshot().revision,
        },
      );
    }
    if (toolName === "plugin_status") {
      const pluginId = requiredString(argumentsValue, "plugin_id");
      const record = this.runtime.get(pluginId);
      if (!record) {
        throw pluginCoordinatorError(
          "plugin_not_found",
          `plugin ${pluginId} was not found`,
        );
      }
      return pluginExecution(
        `Plugin ${pluginId} is ${record.status}`,
        {
          plugin: projectPluginRecord(record),
          capabilities: canonicalize(this.capabilities.list({ pluginId })),
          supply_chain: canonicalize(this.supplyChain.list(pluginId)),
        },
      );
    }
    if (toolName === "reload_plugins") {
      const receipt = await this.reload({
        reason: "tool_request",
      });
      return pluginExecution(
        `Reloaded ${receipt.loaded.length} plugins`,
        canonicalize(receipt) as JsonObject,
      );
    }
    if (toolName === "plugin_disable") {
      const pluginId = requiredString(argumentsValue, "plugin_id");
      const reason = optionalString(argumentsValue, "reason") || "tool_request";
      const unloaded = await this.runtime.unload(pluginId, reason);
      await this.commitGlobalState({
        reason: "plugin_disabled",
        plugin_id: pluginId,
        unloaded,
      });
      return pluginExecution(
        unloaded
          ? `Disabled plugin ${pluginId}`
          : `Plugin ${pluginId} disable is waiting for in-flight calls`,
        {
          plugin_id: pluginId,
          unloaded,
          reason,
        },
      );
    }
    if (toolName !== "plugin_command") {
      throw pluginCoordinatorError(
        "plugin_tool_not_owned",
        `plugin coordinator does not own ${toolName}`,
      );
    }
    const pluginId = requiredString(argumentsValue, "plugin_id");
    const commandName = requiredString(argumentsValue, "command");
    const acquisition = this.runtime.acquire(pluginId);
    const capabilityId = commandName;
    let capabilityRelease: (() => void) | null = null;
    try {
      const capability = this.capabilities.acquire(
        "command",
        capabilityId,
        deterministicId("plugin-command-invocation", {
          plugin_id: pluginId,
          plugin_revision: acquisition.record.revision,
          command: commandName,
          arguments_digest: digest(argumentsValue.arguments ?? {}),
        }, 32),
        this.capabilities.snapshot().revision,
        {
          plugin_revision: acquisition.record.revision,
        },
      );
      capabilityRelease = capability.release;
      const output = await this.invokeCommand(
        acquisition.record,
        commandName,
        objectValue(argumentsValue.arguments),
        signal,
      );
      return pluginExecution(
        `Plugin command ${pluginId}/${commandName} completed`,
        {
          plugin_id: pluginId,
          plugin_revision: acquisition.record.revision,
          command: commandName,
          output: canonicalize(output),
          capability_lease_id: capability.lease.leaseId,
        },
        {
          plugin_id: pluginId,
          plugin_revision: String(acquisition.record.revision),
          plugin_command: commandName,
        },
      );
    } finally {
      capabilityRelease?.();
      acquisition.release();
    }
  }

  async beforeTool(
    context: PluginHookContext,
    signal?: AbortSignal,
  ): Promise<{
    arguments: JsonObject;
    effect: "continue" | "deny" | "ask";
    results: PluginHookResult[];
    failedClosed: boolean;
  }> {
    this.requireOpen();
    return this.hooks.beforeTool(context, signal);
  }

  async reload(metadata: JsonObject = {}): Promise<PluginReloadReceipt> {
    if (this.reloadPromise) {
      return this.reloadPromise;
    }
    this.pendingReload = true;
    this.reloadPromise = this.performReload(metadata);
    try {
      return await this.reloadPromise;
    } finally {
      this.reloadPromise = null;
      this.pendingReload = false;
    }
  }

  snapshot(): PluginCoordinatorSnapshot {
    const withoutDigest = {
      version: "zyra.plugin-coordinator/v1" as const,
      workspaceRoot: this.workspaceRoot,
      opened: this.opened,
      restoredBeforeBootstrap: this.restoredBeforeBootstrap,
      discoveryRevision: this.discoveryRevision,
      roots: this.roots.map(cloneJson),
      candidates: this.listCandidates(),
      discoveryHistory: [...this.discoveryHistory.values()]
        .sort((left, right) => left.revisionAfter - right.revisionAfter)
        .map(cloneJson),
      reloadReceipts: [...this.reloadReceipts.values()]
        .sort((left, right) => left.committedAt.localeCompare(right.committedAt))
        .map(cloneJson),
      watchEvents: this.watchEvents.map(cloneJson),
      watchActive: this.watchers.size > 0,
      watchSequence: this.watchSequence,
      pendingReload: this.pendingReload,
      runtime: this.runtime.snapshot(),
      cache: this.cache.snapshot(),
      dependencies: this.dependencies.snapshot(),
      capabilities: this.capabilities.snapshot(),
      supplyChain: this.supplyChain.snapshot(),
      marketplace: this.marketplace.snapshot(),
      hookRevision: this.hookRevision,
      hookRegistrations: [...this.hookRegistrations.values()]
        .sort((left, right) => left.pluginId.localeCompare(right.pluginId) || left.hook.hookId.localeCompare(right.hook.hookId))
        .map(cloneJson),
      failures: [...this.failures.values()]
        .sort((left, right) => left.occurredAt.localeCompare(right.occurredAt))
        .map(cloneJson),
      capturedAt: this.timestamp(),
    };
    return {
      ...withoutDigest,
      digest: digest(withoutDigest),
    };
  }

  health(): JsonObject {
    const runtimeRecords = this.runtime.list();
    const snapshot = this.snapshot();
    return {
      canonical_owner: "typescript",
      opened: this.opened,
      restored_before_bootstrap: this.restoredBeforeBootstrap,
      discovery_revision: this.discoveryRevision,
      dependency_revision: snapshot.dependencies.revision,
      capability_revision: snapshot.capabilities.revision,
      active_plugins: runtimeRecords.filter((record) => record.status === "active").length,
      failed_plugins: runtimeRecords.filter((record) => record.status === "failed").length,
      discovered_candidates: this.candidates.size,
      hook_revision: this.hookRevision,
      hook_count: this.hookRegistrations.size,
      watch_active: this.watchers.size > 0,
      pending_reload: this.pendingReload,
      discovery_failure_count: this.failures.size,
      python_plugin_dispatch_fallback: false,
      snapshot_digest: snapshot.digest,
    };
  }

  async close(): Promise<void> {
    if (this.reloadTimer) {
      clearTimeout(this.reloadTimer);
      this.reloadTimer = null;
    }
    for (const watcher of this.watchers.values()) {
      watcher.close();
    }
    this.watchers.clear();
    this.opened = false;
  }

  private async performReload(metadata: JsonObject): Promise<PluginReloadReceipt> {
    const startedAt = this.timestamp();
    const discovery = await this.discover(metadata);
    if (discovery.failures.some((failure) => failure.fatal)) {
      throw pluginCoordinatorError(
        "plugin_discovery_failed",
        "one or more required plugin roots could not be discovered",
        {
          failures: canonicalize(discovery.failures),
        },
      );
    }
    const parsed: Array<{
      candidate: PluginSourceCandidate;
      manifest: PluginManifest;
      supply: PluginSupplyChainReceipt;
    }> = [];
    const failures = [...discovery.failures];
    for (const candidate of discovery.candidates) {
      try {
        const manifest = await this.manifests.parse(
          candidate.manifestPath,
          candidate.sourceKind,
        );
        const supply = await this.supplyChain.scan(manifest, {
          discovery_revision: discovery.revisionAfter,
          candidate_id: candidate.candidateId,
        });
        this.supplyChain.requireAllowed(supply.receiptId, manifest.manifestDigest);
        parsed.push({
          candidate,
          manifest,
          supply,
        });
      } catch (error) {
        failures.push(this.rememberFailure({
          rootId: candidate.rootId,
          path: candidate.manifestPath,
          code: errorCode(error),
          message: error instanceof Error ? error.message : String(error),
          fatal: candidate.sourceKind === "managed",
          metadata: {
            candidate_id: candidate.candidateId,
          },
        }));
      }
    }
    if (failures.some((failure) => failure.fatal)) {
      throw pluginCoordinatorError(
        "plugin_validation_failed",
        "managed plugin validation failed",
        {
          failures: canonicalize(failures),
        },
      );
    }
    const selected = resolveCandidateConflicts(parsed);
    const dependency = this.dependencies.resolve(
      selected.map((item) => item.manifest),
      this.dependencies.snapshot().revision,
      {
        discovery_revision: discovery.revisionAfter,
      },
    );
    const selectedById = new Map(
      selected.map((item) => [item.manifest.pluginId, item]),
    );
    const loaded: PluginLoadResult[] = [];
    const unchanged: string[] = [];
    for (const pluginId of dependency.loadOrder) {
      const item = selectedById.get(pluginId);
      if (!item) {
        continue;
      }
      const existing = this.runtime.get(pluginId);
      if (
        existing
        && existing.manifest.manifestDigest === item.manifest.manifestDigest
        && existing.status === "active"
      ) {
        unchanged.push(pluginId);
        this.pluginRevisions.set(pluginId, existing.revision);
        continue;
      }
      try {
        const result = existing
          ? await this.runtime.reload(
              pluginId,
              item.candidate.manifestPath,
              item.candidate.sourceKind,
            )
          : await this.runtime.load(
              item.candidate.manifestPath,
              item.candidate.sourceKind,
            );
        loaded.push(result);
        this.pluginRevisions.set(pluginId, result.revision);
      } catch (error) {
        failures.push(this.rememberFailure({
          rootId: item.candidate.rootId,
          path: item.candidate.manifestPath,
          code: errorCode(error),
          message: error instanceof Error ? error.message : String(error),
          fatal: item.manifest.sourceKind === "managed",
          metadata: {
            plugin_id: pluginId,
            manifest_digest: item.manifest.manifestDigest,
          },
        }));
      }
    }
    if (failures.some((failure) => failure.fatal)) {
      throw pluginCoordinatorError(
        "plugin_load_failed",
        "managed plugin load failed",
        {
          failures: canonicalize(failures),
        },
      );
    }
    const desiredIds = new Set(selected.map((item) => item.manifest.pluginId));
    const unloaded: string[] = [];
    for (const record of this.runtime.list()) {
      if (desiredIds.has(record.pluginId)) {
        continue;
      }
      const complete = await this.runtime.unload(
        record.pluginId,
        "removed_from_discovery",
      );
      if (complete) {
        unloaded.push(record.pluginId);
        this.pluginRevisions.delete(record.pluginId);
      }
    }
    const global = await this.commitGlobalState({
      discovery_revision: discovery.revisionAfter,
      dependency_revision: dependency.revision,
      loaded: loaded.map((item) => item.pluginId),
      unchanged,
      unloaded,
    });
    const base = {
      receiptId: deterministicId("plugin-reload-receipt", {
        discovery_revision: discovery.revisionAfter,
        dependency_revision: dependency.revision,
        capability_revision: global.capabilityRevision,
        loaded: loaded.map((item) => [item.pluginId, item.revision]),
        unchanged,
        unloaded,
        failure_ids: failures.map((failure) => failure.failureId),
      }, 40),
      discoveryRevision: discovery.revisionAfter,
      dependencyRevision: dependency.revision,
      capabilityRevision: global.capabilityRevision,
      loaded: loaded.map(cloneJson),
      unchanged: unchanged.sort(),
      unloaded: unloaded.sort(),
      failed: failures.map(cloneJson),
      supplyChainReceipts: selected
        .map((item) => item.supply.receiptId)
        .sort(),
      startedAt,
      committedAt: this.timestamp(),
      metadata: canonicalize(metadata) as JsonObject,
    };
    const receipt: PluginReloadReceipt = {
      ...base,
      digest: digest(base),
    };
    this.reloadReceipts.set(receipt.receiptId, receipt);
    return cloneJson(receipt);
  }

  private async discover(metadata: JsonObject): Promise<PluginDiscoveryRevision> {
    const revisionBefore = this.discoveryRevision;
    const next = new Map<string, PluginSourceCandidate>();
    const failures: PluginDiscoveryFailure[] = [];
    for (const root of this.roots) {
      if (!root.enabled) {
        continue;
      }
      try {
        const candidates = await discoverRoot(root, this.workspaceRoot);
        for (const candidate of candidates) {
          const existing = next.get(candidate.realManifestPath.toLowerCase());
          if (!existing || compareCandidate(candidate, existing) < 0) {
            next.set(candidate.realManifestPath.toLowerCase(), candidate);
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
    const added: string[] = [];
    const updated: string[] = [];
    const removed: string[] = [];
    const unchanged: string[] = [];
    const nextById = new Map(
      [...next.values()].map((candidate) => [candidate.candidateId, candidate]),
    );
    for (const id of new Set([...this.candidates.keys(), ...nextById.keys()])) {
      const before = this.candidates.get(id);
      const after = nextById.get(id);
      if (!before && after) {
        added.push(id);
      } else if (before && !after) {
        removed.push(id);
      } else if (before && after && digest(before) !== digest(after)) {
        updated.push(id);
      } else {
        unchanged.push(id);
      }
    }
    this.discoveryRevision += 1;
    this.candidates.clear();
    for (const candidate of nextById.values()) {
      this.candidates.set(candidate.candidateId, cloneJson(candidate));
    }
    const base = {
      revisionId: deterministicId("plugin-discovery-revision", {
        revision_before: revisionBefore,
        revision_after: this.discoveryRevision,
        candidates: this.listCandidates().map((candidate) => [
          candidate.candidateId,
          candidate.pathDigest,
          candidate.modifiedAtMs,
        ]),
        failure_ids: failures.map((failure) => failure.failureId),
      }, 40),
      revisionBefore,
      revisionAfter: this.discoveryRevision,
      candidates: this.listCandidates(),
      failures: failures.map(cloneJson),
      added: added.sort(),
      updated: updated.sort(),
      removed: removed.sort(),
      unchanged: unchanged.sort(),
      committedAt: this.timestamp(),
      metadata: canonicalize(metadata) as JsonObject,
    };
    const revision: PluginDiscoveryRevision = {
      ...base,
      digest: digest(base),
    };
    this.discoveryHistory.set(revision.revisionAfter, revision);
    while (this.discoveryHistory.size > 100) {
      const first = [...this.discoveryHistory.keys()].sort((left, right) => left - right)[0];
      this.discoveryHistory.delete(first);
    }
    return cloneJson(revision);
  }

  private integrationBoundary(): PluginRuntimeIntegration {
    return {
      addSkills: async (manifest) => {
        return this.externalIntegration.addSkills(manifest);
      },
      addCommands: async (manifest) => {
        return this.externalIntegration.addCommands(manifest);
      },
      addHooks: async (manifest) => {
        const external = await this.externalIntegration.addHooks(manifest);
        for (const hook of manifest.hooks) {
          const key = `${manifest.pluginId}:${hook.hookId}`;
          this.hookRegistrations.set(key, {
            pluginId: manifest.pluginId,
            pluginRevision: this.pluginRevisions.get(manifest.pluginId) ?? this.runtime?.get(manifest.pluginId)?.revision ?? 0,
            manifestDigest: manifest.manifestDigest,
            manifest: cloneJson(manifest),
            hook: cloneJson(hook),
          });
        }
        this.refreshHooks();
        return {
          ...external,
          hook_revision: this.hookRevision,
          hook_count: manifest.hooks.length,
        };
      },
      addAgents: async (manifest) => {
        return this.externalIntegration.addAgents(manifest);
      },
      addMcpServers: async (manifest) => {
        return this.externalIntegration.addMcpServers(manifest);
      },
      remove: async (pluginId, revision) => {
        for (const key of [...this.hookRegistrations.keys()]) {
          if (key.startsWith(`${pluginId}:`)) {
            this.hookRegistrations.delete(key);
          }
        }
        this.refreshHooks();
        await this.externalIntegration.remove(pluginId, revision);
      },
    };
  }

  private refreshHooks(): void {
    const registrations = [...this.hookRegistrations.values()]
      .sort((left, right) => left.pluginId.localeCompare(right.pluginId) || left.hook.priority - right.hook.priority || left.hook.hookId.localeCompare(right.hook.hookId));
    this.hookRevision = this.hooks.replace(
      registrations,
      this.hookRevision,
    );
  }

  private async commitGlobalState(metadata: JsonObject): Promise<{
    dependencyRevision: number;
    capabilityRevision: number;
  }> {
    const manifests = this.runtime.list()
      .filter((record) => record.status === "active")
      .map((record) => record.manifest);
    const dependencyHead = this.dependencies.head();
    let dependencyRevision = dependencyHead?.revision ?? 0;
    const desiredDigest = digest(
      manifests.map((manifest) => [manifest.pluginId, manifest.manifestDigest]),
    );
    const currentDigest = dependencyHead
      ? digest(dependencyHead.nodes.map((node) => [node.pluginId, node.manifestDigest]))
      : digest([]);
    if (desiredDigest !== currentDigest) {
      dependencyRevision = this.dependencies.resolve(
        manifests,
        this.dependencies.snapshot().revision,
        metadata,
      ).revision;
    }
    const revisions = Object.fromEntries(
      this.runtime.list().map((record) => [record.pluginId, record.revision]),
    );
    const capability = this.capabilities.commit(
      manifests,
      revisions,
      this.capabilities.snapshot().revision,
      metadata,
    );
    return {
      dependencyRevision,
      capabilityRevision: capability.revisionAfter,
    };
  }

  private async rebindRestoredCapabilities(): Promise<void> {
    for (const record of this.runtime.list()) {
      if (record.status !== "active") {
        continue;
      }
      await this.externalIntegration.addSkills(record.manifest);
      await this.externalIntegration.addCommands(record.manifest);
      await this.externalIntegration.addAgents(record.manifest);
      await this.externalIntegration.addMcpServers(record.manifest);
      await this.externalIntegration.addHooks(record.manifest);
      for (const hook of record.manifest.hooks) {
        this.hookRegistrations.set(`${record.pluginId}:${hook.hookId}`, {
          pluginId: record.pluginId,
          pluginRevision: record.revision,
          manifestDigest: record.manifest.manifestDigest,
          manifest: cloneJson(record.manifest),
          hook: cloneJson(hook),
        });
      }
      this.pluginRevisions.set(record.pluginId, record.revision);
    }
    this.refreshHooks();
  }

  private async startWatchers(): Promise<void> {
    for (const root of this.roots) {
      if (!root.enabled || !existsSync(root.rootPath)) {
        continue;
      }
      if (this.watchers.has(root.rootId)) {
        continue;
      }
      try {
        const watcher = watch(
          root.rootPath,
          {
            recursive: root.recursive,
            persistent: false,
          },
          (eventType, filename) => {
            this.observeWatch(root, eventType, filename?.toString() ?? null);
          },
        );
        watcher.on("error", (error) => {
          this.rememberFailure({
            rootId: root.rootId,
            path: root.rootPath,
            code: "plugin_watch_failed",
            message: error.message,
            fatal: root.required,
            metadata: {},
          });
        });
        this.watchers.set(root.rootId, watcher);
      } catch (error) {
        const failure = this.rememberFailure({
          rootId: root.rootId,
          path: root.rootPath,
          code: "plugin_watch_start_failed",
          message: error instanceof Error ? error.message : String(error),
          fatal: root.required,
          metadata: {},
        });
        if (failure.fatal) {
          throw pluginCoordinatorError(
            failure.code,
            failure.message,
          );
        }
      }
    }
  }

  private observeWatch(
    root: PluginSourceRoot,
    eventType: string,
    filename: string | null,
  ): void {
    this.watchSequence += 1;
    const event: PluginWatchEvent = {
      eventId: deterministicId("plugin-watch-event", {
        root_id: root.rootId,
        sequence: this.watchSequence,
        event_type: eventType,
        filename,
      }, 32),
      rootId: root.rootId,
      eventType,
      filename,
      observedAt: this.timestamp(),
      sequence: this.watchSequence,
      metadata: {},
    };
    this.watchEvents.push(event);
    while (this.watchEvents.length > 10_000) {
      this.watchEvents.shift();
    }
    this.pendingReload = true;
    if (this.reloadTimer) {
      clearTimeout(this.reloadTimer);
    }
    this.reloadTimer = setTimeout(() => {
      this.reloadTimer = null;
      void this.reload({
        reason: "filesystem_watch",
        event_id: event.eventId,
      }).catch((error) => {
        this.rememberFailure({
          rootId: root.rootId,
          path: root.rootPath,
          code: errorCode(error),
          message: error instanceof Error ? error.message : String(error),
          fatal: root.required,
          metadata: {
            event_id: event.eventId,
          },
        });
      });
    }, this.watchDebounceMs);
  }

  private rememberFailure(input: Omit<PluginDiscoveryFailure, "failureId" | "occurredAt">): PluginDiscoveryFailure {
    const occurredAt = this.timestamp();
    const failure: PluginDiscoveryFailure = {
      failureId: deterministicId("plugin-discovery-failure", {
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

  private listCandidates(): PluginSourceCandidate[] {
    return [...this.candidates.values()]
      .sort(compareCandidate)
      .map(cloneJson);
  }

  private restoreLocal(snapshot: PluginCoordinatorSnapshot): void {
    this.discoveryRevision = snapshot.discoveryRevision;
    this.hookRevision = 0;
    this.watchSequence = snapshot.watchSequence;
    this.pendingReload = false;
    for (const candidate of snapshot.candidates) {
      this.candidates.set(candidate.candidateId, cloneJson(candidate));
    }
    for (const revision of snapshot.discoveryHistory) {
      this.discoveryHistory.set(revision.revisionAfter, cloneJson(revision));
    }
    for (const receipt of snapshot.reloadReceipts) {
      this.reloadReceipts.set(receipt.receiptId, cloneJson(receipt));
    }
    this.watchEvents.push(...snapshot.watchEvents.map(cloneJson));
    for (const failure of snapshot.failures) {
      this.failures.set(failure.failureId, cloneJson(failure));
    }
    for (const registration of snapshot.hookRegistrations) {
      const key = `${registration.pluginId}:${registration.hook.hookId}`;
      this.hookRegistrations.set(key, cloneJson(registration));
    }
    for (const record of snapshot.runtime.records) {
      this.pluginRevisions.set(record.pluginId, record.revision);
    }
  }

  private validateSnapshot(snapshot: PluginCoordinatorSnapshot): void {
    if (snapshot.version !== "zyra.plugin-coordinator/v1") {
      throw pluginCoordinatorError(
        "unsupported_plugin_coordinator_snapshot",
        `unsupported plugin coordinator snapshot ${snapshot.version}`,
      );
    }
    const { digest: expectedDigest, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expectedDigest) {
      throw pluginCoordinatorError(
        "plugin_coordinator_snapshot_digest_mismatch",
        "plugin coordinator snapshot digest does not match its payload",
      );
    }
    if (resolve(snapshot.workspaceRoot) !== this.workspaceRoot) {
      throw pluginCoordinatorError(
        "plugin_coordinator_workspace_mismatch",
        "plugin coordinator snapshot belongs to another workspace",
      );
    }
  }

  private requireOpen(): void {
    if (!this.opened) {
      throw pluginCoordinatorError(
        "plugin_coordinator_not_open",
        "plugin coordinator must restore and open before use",
      );
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

async function discoverRoot(
  root: PluginSourceRoot,
  workspaceRoot: string,
): Promise<PluginSourceCandidate[]> {
  if (!existsSync(root.rootPath)) {
    if (root.required) {
      throw pluginCoordinatorError(
        "required_plugin_root_missing",
        `required plugin root ${root.rootPath} does not exist`,
      );
    }
    return [];
  }
  const rootRealPath = await realpath(root.rootPath);
  assertContained(workspaceRoot, rootRealPath, "plugin root");
  const output: PluginSourceCandidate[] = [];
  const queue: Array<{ path: string; depth: number }> = [{
    path: rootRealPath,
    depth: 0,
  }];
  const seenDirectories = new Set<string>();
  while (queue.length) {
    const current = queue.shift()!;
    const directoryRealPath = await realpath(current.path);
    assertContained(workspaceRoot, directoryRealPath, "plugin directory");
    if (seenDirectories.has(directoryRealPath.toLowerCase())) {
      continue;
    }
    seenDirectories.add(directoryRealPath.toLowerCase());
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
        if (
          manifestDirectories.has(entry.name)
          || root.recursive && current.depth < root.maximumDepth
        ) {
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
      const parentName = dirname(path).split(/[\\/]/).pop() ?? "";
      const accepted = manifestNames.has(entry.name)
        || manifestDirectories.has(parentName) && entry.name === "plugin.json";
      if (!accepted) {
        continue;
      }
      const realManifestPath = await realpath(path);
      assertContained(workspaceRoot, realManifestPath, "plugin manifest");
      const metadata = await stat(realManifestPath);
      const relativePath = relative(rootRealPath, realManifestPath).replace(/\\/g, "/");
      const base = {
        rootId: root.rootId,
        rootPath: rootRealPath,
        manifestPath: path,
        realManifestPath,
        sourceKind: root.sourceKind,
        priority: root.priority,
        modifiedAtMs: metadata.mtimeMs,
        sizeBytes: metadata.size,
        pathDigest: digest({
          root_id: root.rootId,
          relative_path: relativePath,
          real_path: realManifestPath,
        }),
        metadata: canonicalize({
          ...root.metadata,
          relative_path: relativePath,
          depth: current.depth,
        }) as JsonObject,
      };
      output.push({
        candidateId: deterministicId("plugin-source-candidate", {
          root_id: root.rootId,
          real_manifest_path: realManifestPath,
        }, 40),
        ...base,
      });
    }
  }
  return output.sort(compareCandidate);
}

function resolveCandidateConflicts(
  values: Array<{
    candidate: PluginSourceCandidate;
    manifest: PluginManifest;
    supply: PluginSupplyChainReceipt;
  }>,
): typeof values {
  const groups = new Map<string, typeof values>();
  for (const value of values) {
    const group = groups.get(value.manifest.pluginId) ?? [];
    group.push(value);
    groups.set(value.manifest.pluginId, group);
  }
  const output: typeof values = [];
  for (const [pluginId, group] of groups) {
    group.sort((left, right) => {
      return compareCandidate(left.candidate, right.candidate)
        || compareVersions(right.manifest.version, left.manifest.version)
        || left.manifest.manifestDigest.localeCompare(right.manifest.manifestDigest);
    });
    const winner = group[0];
    if (!winner) {
      throw pluginCoordinatorError(
        "plugin_candidate_group_empty",
        `plugin candidate group ${pluginId} is empty`,
      );
    }
    output.push(winner);
  }
  return output.sort((left, right) => left.manifest.pluginId.localeCompare(right.manifest.pluginId));
}

function normalizeRoot(
  value: PluginSourceRoot,
  workspaceRoot: string,
): PluginSourceRoot {
  if (!value.rootId) {
    throw pluginCoordinatorError(
      "plugin_root_id_missing",
      "plugin source root requires an id",
    );
  }
  const rootPath = resolve(workspaceRoot, value.rootPath);
  assertContained(workspaceRoot, rootPath, "plugin source root");
  if (!Number.isSafeInteger(value.priority)) {
    throw pluginCoordinatorError(
      "plugin_root_priority_invalid",
      `plugin root ${value.rootId} priority must be an integer`,
    );
  }
  if (
    !Number.isSafeInteger(value.maximumDepth)
    || value.maximumDepth < 0
    || value.maximumDepth > 32
  ) {
    throw pluginCoordinatorError(
      "plugin_root_depth_invalid",
      `plugin root ${value.rootId} maximum depth must be between 0 and 32`,
    );
  }
  return {
    ...cloneJson(value),
    rootPath,
  };
}

function compareRoots(
  left: PluginSourceRoot,
  right: PluginSourceRoot,
): number {
  return right.priority - left.priority
    || left.rootId.localeCompare(right.rootId);
}

function compareCandidate(
  left: PluginSourceCandidate,
  right: PluginSourceCandidate,
): number {
  return right.priority - left.priority
    || left.rootId.localeCompare(right.rootId)
    || left.realManifestPath.localeCompare(right.realManifestPath);
}

function compareVersions(left: string, right: string): number {
  const leftParts = left.split(/[.+-]/).slice(0, 3).map(Number);
  const rightParts = right.split(/[.+-]/).slice(0, 3).map(Number);
  for (let index = 0; index < 3; index += 1) {
    const difference = (leftParts[index] || 0) - (rightParts[index] || 0);
    if (difference) {
      return difference;
    }
  }
  return left.localeCompare(right);
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
    throw pluginCoordinatorError(
      "plugin_path_outside_workspace",
      `${label} ${path} is outside workspace ${workspaceRoot}`,
    );
  }
}

function projectPluginRecord(record: PluginRuntimeRecord): JsonObject {
  return {
    plugin_id: record.pluginId,
    name: record.manifest.name,
    version: record.manifest.version,
    source_kind: record.manifest.sourceKind,
    revision: record.revision,
    revision_id: record.revisionId,
    status: record.status,
    manifest_digest: record.manifest.manifestDigest,
    capability_digest: record.capabilityDigest,
    active_invocations: record.activeInvocations,
    pending_reload_revision: record.pendingReloadRevision,
    loaded_at: record.loadedAt,
    failure: record.failure,
    capability_counts: {
      skills: record.manifest.skillRoots.length,
      commands: record.manifest.commands.length,
      hooks: record.manifest.hooks.length,
      agents: record.manifest.agents.length,
      mcp_servers: record.manifest.mcpServers.length,
    },
  };
}

function pluginTool(
  name: string,
  purpose: string,
  properties: JsonObject,
  accessMode: string,
  required: string[] = [],
): ToolSpecContract {
  return {
    name,
    purpose,
    source: "typescript-plugin",
    input_schema: {
      type: "object",
      properties,
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
      namespace: "plugin",
      server_id: "",
      version: "1",
      source: "zyra-e02-plugin-coordinator",
    },
  };
}

function pluginExecution(
  summary: string,
  output: JsonObject,
  metadata: Record<string, string> = {},
): PluginCoordinatorExecution {
  return {
    summary,
    output,
    metadata: {
      canonical_runtime_owner: "typescript",
      capability_owner: "typescript-plugin",
      python_plugin_fallback: "false",
      ...metadata,
    },
  };
}

function requiredString(value: JsonObject, key: string): string {
  const item = value[key];
  if (typeof item !== "string" || !item.trim()) {
    throw pluginCoordinatorError(
      "plugin_argument_missing",
      `plugin argument ${key} is required`,
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

function errorCode(error: unknown): string {
  if (error && typeof error === "object") {
    const value = (error as { code?: unknown }).code;
    if (typeof value === "string" && value) {
      return value;
    }
  }
  return error instanceof Error && error.name
    ? error.name
    : "plugin_runtime_failed";
}

function pluginCoordinatorError(
  code: string,
  message: string,
  details: JsonObject = {},
): Error {
  const error = new Error(message);
  error.name = "PluginCoordinatorError";
  Object.assign(error, {
    code,
    details: canonicalize(details),
  });
  return error;
}

function marketplaceVerifier(
  keys: Record<string, string>,
): (keyId: string, payloadDigest: string, signature: string) => boolean {
  return (keyId, payloadDigest, signature) => {
    const key = keys[keyId];
    if (!key) {
      return false;
    }
    const expected = createHmac("sha256", key)
      .update(payloadDigest)
      .digest();
    let observed: Buffer;
    try {
      observed = Buffer.from(signature, /^[0-9a-f]{64}$/i.test(signature) ? "hex" : "base64");
    } catch {
      return false;
    }
    return observed.length === expected.length
      && timingSafeEqual(observed, expected);
  };
}
