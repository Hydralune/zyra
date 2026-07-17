import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type { PluginDependency, PluginManifest } from "./contracts.ts";

export interface PluginDependencyNode {
  pluginId: string;
  version: string;
  manifestDigest: string;
  enabled: boolean;
  dependencies: PluginDependency[];
  dependents: string[];
  depth: number;
  loadOrder: number;
  status: "ready" | "missing_dependency" | "version_mismatch" | "cycle" | "disabled";
  failures: JsonObject[];
  metadata: JsonObject;
}

export interface PluginDependencyEdge {
  edgeId: string;
  fromPluginId: string;
  toPluginId: string;
  versionRange: string;
  optional: boolean;
  satisfied: boolean;
  selectedVersion: string | null;
  failureCode: string | null;
  metadata: JsonObject;
}

export interface PluginDependencyResolution {
  resolutionId: string;
  revision: number;
  nodes: PluginDependencyNode[];
  edges: PluginDependencyEdge[];
  loadOrder: string[];
  blocked: string[];
  cycles: string[][];
  valid: boolean;
  resolvedAt: string;
  digest: string;
  metadata: JsonObject;
}

export interface PluginDependencySnapshot {
  version: "zyra.plugin-dependency-runtime/v1";
  revision: number;
  resolutions: PluginDependencyResolution[];
  head: PluginDependencyResolution | null;
  digest: string;
  capturedAt: string;
}

export class PluginDependencyRuntime {
  private readonly resolutions = new Map<number, PluginDependencyResolution>();
  private readonly now: () => Date;
  private readonly maximumResolutions: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumResolutions?: number; snapshot?: PluginDependencySnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumResolutions = options.maximumResolutions ?? 100;
    if (options.snapshot) this.restore(options.snapshot);
  }

  resolve(manifestsValue: PluginManifest[], expectedRevision = this.revision, metadata: JsonObject = {}): PluginDependencyResolution {
    if (expectedRevision !== this.revision) throw dependencyError("dependency_revision_conflict", `plugin dependency revision ${expectedRevision} does not match ${this.revision}`);
    const manifests = manifestsValue.map(cloneJson).sort((left, right) => left.pluginId.localeCompare(right.pluginId));
    const byId = new Map<string, PluginManifest>();
    for (const manifest of manifests) {
      if (byId.has(manifest.pluginId)) throw dependencyError("duplicate_plugin", `duplicate plugin ${manifest.pluginId}`);
      byId.set(manifest.pluginId, manifest);
    }
    const edges: PluginDependencyEdge[] = [];
    const failures = new Map<string, JsonObject[]>();
    for (const manifest of manifests) {
      for (const dependency of manifest.dependencies) {
        const selected = byId.get(dependency.pluginId);
        const satisfied = Boolean(selected && satisfiesVersion(selected.version, dependency.versionRange));
        const failureCode = selected ? satisfied ? null : "dependency_version_mismatch" : dependency.optional ? null : "dependency_missing";
        const edgeBase = {
          fromPluginId: manifest.pluginId,
          toPluginId: dependency.pluginId,
          versionRange: dependency.versionRange,
          optional: dependency.optional,
          satisfied,
          selectedVersion: selected?.version ?? null,
          failureCode,
          metadata: { capabilities: dependency.capabilities },
        };
        edges.push({ edgeId: deterministicId("plugin-dependency-edge", edgeBase, 32), ...edgeBase });
        if (failureCode) {
          const list = failures.get(manifest.pluginId) ?? [];
          list.push({ code: failureCode, dependency: dependency.pluginId, required: dependency.versionRange, actual: selected?.version ?? null });
          failures.set(manifest.pluginId, list);
        }
      }
    }
    const cycles = findCycles(manifests.map((manifest) => manifest.pluginId), edges.filter((edge) => edge.satisfied));
    for (const cycle of cycles) {
      for (const pluginId of cycle) {
        const list = failures.get(pluginId) ?? [];
        list.push({ code: "dependency_cycle", cycle });
        failures.set(pluginId, list);
      }
    }
    const blocked = new Set([...failures.keys()]);
    propagateBlocked(edges, blocked);
    const loadOrder = topologicalOrder(manifests.map((manifest) => manifest.pluginId).filter((id) => !blocked.has(id)), edges);
    const depth = dependencyDepth(loadOrder, edges);
    const nodes = manifests.map((manifest): PluginDependencyNode => {
      const nodeFailures = failures.get(manifest.pluginId) ?? [];
      let status: PluginDependencyNode["status"] = "ready";
      if (!manifest.enabled) status = "disabled";
      else if (nodeFailures.some((failure) => failure.code === "dependency_cycle")) status = "cycle";
      else if (nodeFailures.some((failure) => failure.code === "dependency_version_mismatch")) status = "version_mismatch";
      else if (nodeFailures.length || blocked.has(manifest.pluginId)) status = "missing_dependency";
      return {
        pluginId: manifest.pluginId,
        version: manifest.version,
        manifestDigest: manifest.manifestDigest,
        enabled: manifest.enabled,
        dependencies: manifest.dependencies.map(cloneJson),
        dependents: edges.filter((edge) => edge.toPluginId === manifest.pluginId).map((edge) => edge.fromPluginId).sort(),
        depth: depth.get(manifest.pluginId) ?? 0,
        loadOrder: loadOrder.indexOf(manifest.pluginId),
        status,
        failures: nodeFailures.map(cloneJson),
        metadata: {},
      };
    });
    this.revision += 1;
    const base = {
      revision: this.revision,
      nodes,
      edges: edges.sort((left, right) => left.edgeId.localeCompare(right.edgeId)),
      loadOrder,
      blocked: [...blocked].sort(),
      cycles: cycles.map((cycle) => [...cycle]),
      valid: nodes.every((node) => node.status === "ready" || node.status === "disabled"),
      resolvedAt: this.timestamp(),
      metadata: cloneJson(metadata),
    };
    const resolutionId = deterministicId("plugin-dependency-resolution", base, 40);
    const resolution: PluginDependencyResolution = { resolutionId, ...base, digest: digest({ resolutionId, ...base }) };
    this.resolutions.set(this.revision, resolution);
    this.trim();
    return cloneJson(resolution);
  }

  requireLoadable(pluginId: string, revision = this.revision): PluginDependencyNode {
    const resolution = this.resolutions.get(revision);
    if (!resolution) throw dependencyError("dependency_resolution_not_found", `dependency resolution ${revision} was not found`);
    const node = resolution.nodes.find((value) => value.pluginId === pluginId);
    if (!node) throw dependencyError("plugin_not_resolved", `plugin ${pluginId} was not resolved`);
    if (node.status !== "ready") throw dependencyError("plugin_dependency_blocked", `plugin ${pluginId} dependency status is ${node.status}`, { failures: node.failures });
    return cloneJson(node);
  }

  head(): PluginDependencyResolution | null {
    const value = this.resolutions.get(this.revision);
    return value ? cloneJson(value) : null;
  }

  snapshot(): PluginDependencySnapshot {
    const withoutDigest = {
      version: "zyra.plugin-dependency-runtime/v1" as const,
      revision: this.revision,
      resolutions: [...this.resolutions.values()].sort((left, right) => left.revision - right.revision).map(cloneJson),
      head: this.head(),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: PluginDependencySnapshot): void {
    if (snapshot.version !== "zyra.plugin-dependency-runtime/v1") throw dependencyError("unsupported_dependency_snapshot", "unsupported plugin dependency snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw dependencyError("dependency_snapshot_digest_mismatch", "plugin dependency snapshot digest mismatch");
    this.resolutions.clear();
    this.revision = snapshot.revision;
    for (const resolution of snapshot.resolutions) this.resolutions.set(resolution.revision, cloneJson(resolution));
    if (snapshot.head && snapshot.head.revision !== this.revision) throw dependencyError("dependency_head_mismatch", "plugin dependency snapshot head mismatch");
  }

  private trim(): void {
    while (this.resolutions.size > this.maximumResolutions) {
      const oldest = [...this.resolutions.keys()].sort((left, right) => left - right)[0];
      if (oldest === undefined || oldest === this.revision) break;
      this.resolutions.delete(oldest);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

export function satisfiesVersion(version: string, rangeValue: string): boolean {
  const range = rangeValue.trim();
  if (!range || range === "*" || range.toLowerCase() === "latest") return true;
  const parsed = parseVersion(version);
  for (const alternative of range.split("||").map((value) => value.trim()).filter(Boolean)) {
    const conditions = alternative.split(/\s+/).filter(Boolean);
    if (conditions.every((condition) => satisfiesCondition(parsed, condition))) return true;
  }
  return false;
}

function parseVersion(value: string): [number, number, number, string] {
  const match = /^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?/.exec(value.trim());
  if (!match) throw dependencyError("plugin_version_invalid", `invalid plugin version ${value}`);
  return [Number(match[1]), Number(match[2] ?? 0), Number(match[3] ?? 0), match[4] ?? ""];
}

function satisfiesCondition(version: [number, number, number, string], condition: string): boolean {
  if (condition.startsWith("^")) {
    const base = parseVersion(condition.slice(1));
    const upper: [number, number, number, string] = base[0] > 0 ? [base[0] + 1, 0, 0, ""] : base[1] > 0 ? [0, base[1] + 1, 0, ""] : [0, 0, base[2] + 1, ""];
    return compareVersion(version, base) >= 0 && compareVersion(version, upper) < 0;
  }
  if (condition.startsWith("~")) {
    const base = parseVersion(condition.slice(1));
    const upper: [number, number, number, string] = [base[0], base[1] + 1, 0, ""];
    return compareVersion(version, base) >= 0 && compareVersion(version, upper) < 0;
  }
  const wildcard = /^(\d+|x|\*)\.(\d+|x|\*)(?:\.(\d+|x|\*))?$/.exec(condition);
  if (wildcard) {
    if (wildcard[1] !== "x" && wildcard[1] !== "*" && version[0] !== Number(wildcard[1])) return false;
    if (wildcard[2] !== "x" && wildcard[2] !== "*" && version[1] !== Number(wildcard[2])) return false;
    if (wildcard[3] && wildcard[3] !== "x" && wildcard[3] !== "*" && version[2] !== Number(wildcard[3])) return false;
    return true;
  }
  const comparison = /^(>=|<=|>|<|=)?(.+)$/.exec(condition)!;
  const target = parseVersion(comparison[2]);
  const value = compareVersion(version, target);
  if (comparison[1] === ">=") return value >= 0;
  if (comparison[1] === "<=") return value <= 0;
  if (comparison[1] === ">") return value > 0;
  if (comparison[1] === "<") return value < 0;
  return value === 0;
}

function compareVersion(left: [number, number, number, string], right: [number, number, number, string]): number {
  for (let index = 0; index < 3; index += 1) if (left[index] !== right[index]) return Number(left[index]) - Number(right[index]);
  if (!left[3] && right[3]) return 1;
  if (left[3] && !right[3]) return -1;
  return left[3].localeCompare(right[3]);
}

function findCycles(nodes: string[], edges: PluginDependencyEdge[]): string[][] {
  const adjacency = new Map(nodes.map((node) => [node, edges.filter((edge) => edge.fromPluginId === node && nodes.includes(edge.toPluginId)).map((edge) => edge.toPluginId)]));
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const stack: string[] = [];
  const cycles: string[][] = [];
  const visit = (node: string): void => {
    if (visiting.has(node)) {
      const offset = stack.indexOf(node);
      const cycle = [...stack.slice(offset), node];
      if (!cycles.some((value) => digest(value) === digest(cycle))) cycles.push(cycle);
      return;
    }
    if (visited.has(node)) return;
    visiting.add(node);
    stack.push(node);
    for (const child of adjacency.get(node) ?? []) visit(child);
    stack.pop();
    visiting.delete(node);
    visited.add(node);
  };
  for (const node of nodes) visit(node);
  return cycles;
}

function propagateBlocked(edges: PluginDependencyEdge[], blocked: Set<string>): void {
  let changed = true;
  while (changed) {
    changed = false;
    for (const edge of edges) {
      if (!edge.optional && blocked.has(edge.toPluginId) && !blocked.has(edge.fromPluginId)) {
        blocked.add(edge.fromPluginId);
        changed = true;
      }
    }
  }
}

function topologicalOrder(nodes: string[], edges: PluginDependencyEdge[]): string[] {
  const output: string[] = [];
  const visited = new Set<string>();
  const visit = (node: string): void => {
    if (visited.has(node)) return;
    visited.add(node);
    for (const dependency of edges.filter((edge) => edge.fromPluginId === node && edge.satisfied && nodes.includes(edge.toPluginId)).map((edge) => edge.toPluginId).sort()) visit(dependency);
    output.push(node);
  };
  for (const node of [...nodes].sort()) visit(node);
  return output;
}

function dependencyDepth(loadOrder: string[], edges: PluginDependencyEdge[]): Map<string, number> {
  const depth = new Map<string, number>();
  for (const node of loadOrder) {
    const dependencies = edges.filter((edge) => edge.fromPluginId === node && edge.satisfied).map((edge) => depth.get(edge.toPluginId) ?? 0);
    depth.set(node, dependencies.length ? Math.max(...dependencies) + 1 : 0);
  }
  return depth;
}

function dependencyError(code: string, message: string, details: JsonObject = {}): Error {
  return Object.assign(new Error(message), { name: "PluginDependencyError", code, details });
}
