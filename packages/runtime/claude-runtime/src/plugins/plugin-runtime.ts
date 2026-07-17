import { readFile } from "node:fs/promises";

import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type {
  PluginLoadResult,
  PluginManifest,
  PluginRevision,
  PluginRuntimeRecord,
  PluginRuntimeSnapshot,
  PluginSourceKind,
} from "./contracts.ts";
import { PluginCacheRuntime } from "./cache-runtime.ts";
import { PluginManifestRuntime } from "./manifest-runtime.ts";

export interface PluginRuntimeIntegration {
  addSkills(manifest: PluginManifest): Promise<JsonObject>;
  addCommands(manifest: PluginManifest): Promise<JsonObject>;
  addHooks(manifest: PluginManifest): Promise<JsonObject>;
  addAgents(manifest: PluginManifest): Promise<JsonObject>;
  addMcpServers(manifest: PluginManifest): Promise<JsonObject>;
  remove(pluginId: string, revision: number): Promise<void>;
}

export class PluginRuntime {
  private readonly manifests: PluginManifestRuntime;
  private readonly cache: PluginCacheRuntime;
  private readonly integration: PluginRuntimeIntegration;
  private readonly now: () => Date;
  private readonly records = new Map<string, PluginRuntimeRecord>();
  private readonly revisions = new Map<number, PluginRevision>();
  private readonly pins = new Map<string, number>();
  private revision = 0;
  private headRevisionId: string | null = null;
  private lastTimestamp: string | null = null;

  constructor(options: {
    manifests: PluginManifestRuntime;
    cache: PluginCacheRuntime;
    integration: PluginRuntimeIntegration;
    now?: () => Date;
    snapshot?: PluginRuntimeSnapshot | null;
  }) {
    this.manifests = options.manifests;
    this.cache = options.cache;
    this.integration = options.integration;
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  async load(
    manifestPath: string,
    sourceKind: PluginSourceKind,
    overrides: JsonObject = {},
  ): Promise<PluginLoadResult> {
    const manifest = await this.manifests.parse(manifestPath, sourceKind, overrides);
    const existing = this.records.get(manifest.pluginId);
    if (existing && existing.manifest.manifestDigest === manifest.manifestDigest && existing.status === "active") {
      return loadResult(existing);
    }
    if (existing) return this.reload(manifest.pluginId, manifestPath, sourceKind, overrides);
    validateDependencies(manifest, this.records);
    const revision = this.revision + 1;
    const revisionId = deterministicId("plugin-revision", {
      parent_revision_id: this.headRevisionId,
      revision,
      plugin_id: manifest.pluginId,
      manifest_digest: manifest.manifestDigest,
    }, 32);
    const record: PluginRuntimeRecord = {
      pluginId: manifest.pluginId,
      revision,
      revisionId,
      manifest,
      status: manifest.enabled ? "loading" : "disabled",
      loadedAt: null,
      unloadedAt: null,
      activeInvocations: 0,
      pendingReloadRevision: null,
      capabilityDigest: "",
      failure: null,
      metadata: {},
    };
    this.records.set(manifest.pluginId, record);
    if (!manifest.enabled) {
      this.commitRevision([manifest], [], [], [], [], { action: "load_disabled" });
      return loadResult(record);
    }
    try {
      const compiled = await this.integrate(manifest);
      record.status = "active";
      record.loadedAt = this.timestamp();
      record.capabilityDigest = digest(compiled);
      record.metadata = { compiled_capabilities: compiled };
      this.cache.commit({
        manifest,
        sourceDigest: await sourceDigest(manifest),
        compiledCapabilities: compiled,
        status: "active",
      });
      this.commitRevision([manifest], [manifest.pluginId], [], [], [], { action: "load" });
      return loadResult(record);
    } catch (error) {
      record.status = "failed";
      record.failure = {
        name: error instanceof Error ? error.name : "Error",
        message: error instanceof Error ? error.message : String(error),
      };
      this.cache.commit({
        manifest,
        sourceDigest: await sourceDigest(manifest),
        compiledCapabilities: {},
        status: "failed",
        error: record.failure,
      });
      this.commitRevision([manifest], [], [], [], [manifest.pluginId], { action: "load_failed" });
      throw error;
    }
  }

  async reload(
    pluginId: string,
    manifestPath: string,
    sourceKind: PluginSourceKind,
    overrides: JsonObject = {},
  ): Promise<PluginLoadResult> {
    const current = this.records.get(pluginId);
    if (!current) return this.load(manifestPath, sourceKind, overrides);
    const manifest = await this.manifests.parse(manifestPath, sourceKind, overrides);
    if (manifest.pluginId !== pluginId) throw new Error(`plugin reload id ${manifest.pluginId} does not match ${pluginId}`);
    validateDependencies(manifest, this.records);
    const targetRevision = this.revision + 1;
    current.pendingReloadRevision = targetRevision;
    const oldRecord = cloneJson(current);
    const staged: PluginRuntimeRecord = {
      ...cloneJson(current),
      revision: targetRevision,
      revisionId: deterministicId("plugin-revision", {
        parent_revision_id: this.headRevisionId,
        revision: targetRevision,
        plugin_id: pluginId,
        manifest_digest: manifest.manifestDigest,
      }, 32),
      manifest,
      status: manifest.enabled ? "loading" : "disabled",
      loadedAt: null,
      unloadedAt: null,
      pendingReloadRevision: null,
      capabilityDigest: "",
      failure: null,
      metadata: {},
    };
    try {
      const compiled = manifest.enabled ? await this.integrate(manifest) : {};
      staged.status = manifest.enabled ? "active" : "disabled";
      staged.loadedAt = manifest.enabled ? this.timestamp() : null;
      staged.capabilityDigest = digest(compiled);
      staged.metadata = { compiled_capabilities: compiled, replaced_revision: oldRecord.revision };
      this.records.set(pluginId, staged);
      current.pendingReloadRevision = null;
      this.cache.commit({
        manifest,
        sourceDigest: await sourceDigest(manifest),
        compiledCapabilities: compiled,
        status: staged.status,
      });
      this.commitRevision([manifest], [], [pluginId], [], [], { action: "reload", prior_revision: oldRecord.revision });
      return loadResult(staged);
    } catch (error) {
      current.pendingReloadRevision = null;
      this.records.set(pluginId, current);
      throw error;
    }
  }

  async unload(pluginId: string, reason = "requested"): Promise<boolean> {
    const record = this.records.get(pluginId);
    if (!record) return false;
    record.status = "unloading";
    if ((this.pins.get(pinKey(pluginId, record.revision)) ?? 0) > 0) {
      record.metadata = { ...record.metadata, unload_pending: true, unload_reason: reason };
      return false;
    }
    await this.integration.remove(pluginId, record.revision);
    record.status = "disabled";
    record.unloadedAt = this.timestamp();
    this.records.delete(pluginId);
    this.cache.invalidate(pluginId);
    this.commitRevision([], [], [], [pluginId], [], { action: "unload", reason });
    return true;
  }

  acquire(pluginId: string): { record: PluginRuntimeRecord; release(): void } {
    const record = this.records.get(pluginId);
    if (!record || record.status !== "active") throw new Error(`plugin ${pluginId} is not active`);
    const key = pinKey(pluginId, record.revision);
    this.pins.set(key, (this.pins.get(key) ?? 0) + 1);
    record.activeInvocations += 1;
    let released = false;
    return {
      record: cloneJson(record),
      release: () => {
        if (released) return;
        released = true;
        const count = this.pins.get(key) ?? 0;
        if (count <= 1) this.pins.delete(key);
        else this.pins.set(key, count - 1);
        const current = this.records.get(pluginId);
        if (current?.revision === record.revision) current.activeInvocations = Math.max(0, current.activeInvocations - 1);
        if (record.metadata.unload_pending === true && count <= 1) void this.integration.remove(pluginId, record.revision);
      },
    };
  }

  get(pluginId: string): PluginRuntimeRecord | null {
    const value = this.records.get(pluginId);
    return value ? cloneJson(value) : null;
  }

  list(): PluginRuntimeRecord[] {
    return [...this.records.values()].sort((left, right) => left.pluginId.localeCompare(right.pluginId)).map(cloneJson);
  }

  snapshot(): PluginRuntimeSnapshot {
    const withoutDigest = {
      version: "zyra.plugin-runtime/v2" as const,
      revision: this.revision,
      headRevisionId: this.headRevisionId,
      records: this.list(),
      revisions: [...this.revisions.values()].sort((left, right) => left.revision - right.revision).map(cloneJson),
      cache: this.cache.list(),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: PluginRuntimeSnapshot): void {
    if (snapshot.version !== "zyra.plugin-runtime/v2") throw new Error("unsupported plugin runtime snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw new Error("plugin runtime snapshot digest mismatch");
    this.records.clear();
    this.revisions.clear();
    this.revision = snapshot.revision;
    this.headRevisionId = snapshot.headRevisionId;
    for (const record of snapshot.records) {
      const restored = cloneJson(record);
      if (restored.status === "loading" || restored.status === "unloading") {
        restored.status = "failed";
        restored.failure = { code: "plugin_restart_interrupted_transition", prior_status: record.status };
      }
      restored.activeInvocations = 0;
      this.records.set(restored.pluginId, restored);
    }
    for (const revision of snapshot.revisions) this.revisions.set(revision.revision, cloneJson(revision));
  }

  private async integrate(manifest: PluginManifest): Promise<JsonObject> {
    const completed: string[] = [];
    try {
      const skills = await this.integration.addSkills(manifest);
      completed.push("skills");
      const commands = await this.integration.addCommands(manifest);
      completed.push("commands");
      const hooks = await this.integration.addHooks(manifest);
      completed.push("hooks");
      const agents = await this.integration.addAgents(manifest);
      completed.push("agents");
      const mcp = await this.integration.addMcpServers(manifest);
      completed.push("mcp");
      return { skills, commands, hooks, agents, mcp };
    } catch (error) {
      try {
        await this.integration.remove(manifest.pluginId, 0);
      } catch (rollbackError) {
        throw Object.assign(new Error(
          `plugin ${manifest.pluginId} integration failed after ${completed.join(", ")}; rollback also failed: ${rollbackError instanceof Error ? rollbackError.message : String(rollbackError)}`,
        ), {
          name: "PluginIntegrationRollbackError",
          code: "plugin_integration_rollback_failed",
          cause: error,
          completed,
          rollbackError,
        });
      }
      throw error;
    }
  }

  private async deferRemoval(record: PluginRuntimeRecord): Promise<void> {
    const key = pinKey(record.pluginId, record.revision);
    if ((this.pins.get(key) ?? 0) === 0) await this.integration.remove(record.pluginId, record.revision);
  }

  private commitRevision(
    manifests: PluginManifest[],
    added: string[],
    updated: string[],
    removed: string[],
    failed: string[],
    metadata: JsonObject,
  ): PluginRevision {
    this.revision += 1;
    const committedAt = this.timestamp();
    const base = {
      revision: this.revision,
      parentRevisionId: this.headRevisionId,
      manifests: manifests.map(cloneJson),
      cacheEntries: this.cache.list(),
      added: [...added].sort(),
      updated: [...updated].sort(),
      removed: [...removed].sort(),
      failed: [...failed].sort(),
      committedAt,
      metadata: cloneJson(metadata),
    };
    const revisionId = deterministicId("plugin-runtime-revision", base, 32);
    const record: PluginRevision = { revisionId, ...base, digest: digest({ revisionId, ...base }) };
    this.headRevisionId = revisionId;
    this.revisions.set(record.revision, record);
    return cloneJson(record);
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function loadResult(record: PluginRuntimeRecord): PluginLoadResult {
  return {
    pluginId: record.pluginId,
    revision: record.revision,
    revisionId: record.revisionId,
    status: record.status,
    manifestDigest: record.manifest.manifestDigest,
    capabilityDigest: record.capabilityDigest,
    skillRoots: record.manifest.skillRoots.map(cloneJson),
    commands: record.manifest.commands.map(cloneJson),
    hooks: record.manifest.hooks.map(cloneJson),
    agents: record.manifest.agents.map(cloneJson),
    mcpServers: record.manifest.mcpServers.map(cloneJson),
    metadata: cloneJson(record.metadata),
  };
}

function validateDependencies(manifest: PluginManifest, records: Map<string, PluginRuntimeRecord>): void {
  for (const dependency of manifest.dependencies) {
    const record = records.get(dependency.pluginId);
    if (!record && !dependency.optional) throw new Error(`plugin ${manifest.pluginId} requires ${dependency.pluginId}`);
    if (record && record.status !== "active" && !dependency.optional) throw new Error(`plugin dependency ${dependency.pluginId} is ${record.status}`);
  }
}

async function sourceDigest(manifest: PluginManifest): Promise<string> {
  const parts: JsonObject = { manifest_digest: manifest.manifestDigest };
  for (const command of manifest.commands) {
    try { parts[`command:${command.name}`] = digest(await readFile(command.path, "utf8")); } catch { parts[`command:${command.name}`] = "missing"; }
  }
  for (const hook of manifest.hooks) {
    if (!hook.path) continue;
    try { parts[`hook:${hook.hookId}`] = digest(await readFile(hook.path, "utf8")); } catch { parts[`hook:${hook.hookId}`] = "missing"; }
  }
  return digest(parts);
}

function pinKey(pluginId: string, revision: number): string {
  return `${pluginId}\0${revision}`;
}
