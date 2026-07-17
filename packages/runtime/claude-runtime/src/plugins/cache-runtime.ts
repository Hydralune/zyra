import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type { PluginCacheEntry, PluginManifest, PluginStatus } from "./contracts.ts";

export interface PluginCacheSnapshot {
  version: "zyra.plugin-cache/v2";
  revision: number;
  entries: PluginCacheEntry[];
  digest: string;
  capturedAt: string;
}

export class PluginCacheRuntime {
  private readonly entries = new Map<string, PluginCacheEntry>();
  private readonly byPlugin = new Map<string, string>();
  private readonly now: () => Date;
  private readonly maximumEntries: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumEntries?: number; snapshot?: PluginCacheSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumEntries = options.maximumEntries ?? 1_000;
    if (options.snapshot) this.restore(options.snapshot);
  }

  commit(input: {
    manifest: PluginManifest;
    sourceDigest: string;
    compiledCapabilities: JsonObject;
    status: PluginStatus;
    error?: JsonObject | null;
    expectedRevision?: number | null;
    metadata?: JsonObject;
  }): PluginCacheEntry {
    const cacheKey = deterministicId("plugin-cache", {
      plugin_id: input.manifest.pluginId,
      version: input.manifest.version,
      manifest_digest: input.manifest.manifestDigest,
      source_digest: input.sourceDigest,
    }, 40);
    const existingKey = this.byPlugin.get(input.manifest.pluginId);
    const existing = existingKey ? this.entries.get(existingKey) : null;
    if (input.expectedRevision !== undefined && input.expectedRevision !== null && input.expectedRevision !== (existing?.revision ?? 0)) {
      throw new Error(`plugin cache revision ${input.expectedRevision} does not match ${existing?.revision ?? 0}`);
    }
    const timestamp = this.timestamp();
    const entry: PluginCacheEntry = {
      cacheKey,
      pluginId: input.manifest.pluginId,
      pluginVersion: input.manifest.version,
      manifestDigest: input.manifest.manifestDigest,
      sourceDigest: input.sourceDigest,
      revision: (existing?.revision ?? 0) + 1,
      status: input.status,
      compiledCapabilities: cloneJson(input.compiledCapabilities),
      error: input.error ? cloneJson(input.error) : null,
      createdAt: existing?.createdAt ?? timestamp,
      updatedAt: timestamp,
      lastUsedAt: timestamp,
      useCount: existing?.useCount ?? 0,
      metadata: cloneJson(input.metadata ?? {}),
    };
    if (existingKey && existingKey !== cacheKey) this.entries.delete(existingKey);
    this.entries.set(cacheKey, entry);
    this.byPlugin.set(input.manifest.pluginId, cacheKey);
    this.revision += 1;
    this.evict();
    return cloneJson(entry);
  }

  get(pluginId: string, manifestDigest?: string): PluginCacheEntry | null {
    const key = this.byPlugin.get(pluginId);
    const entry = key ? this.entries.get(key) : null;
    if (!entry || (manifestDigest && entry.manifestDigest !== manifestDigest)) return null;
    entry.lastUsedAt = this.timestamp();
    entry.useCount += 1;
    return cloneJson(entry);
  }

  invalidate(pluginId: string): boolean {
    const key = this.byPlugin.get(pluginId);
    if (!key) return false;
    this.byPlugin.delete(pluginId);
    this.entries.delete(key);
    this.revision += 1;
    return true;
  }

  list(): PluginCacheEntry[] {
    return [...this.entries.values()].sort((left, right) => left.pluginId.localeCompare(right.pluginId)).map(cloneJson);
  }

  snapshot(): PluginCacheSnapshot {
    const withoutDigest = {
      version: "zyra.plugin-cache/v2" as const,
      revision: this.revision,
      entries: this.list(),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: PluginCacheSnapshot): void {
    if (snapshot.version !== "zyra.plugin-cache/v2") throw new Error("unsupported plugin cache snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw new Error("plugin cache snapshot digest mismatch");
    this.entries.clear();
    this.byPlugin.clear();
    this.revision = snapshot.revision;
    for (const entry of snapshot.entries) {
      this.entries.set(entry.cacheKey, cloneJson(entry));
      this.byPlugin.set(entry.pluginId, entry.cacheKey);
    }
  }

  private evict(): void {
    const candidates = [...this.entries.values()].sort((left, right) => left.lastUsedAt.localeCompare(right.lastUsedAt));
    while (this.entries.size > this.maximumEntries && candidates.length) {
      const entry = candidates.shift()!;
      this.entries.delete(entry.cacheKey);
      if (this.byPlugin.get(entry.pluginId) === entry.cacheKey) this.byPlugin.delete(entry.pluginId);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}
