import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import { satisfiesVersion } from "./dependency-runtime.ts";

export interface PluginMarketplaceEntry {
  entryId: string;
  marketplaceId: string;
  pluginId: string;
  name: string;
  version: string;
  description: string;
  artifactUri: string;
  artifactDigest: string;
  manifestDigest: string;
  publishedAt: string;
  minimumZyraVersion: string | null;
  yanked: boolean;
  signature: string | null;
  signingKeyId: string | null;
  metadata: JsonObject;
}

export interface PluginMarketplaceIndex {
  marketplaceId: string;
  revision: number;
  entries: PluginMarketplaceEntry[];
  signature: string | null;
  signingKeyId: string | null;
  fetchedAt: string;
  expiresAt: string | null;
  sourceDigest: string;
  metadata: JsonObject;
}

export interface PluginMarketplaceResolution {
  resolutionId: string;
  marketplaceId: string;
  pluginId: string;
  requestedRange: string;
  selected: PluginMarketplaceEntry;
  candidates: string[];
  indexRevision: number;
  resolvedAt: string;
  digest: string;
  metadata: JsonObject;
}

export interface PluginMarketplaceSnapshot {
  version: "zyra.plugin-marketplace-runtime/v1";
  revision: number;
  indexes: PluginMarketplaceIndex[];
  resolutions: PluginMarketplaceResolution[];
  digest: string;
  capturedAt: string;
}

export class PluginMarketplaceRuntime {
  private readonly indexes = new Map<string, PluginMarketplaceIndex>();
  private readonly resolutions = new Map<string, PluginMarketplaceResolution>();
  private readonly now: () => Date;
  private readonly verifySignature: (keyId: string, payloadDigest: string, signature: string) => boolean;
  private readonly allowedArtifactSchemes: Set<string>;
  private readonly maximumEntries: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: {
    verifySignature?: (keyId: string, payloadDigest: string, signature: string) => boolean;
    allowedArtifactSchemes?: string[];
    maximumEntries?: number;
    now?: () => Date;
    snapshot?: PluginMarketplaceSnapshot | null;
  } = {}) {
    this.verifySignature = options.verifySignature ?? (() => false);
    this.allowedArtifactSchemes = new Set(options.allowedArtifactSchemes ?? ["file:", "https:"]);
    this.maximumEntries = options.maximumEntries ?? 100_000;
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  commit(indexValue: PluginMarketplaceIndex, expectedRevision = this.revision): PluginMarketplaceIndex {
    if (expectedRevision !== this.revision) throw marketplaceError("marketplace_revision_conflict", `marketplace revision ${expectedRevision} does not match ${this.revision}`);
    const index = normalizeIndex(indexValue, this.maximumEntries, this.allowedArtifactSchemes);
    const prior = this.indexes.get(index.marketplaceId);
    if (prior && index.revision <= prior.revision) throw marketplaceError("marketplace_index_stale", `marketplace ${index.marketplaceId} revision ${index.revision} is not newer than ${prior.revision}`);
    if (index.signature && index.signingKeyId) {
      const payloadDigest = digest({ ...index, signature: null });
      if (!this.verifySignature(index.signingKeyId, payloadDigest, index.signature)) throw marketplaceError("marketplace_signature_invalid", `marketplace ${index.marketplaceId} signature is invalid`);
    } else if (index.entries.some((entry) => entry.artifactUri.startsWith("https:"))) {
      throw marketplaceError("marketplace_signature_required", `remote marketplace ${index.marketplaceId} must be signed`);
    }
    this.indexes.set(index.marketplaceId, index);
    this.revision += 1;
    return cloneJson(index);
  }

  resolve(marketplaceId: string, pluginId: string, versionRange = "*", zyraVersion = "0.1.0", metadata: JsonObject = {}): PluginMarketplaceResolution {
    const index = this.indexes.get(marketplaceId);
    if (!index) throw marketplaceError("marketplace_not_found", `marketplace ${marketplaceId} was not found`);
    if (index.expiresAt && Date.parse(index.expiresAt) <= this.now().getTime()) throw marketplaceError("marketplace_index_expired", `marketplace ${marketplaceId} index expired`);
    const candidates = index.entries
      .filter((entry) => entry.pluginId === pluginId && !entry.yanked)
      .filter((entry) => satisfiesVersion(entry.version, versionRange))
      .filter((entry) => !entry.minimumZyraVersion || satisfiesVersion(zyraVersion, `>=${entry.minimumZyraVersion}`))
      .sort((left, right) => compareSemver(right.version, left.version) || right.publishedAt.localeCompare(left.publishedAt));
    const selected = candidates[0];
    if (!selected) throw marketplaceError("marketplace_plugin_unresolved", `plugin ${pluginId}@${versionRange} was not found in ${marketplaceId}`);
    const base = {
      marketplaceId,
      pluginId,
      requestedRange: versionRange,
      selected: cloneJson(selected),
      candidates: candidates.map((entry) => entry.entryId),
      indexRevision: index.revision,
      resolvedAt: this.timestamp(),
      metadata: cloneJson(metadata),
    };
    const resolutionId = deterministicId("plugin-marketplace-resolution", base, 40);
    const resolution: PluginMarketplaceResolution = { resolutionId, ...base, digest: digest({ resolutionId, ...base }) };
    this.resolutions.set(resolutionId, resolution);
    return cloneJson(resolution);
  }

  verifyArtifact(resolutionId: string, bytes: Uint8Array): PluginMarketplaceResolution {
    const resolution = this.resolutions.get(resolutionId);
    if (!resolution) throw marketplaceError("marketplace_resolution_not_found", `marketplace resolution ${resolutionId} was not found`);
    const actual = digest(Buffer.from(bytes).toString("base64"));
    if (actual !== resolution.selected.artifactDigest) throw marketplaceError("plugin_artifact_digest_mismatch", `plugin artifact ${resolution.selected.pluginId} digest mismatch`);
    if (resolution.selected.signature && resolution.selected.signingKeyId) {
      if (!this.verifySignature(resolution.selected.signingKeyId, actual, resolution.selected.signature)) throw marketplaceError("plugin_artifact_signature_invalid", `plugin artifact ${resolution.selected.pluginId} signature is invalid`);
    }
    return cloneJson(resolution);
  }

  list(marketplaceId?: string): PluginMarketplaceEntry[] {
    return [...this.indexes.values()].filter((index) => !marketplaceId || index.marketplaceId === marketplaceId).flatMap((index) => index.entries.map(cloneJson)).sort((left, right) => left.pluginId.localeCompare(right.pluginId) || compareSemver(right.version, left.version));
  }

  snapshot(): PluginMarketplaceSnapshot {
    const withoutDigest = {
      version: "zyra.plugin-marketplace-runtime/v1" as const,
      revision: this.revision,
      indexes: [...this.indexes.values()].sort((left, right) => left.marketplaceId.localeCompare(right.marketplaceId)).map(cloneJson),
      resolutions: [...this.resolutions.values()].sort((left, right) => left.resolvedAt.localeCompare(right.resolvedAt)).map(cloneJson),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: PluginMarketplaceSnapshot): void {
    if (snapshot.version !== "zyra.plugin-marketplace-runtime/v1") throw marketplaceError("unsupported_marketplace_snapshot", "unsupported plugin marketplace snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw marketplaceError("marketplace_snapshot_digest_mismatch", "plugin marketplace snapshot digest mismatch");
    this.indexes.clear();
    this.resolutions.clear();
    this.revision = snapshot.revision;
    for (const index of snapshot.indexes) this.indexes.set(index.marketplaceId, cloneJson(index));
    for (const resolution of snapshot.resolutions) this.resolutions.set(resolution.resolutionId, cloneJson(resolution));
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function normalizeIndex(value: PluginMarketplaceIndex, maximumEntries: number, schemes: Set<string>): PluginMarketplaceIndex {
  const index = cloneJson(value);
  if (!index.marketplaceId || !Number.isSafeInteger(index.revision) || index.revision < 1) throw marketplaceError("marketplace_index_invalid", "marketplace identity or revision is invalid");
  if (index.entries.length > maximumEntries) throw marketplaceError("marketplace_index_oversized", `marketplace contains ${index.entries.length} entries`);
  const ids = new Set<string>();
  for (const entry of index.entries) {
    const expectedId = deterministicId("plugin-marketplace-entry", {
      marketplace_id: index.marketplaceId,
      plugin_id: entry.pluginId,
      version: entry.version,
      artifact_digest: entry.artifactDigest,
      manifest_digest: entry.manifestDigest,
    }, 32);
    if (entry.entryId !== expectedId) throw marketplaceError("marketplace_entry_id_invalid", `marketplace entry ${entry.pluginId}@${entry.version} id mismatch`);
    if (ids.has(entry.entryId)) throw marketplaceError("marketplace_entry_duplicate", `duplicate marketplace entry ${entry.entryId}`);
    ids.add(entry.entryId);
    let url: URL;
    try { url = new URL(entry.artifactUri); } catch { throw marketplaceError("marketplace_artifact_uri_invalid", `invalid artifact URI ${entry.artifactUri}`); }
    if (!schemes.has(url.protocol)) throw marketplaceError("marketplace_artifact_scheme_denied", `artifact scheme ${url.protocol} is denied`);
  }
  index.entries.sort((left, right) => left.pluginId.localeCompare(right.pluginId) || compareSemver(right.version, left.version));
  index.sourceDigest = digest(index.entries);
  return index;
}

function compareSemver(left: string, right: string): number {
  const parse = (value: string): number[] => (value.match(/\d+/g) ?? ["0"]).slice(0, 3).map(Number);
  const a = parse(left);
  const b = parse(right);
  for (let index = 0; index < 3; index += 1) if ((a[index] ?? 0) !== (b[index] ?? 0)) return (a[index] ?? 0) - (b[index] ?? 0);
  return left.localeCompare(right);
}

function marketplaceError(code: string, message: string): Error {
  return Object.assign(new Error(message), { name: "PluginMarketplaceError", code });
}
