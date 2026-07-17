import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type { PluginManifest } from "./contracts.ts";

export type PluginRegisteredCapabilityKind = "skill" | "command" | "hook" | "agent" | "mcp_server";

export interface PluginCapabilityClaim {
  claimId: string;
  pluginId: string;
  pluginRevision: number;
  manifestDigest: string;
  kind: PluginRegisteredCapabilityKind;
  capabilityId: string;
  permissionScope: string[];
  enabled: boolean;
  contentDigest: string;
  registeredAt: string;
  metadata: JsonObject;
}

export interface PluginCapabilityRevision {
  revisionId: string;
  revisionBefore: number;
  revisionAfter: number;
  claims: PluginCapabilityClaim[];
  added: string[];
  updated: string[];
  removed: string[];
  committedAt: string;
  digest: string;
  metadata: JsonObject;
}

export interface PluginCapabilityLease {
  leaseId: string;
  claimId: string;
  pluginId: string;
  pluginRevision: number;
  registryRevision: number;
  invocationId: string;
  acquiredAt: string;
  releasedAt: string | null;
  metadata: JsonObject;
}

export interface PluginCapabilitySnapshot {
  version: "zyra.plugin-capability-runtime/v1";
  revision: number;
  claims: PluginCapabilityClaim[];
  revisions: PluginCapabilityRevision[];
  leases: PluginCapabilityLease[];
  digest: string;
  capturedAt: string;
}

export class PluginCapabilityRuntime {
  private readonly claims = new Map<string, PluginCapabilityClaim>();
  private readonly revisions = new Map<number, PluginCapabilityRevision>();
  private readonly leases = new Map<string, PluginCapabilityLease>();
  private readonly pins = new Map<string, number>();
  private readonly now: () => Date;
  private readonly maximumRevisions: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumRevisions?: number; snapshot?: PluginCapabilitySnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumRevisions = options.maximumRevisions ?? 100;
    if (options.snapshot) this.restore(options.snapshot);
  }

  commit(manifestsValue: PluginManifest[], pluginRevisions: Record<string, number>, expectedRevision = this.revision, metadata: JsonObject = {}): PluginCapabilityRevision {
    if (expectedRevision !== this.revision) throw capabilityError("capability_revision_conflict", `plugin capability revision ${expectedRevision} does not match ${this.revision}`);
    const next = new Map<string, PluginCapabilityClaim>();
    for (const manifest of manifestsValue.map(cloneJson).sort((left, right) => left.pluginId.localeCompare(right.pluginId))) {
      const pluginRevision = pluginRevisions[manifest.pluginId];
      if (!Number.isSafeInteger(pluginRevision) || pluginRevision < 1) throw capabilityError("plugin_revision_missing", `plugin ${manifest.pluginId} lacks committed revision`);
      for (const claim of claimsFor(manifest, pluginRevision, this.timestamp())) {
        const key = claimKey(claim.kind, claim.capabilityId);
        const conflict = next.get(key);
        if (conflict) throw capabilityError("plugin_capability_conflict", `${claim.kind} ${claim.capabilityId} is claimed by ${conflict.pluginId} and ${claim.pluginId}`);
        next.set(key, claim);
      }
    }
    const added: string[] = [];
    const updated: string[] = [];
    const removed: string[] = [];
    for (const key of new Set([...this.claims.keys(), ...next.keys()])) {
      const before = this.claims.get(key);
      const after = next.get(key);
      if (!before && after) added.push(after.claimId);
      else if (before && !after) {
        if ((this.pins.get(before.claimId) ?? 0) > 0) next.set(key, before);
        else removed.push(before.claimId);
      } else if (before && after && before.contentDigest !== after.contentDigest) updated.push(after.claimId);
    }
    const committedAt = this.timestamp();
    const revisionBase = {
      revisionBefore: this.revision,
      revisionAfter: this.revision + 1,
      claims: [...next.values()].sort(compareClaim).map(cloneJson),
      added: added.sort(),
      updated: updated.sort(),
      removed: removed.sort(),
      committedAt,
      metadata: cloneJson(metadata),
    };
    const revisionId = deterministicId("plugin-capability-revision", revisionBase, 40);
    const revision: PluginCapabilityRevision = { revisionId, ...revisionBase, digest: digest({ revisionId, ...revisionBase }) };
    this.claims.clear();
    for (const [key, claim] of next) this.claims.set(key, claim);
    this.revision += 1;
    this.revisions.set(this.revision, revision);
    this.trim();
    return cloneJson(revision);
  }

  resolve(kind: PluginRegisteredCapabilityKind, capabilityId: string, revision = this.revision): PluginCapabilityClaim {
    const record = this.revisions.get(revision);
    if (!record) throw capabilityError("capability_revision_not_found", `plugin capability revision ${revision} was not found`);
    const claim = record.claims.find((value) => value.kind === kind && value.capabilityId === capabilityId);
    if (!claim) throw capabilityError("plugin_capability_not_found", `${kind} ${capabilityId} was not found at revision ${revision}`);
    if (!claim.enabled) throw capabilityError("plugin_capability_disabled", `${kind} ${capabilityId} is disabled`);
    return cloneJson(claim);
  }

  acquire(kind: PluginRegisteredCapabilityKind, capabilityId: string, invocationId: string, revision = this.revision, metadata: JsonObject = {}): { claim: PluginCapabilityClaim; lease: PluginCapabilityLease; release: () => void } {
    const claim = this.resolve(kind, capabilityId, revision);
    const acquiredAt = this.timestamp();
    const base = {
      claimId: claim.claimId,
      pluginId: claim.pluginId,
      pluginRevision: claim.pluginRevision,
      registryRevision: revision,
      invocationId,
      acquiredAt,
      releasedAt: null,
      metadata: cloneJson(metadata),
    };
    const lease: PluginCapabilityLease = { leaseId: deterministicId("plugin-capability-lease", base, 40), ...base };
    this.leases.set(lease.leaseId, lease);
    this.pins.set(claim.claimId, (this.pins.get(claim.claimId) ?? 0) + 1);
    let released = false;
    return {
      claim,
      lease: cloneJson(lease),
      release: () => {
        if (released) return;
        released = true;
        const current = this.leases.get(lease.leaseId);
        if (current) current.releasedAt = this.timestamp();
        const count = this.pins.get(claim.claimId) ?? 0;
        if (count <= 1) this.pins.delete(claim.claimId);
        else this.pins.set(claim.claimId, count - 1);
      },
    };
  }

  list(options: { revision?: number; pluginId?: string; kind?: PluginRegisteredCapabilityKind; enabled?: boolean } = {}): PluginCapabilityClaim[] {
    const record = this.revisions.get(options.revision ?? this.revision);
    if (!record) return [];
    return record.claims
      .filter((claim) => !options.pluginId || claim.pluginId === options.pluginId)
      .filter((claim) => !options.kind || claim.kind === options.kind)
      .filter((claim) => options.enabled === undefined || claim.enabled === options.enabled)
      .map(cloneJson);
  }

  snapshot(): PluginCapabilitySnapshot {
    const withoutDigest = {
      version: "zyra.plugin-capability-runtime/v1" as const,
      revision: this.revision,
      claims: [...this.claims.values()].sort(compareClaim).map(cloneJson),
      revisions: [...this.revisions.values()].sort((left, right) => left.revisionAfter - right.revisionAfter).map(cloneJson),
      leases: [...this.leases.values()].sort((left, right) => left.acquiredAt.localeCompare(right.acquiredAt)).map(cloneJson),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: PluginCapabilitySnapshot): void {
    if (snapshot.version !== "zyra.plugin-capability-runtime/v1") throw capabilityError("unsupported_capability_snapshot", "unsupported plugin capability snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw capabilityError("capability_snapshot_digest_mismatch", "plugin capability snapshot digest mismatch");
    this.claims.clear();
    this.revisions.clear();
    this.leases.clear();
    this.pins.clear();
    this.revision = snapshot.revision;
    for (const claim of snapshot.claims) this.claims.set(claimKey(claim.kind, claim.capabilityId), cloneJson(claim));
    for (const revision of snapshot.revisions) this.revisions.set(revision.revisionAfter, cloneJson(revision));
    for (const value of snapshot.leases) {
      const lease = cloneJson(value);
      if (!lease.releasedAt) {
        lease.releasedAt = snapshot.capturedAt;
        lease.metadata = { ...lease.metadata, restore_release: "process invocation no longer active" };
      }
      this.leases.set(lease.leaseId, lease);
    }
  }

  private trim(): void {
    while (this.revisions.size > this.maximumRevisions) {
      const oldest = [...this.revisions.keys()].sort((left, right) => left - right)[0];
      if (oldest === undefined || oldest === this.revision) break;
      if (this.list({ revision: oldest }).some((claim) => (this.pins.get(claim.claimId) ?? 0) > 0)) break;
      this.revisions.delete(oldest);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function claimsFor(manifest: PluginManifest, pluginRevision: number, registeredAt: string): PluginCapabilityClaim[] {
  const output: PluginCapabilityClaim[] = [];
  const add = (kind: PluginRegisteredCapabilityKind, capabilityId: string, value: JsonObject, permissionScope: string[], enabled = true): void => {
    const base = {
      pluginId: manifest.pluginId,
      pluginRevision,
      manifestDigest: manifest.manifestDigest,
      kind,
      capabilityId,
      permissionScope: [...new Set(permissionScope)].sort(),
      enabled: manifest.enabled && enabled,
      contentDigest: digest(value),
      registeredAt,
      metadata: { plugin_version: manifest.version },
    };
    output.push({ claimId: deterministicId("plugin-capability-claim", base, 40), ...base });
  };
  const manifestPermissions = permissionStrings(manifest.permissions);
  for (const skill of manifest.skillRoots) add("skill", `${manifest.pluginId}:${skill.sourceId}`, cloneJson(skill) as unknown as JsonObject, manifestPermissions);
  for (const command of manifest.commands) add("command", command.name, cloneJson(command) as unknown as JsonObject, [...manifestPermissions, ...(command.permission ? [command.permission] : [])]);
  for (const hook of manifest.hooks) add("hook", hook.hookId, cloneJson(hook) as unknown as JsonObject, manifestPermissions);
  for (const agent of manifest.agents) add("agent", agent.agentId, cloneJson(agent) as unknown as JsonObject, [...manifestPermissions, ...permissionStrings(agent.toolScope)]);
  for (const server of manifest.mcpServers) add("mcp_server", server.serverId, cloneJson(server) as unknown as JsonObject, [...manifestPermissions, ...permissionStrings(server.permissionScope)], server.enabledByDefault);
  return output;
}

function permissionStrings(value: JsonObject): string[] {
  const output: string[] = [];
  for (const [key, item] of Object.entries(value)) {
    if (item === true) output.push(key);
    else if (typeof item === "string") output.push(`${key}:${item}`);
    else if (Array.isArray(item)) for (const child of item) if (typeof child === "string") output.push(`${key}:${child}`);
  }
  return output;
}

function claimKey(kind: PluginRegisteredCapabilityKind, capabilityId: string): string {
  return `${kind}\0${capabilityId.toLowerCase()}`;
}

function compareClaim(left: PluginCapabilityClaim, right: PluginCapabilityClaim): number {
  return left.kind.localeCompare(right.kind) || left.capabilityId.localeCompare(right.capabilityId) || left.pluginId.localeCompare(right.pluginId);
}

function capabilityError(code: string, message: string, details: JsonObject = {}): Error {
  return Object.assign(new Error(message), { name: "PluginCapabilityError", code, details });
}
