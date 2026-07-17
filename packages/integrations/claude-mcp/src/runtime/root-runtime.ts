import { relative, resolve, sep } from "node:path";

import type { JsonObject } from "../contracts.ts";
import type { McpTransportAdapter } from "../connection/contracts.ts";
import { cloneJson, deterministicMcpId, monotonicNow, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import { McpProtocolCodec, type McpRoot } from "../core/protocol.ts";

export interface McpRootRecord extends McpRoot {
  rootId: string;
  workspaceRoot: string;
  realPath: string;
  enabled: boolean;
  source: "workspace" | "project" | "session";
  revision: number;
  createdAt: string;
  updatedAt: string;
  metadata: JsonObject;
}

export interface McpRootRevision {
  revisionId: string;
  revisionBefore: number;
  revisionAfter: number;
  added: string[];
  updated: string[];
  removed: string[];
  changedAt: string;
  digest: string;
  metadata: JsonObject;
}

export interface McpRootSnapshot {
  version: "zyra.mcp-root-runtime/v1";
  revision: number;
  roots: McpRootRecord[];
  revisions: McpRootRevision[];
  digest: string;
  capturedAt: string;
}

export class McpRootRuntime {
  private readonly workspaceRoot: string;
  private readonly codec = new McpProtocolCodec();
  private readonly roots = new Map<string, McpRootRecord>();
  private readonly revisions: McpRootRevision[] = [];
  private readonly now: () => Date;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { workspaceRoot: string; now?: () => Date; snapshot?: McpRootSnapshot | null }) {
    if (!options.workspaceRoot) throw rootError("workspace_root_missing", "MCP root runtime requires workspace root");
    this.workspaceRoot = resolve(options.workspaceRoot);
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  replace(
    values: readonly { uri?: string; path?: string; name?: string; source?: McpRootRecord["source"]; enabled?: boolean; metadata?: JsonObject }[],
    expectedRevision = this.revision,
    metadata: JsonObject = {},
  ): McpRootRevision {
    if (expectedRevision !== this.revision) throw rootError("root_revision_conflict", `expected root revision ${expectedRevision}, current ${this.revision}`);
    const next = new Map<string, McpRootRecord>();
    for (const value of values) {
      const realPath = this.resolvePath(value.path ?? uriPath(value.uri ?? ""));
      const uri = pathUri(realPath);
      const rootId = deterministicMcpId("mcp-root", { workspace_root: this.workspaceRoot, real_path: realPath }, 32);
      if (next.has(rootId)) throw rootError("duplicate_root", `duplicate MCP root ${realPath}`);
      const prior = this.roots.get(rootId);
      const timestamp = this.timestamp();
      next.set(rootId, {
        rootId,
        uri,
        name: value.name?.trim() || prior?.name || relative(this.workspaceRoot, realPath) || "workspace",
        meta: prior?.meta ?? {},
        workspaceRoot: this.workspaceRoot,
        realPath,
        enabled: value.enabled !== false,
        source: value.source ?? prior?.source ?? "project",
        revision: prior ? prior.revision + 1 : 1,
        createdAt: prior?.createdAt ?? timestamp,
        updatedAt: timestamp,
        metadata: cloneJson(value.metadata ?? prior?.metadata ?? {}),
      });
    }
    const added = [...next.keys()].filter((id) => !this.roots.has(id));
    const removed = [...this.roots.keys()].filter((id) => !next.has(id));
    const updated = [...next.keys()].filter((id) => {
      const prior = this.roots.get(id);
      const current = next.get(id)!;
      return Boolean(prior && sha256(project(prior)) !== sha256(project(current)));
    });
    const changedAt = this.timestamp();
    const base = {
      revisionBefore: this.revision,
      revisionAfter: this.revision + 1,
      added: added.sort(),
      updated: updated.sort(),
      removed: removed.sort(),
      changedAt,
      metadata: cloneJson(metadata),
    };
    const revision: McpRootRevision = {
      revisionId: deterministicMcpId("mcp-root-revision", base, 40),
      ...base,
      digest: sha256(base),
    };
    this.roots.clear();
    for (const [id, root] of next) this.roots.set(id, root);
    this.revision += 1;
    this.revisions.push(revision);
    return cloneJson(revision);
  }

  async notifyChanged(transports: readonly McpTransportAdapter[], revisionId?: string, signal?: AbortSignal): Promise<void> {
    const current = revisionId ? this.revisions.find((entry) => entry.revisionId === revisionId) : this.revisions.at(-1);
    if (!current) throw rootError("root_revision_not_found", `root revision ${revisionId ?? "latest"} was not found`);
    const notification = this.codec.notification("notifications/roots/list_changed", {
      revision: current.revisionAfter,
      revisionId: current.revisionId,
      digest: current.digest,
    });
    const failures: JsonObject[] = [];
    for (const transport of transports) {
      try {
        await transport.notify(notification, signal);
      } catch (error) {
        failures.push({ server_id: transport.serverId, message: error instanceof Error ? error.message : String(error) });
      }
    }
    if (failures.length) throw rootError("root_notification_failed", `failed to notify ${failures.length} MCP servers`, { failures });
  }

  list(options: { includeDisabled?: boolean; serverAllowedRoots?: string[] } = {}): McpRoot[] {
    const allowed = (options.serverAllowedRoots ?? []).map((value) => this.resolvePath(value));
    return [...this.roots.values()]
      .filter((root) => options.includeDisabled || root.enabled)
      .filter((root) => !allowed.length || allowed.some((candidate) => contains(candidate, root.realPath) || contains(root.realPath, candidate)))
      .sort((left, right) => left.realPath.localeCompare(right.realPath))
      .map((root) => ({ uri: root.uri, name: root.name, meta: cloneJson(root.meta) }));
  }

  records(): McpRootRecord[] {
    return [...this.roots.values()].sort((left, right) => left.rootId.localeCompare(right.rootId)).map(cloneJson);
  }

  snapshot(): McpRootSnapshot {
    const withoutDigest = {
      version: "zyra.mcp-root-runtime/v1" as const,
      revision: this.revision,
      roots: this.records(),
      revisions: this.revisions.map(cloneJson),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  restore(snapshot: McpRootSnapshot): void {
    if (snapshot.version !== "zyra.mcp-root-runtime/v1") throw rootError("unsupported_root_snapshot", "unsupported MCP root snapshot version");
    const { digest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== digest) throw rootError("root_snapshot_digest_mismatch", "MCP root snapshot digest mismatch");
    this.roots.clear();
    this.revisions.splice(0);
    this.revision = snapshot.revision;
    for (const value of snapshot.roots) {
      const record = cloneJson(value);
      const resolved = this.resolvePath(record.realPath);
      if (resolved !== record.realPath || pathUri(resolved) !== record.uri) throw rootError("restored_root_binding_invalid", `restored root ${record.rootId} is outside workspace`);
      this.roots.set(record.rootId, record);
    }
    for (const revision of snapshot.revisions) this.revisions.push(cloneJson(revision));
  }

  private resolvePath(value: string): string {
    if (!value) throw rootError("root_path_missing", "MCP root path is required");
    const candidate = resolve(this.workspaceRoot, value);
    if (!contains(this.workspaceRoot, candidate)) throw rootError("root_path_escape", `MCP root ${value} escapes workspace`);
    return candidate;
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function contains(parent: string, child: string): boolean {
  const rel = relative(parent, child);
  return rel === "" || (!rel.startsWith("..") && !resolve(rel).startsWith(sep));
}

function uriPath(uri: string): string {
  if (!uri.startsWith("file:")) return uri;
  return decodeURIComponent(uri.replace(/^file:\/\//, "").replace(/^\/([A-Za-z]:)/, "$1"));
}

function pathUri(path: string): string {
  return `file://${path.replace(/\\/g, "/").replace(/^([A-Za-z]:)/, "/$1")}`;
}

function project(root: McpRootRecord): JsonObject {
  return { uri: root.uri, name: root.name, enabled: root.enabled, source: root.source, metadata: root.metadata };
}

function rootError(code: string, message: string, details: JsonObject = {}): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-root", { code, message, details }),
    category: code.includes("revision") ? "conflict" : "policy",
    code,
    message,
    retryable: code.includes("notification"),
    disposition: code.includes("notification") ? "retry_same_connection" : "terminal",
    details,
  });
}
