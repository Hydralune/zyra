import { existsSync, realpathSync, statSync } from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";

import type { JsonObject } from "../contracts.ts";
import {
  canonicalize,
  cloneJson,
  constantTimeDigestEquals,
  deterministicId,
  digest,
  monotonicNow,
  normalizeIdentifier,
} from "../e02/index.ts";
import type { SkillSourceRoot } from "./contracts-v2.ts";

export interface SkillRootFailure extends JsonObject {
  failureId: string;
  sourceId: string;
  pluginId: string | null;
  code: string;
  message: string;
  fatal: boolean;
  observedAt: string;
  metadata: JsonObject;
}

export interface SkillRootRevision {
  revisionId: string;
  revisionBefore: number;
  revisionAfter: number;
  roots: SkillSourceRoot[];
  added: string[];
  updated: string[];
  removed: string[];
  unchanged: string[];
  rejected: SkillRootFailure[];
  committedAt: string;
  metadata: JsonObject;
  digest: string;
}

export interface SkillRootSnapshot {
  version: "zyra.skill-root-runtime/v1";
  workspaceRoot: string;
  revision: number;
  roots: SkillSourceRoot[];
  revisions: SkillRootRevision[];
  failures: SkillRootFailure[];
  digest: string;
  capturedAt: string;
}

export interface SkillRootMutation {
  kind: "replace" | "remove";
  sourceId: string;
  root?: SkillSourceRoot;
}

export interface SkillPluginRootInput {
  pluginId: string;
  pluginRevision: number;
  manifestDigest: string;
  roots: SkillSourceRoot[];
  enabled: boolean;
  metadata?: JsonObject;
}

export class SkillRootRuntime {
  private readonly workspaceRoot: string;
  private readonly now: () => Date;
  private readonly maximumRevisions: number;
  private readonly maximumFailures: number;
  private readonly roots = new Map<string, SkillSourceRoot>();
  private readonly revisions = new Map<number, SkillRootRevision>();
  private readonly failures = new Map<string, SkillRootFailure>();
  private revisionValue = 0;
  private lastTimestamp: string | null = null;

  constructor(options: {
    workspaceRoot: string;
    roots?: readonly SkillSourceRoot[];
    now?: () => Date;
    maximumRevisions?: number;
    maximumFailures?: number;
    snapshot?: SkillRootSnapshot | null;
  }) {
    if (!options.workspaceRoot) {
      throw rootError("skill_root_workspace_missing", "skill root runtime requires a workspace root");
    }
    this.workspaceRoot = resolve(options.workspaceRoot);
    this.now = options.now ?? (() => new Date());
    this.maximumRevisions = options.maximumRevisions ?? 2_000;
    this.maximumFailures = options.maximumFailures ?? 2_000;
    if (options.snapshot) {
      this.restore(options.snapshot);
      return;
    }
    const initial = (options.roots ?? []).map((root) => this.normalize(root));
    assertUniqueSourceIds(initial);
    for (const root of initial) this.roots.set(root.sourceId, root);
  }

  get revision(): number {
    return this.revisionValue;
  }

  list(options: { enabledOnly?: boolean; pluginId?: string | null; existingOnly?: boolean } = {}): SkillSourceRoot[] {
    return [...this.roots.values()]
      .filter((root) => !options.enabledOnly || root.enabled)
      .filter((root) => options.pluginId === undefined || root.pluginId === options.pluginId)
      .filter((root) => !options.existingOnly || existsSync(root.rootPath))
      .sort(compareRoots)
      .map(cloneJson);
  }

  require(sourceId: string): SkillSourceRoot {
    const normalized = normalizeIdentifier(sourceId, "skill source id");
    const root = this.roots.get(normalized);
    if (!root) throw rootError("skill_root_not_found", `skill source ${normalized} was not found`);
    return cloneJson(root);
  }

  replaceAll(rootsValue: readonly SkillSourceRoot[], expectedRevision = this.revisionValue, metadata: JsonObject = {}): SkillRootRevision {
    const mutations: SkillRootMutation[] = rootsValue.map((root) => ({
      kind: "replace",
      sourceId: root.sourceId,
      root,
    }));
    for (const sourceId of this.roots.keys()) {
      if (!rootsValue.some((root) => root.sourceId === sourceId)) {
        mutations.push({ kind: "remove", sourceId });
      }
    }
    return this.commit(
      mutations,
      expectedRevision,
      metadata,
    );
  }

  replacePlugin(inputValue: SkillPluginRootInput, expectedRevision = this.revisionValue): SkillRootRevision {
    const input = cloneJson(inputValue);
    if (!input.pluginId) throw rootError("skill_plugin_id_missing", "plugin skill roots require a plugin id");
    if (!Number.isSafeInteger(input.pluginRevision) || input.pluginRevision < 1) {
      throw rootError("skill_plugin_revision_invalid", `plugin ${input.pluginId} has invalid skill root revision`);
    }
    if (!/^[a-f0-9]{64}$/i.test(input.manifestDigest)) {
      throw rootError("skill_plugin_manifest_digest_invalid", `plugin ${input.pluginId} has invalid manifest digest`);
    }
    const currentIds = this.list({ pluginId: input.pluginId }).map((root) => root.sourceId);
    const next = input.roots.map((root, index) => this.normalize({
      ...root,
      sourceId: root.sourceId || `${input.pluginId}-skill-${index + 1}`,
      kind: "plugin",
      pluginId: input.pluginId,
      enabled: input.enabled && root.enabled,
      revision: input.pluginRevision,
      metadata: {
        ...root.metadata,
        plugin_id: input.pluginId,
        plugin_revision: input.pluginRevision,
        plugin_manifest_digest: input.manifestDigest,
      },
    }));
    assertUniqueSourceIds(next);
    const mutations: SkillRootMutation[] = next.map((root) => ({ kind: "replace", sourceId: root.sourceId, root }));
    for (const sourceId of currentIds) {
      if (!next.some((root) => root.sourceId === sourceId)) mutations.push({ kind: "remove", sourceId });
    }
    return this.commit(mutations, expectedRevision, {
      ...input.metadata,
      reason: "plugin_skill_roots_replaced",
      plugin_id: input.pluginId,
      plugin_revision: input.pluginRevision,
      plugin_manifest_digest: input.manifestDigest,
    });
  }

  removePlugin(pluginId: string, expectedRevision = this.revisionValue, metadata: JsonObject = {}): SkillRootRevision {
    const roots = this.list({ pluginId });
    return this.commit(
      roots.map((root) => ({ kind: "remove", sourceId: root.sourceId })),
      expectedRevision,
      { ...metadata, reason: "plugin_skill_roots_removed", plugin_id: pluginId },
    );
  }

  validateReachable(options: { enabledOnly?: boolean; requireDirectories?: boolean } = {}): SkillRootFailure[] {
    const failures: SkillRootFailure[] = [];
    for (const root of this.list({ enabledOnly: options.enabledOnly !== false })) {
      try {
        this.validateFilesystemRoot(root, options.requireDirectories !== false);
      } catch (error) {
        failures.push(this.recordFailure({
          sourceId: root.sourceId,
          pluginId: root.pluginId,
          code: errorCode(error),
          message: error instanceof Error ? error.message : String(error),
          fatal: true,
          metadata: { root_path: root.rootPath, root_revision: root.revision },
        }));
      }
    }
    return failures;
  }

  recordFailure(input: { sourceId: string; pluginId: string | null; code: string; message: string; fatal: boolean; metadata?: JsonObject }): SkillRootFailure {
    const observedAt = this.timestamp();
    const base = {
      sourceId: input.sourceId,
      pluginId: input.pluginId,
      code: input.code,
      message: input.message,
      fatal: input.fatal,
      observedAt,
      metadata: cloneJson(input.metadata ?? {}),
    };
    const failure: SkillRootFailure = { failureId: deterministicId("skill-root-failure", base, 40), ...base };
    this.failures.set(failure.failureId, failure);
    while (this.failures.size > this.maximumFailures) {
      const oldest = [...this.failures.values()].sort((left, right) => left.observedAt.localeCompare(right.observedAt))[0];
      if (!oldest) break;
      this.failures.delete(oldest.failureId);
    }
    return cloneJson(failure);
  }

  listFailures(): SkillRootFailure[] {
    return [...this.failures.values()].sort((left, right) => left.observedAt.localeCompare(right.observedAt)).map(cloneJson);
  }

  snapshot(): SkillRootSnapshot {
    const withoutDigest = {
      version: "zyra.skill-root-runtime/v1" as const,
      workspaceRoot: this.workspaceRoot,
      revision: this.revisionValue,
      roots: this.list(),
      revisions: [...this.revisions.values()].sort((left, right) => left.revisionAfter - right.revisionAfter).map(cloneJson),
      failures: this.listFailures(),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshotValue: SkillRootSnapshot): void {
    const snapshot = cloneJson(snapshotValue);
    if (snapshot.version !== "zyra.skill-root-runtime/v1") {
      throw rootError("unsupported_skill_root_snapshot", `unsupported skill root snapshot ${snapshot.version}`);
    }
    const { digest: expectedDigest, ...withoutDigest } = snapshot;
    if (!constantTimeDigestEquals(digest(withoutDigest), expectedDigest)) {
      throw rootError("skill_root_snapshot_digest_mismatch", "skill root snapshot digest does not match its payload");
    }
    if (resolve(snapshot.workspaceRoot) !== this.workspaceRoot) {
      throw rootError("skill_root_snapshot_workspace_mismatch", "skill root snapshot belongs to a different workspace");
    }
    this.roots.clear();
    this.revisions.clear();
    this.failures.clear();
    this.revisionValue = snapshot.revision;
    for (const root of snapshot.roots) {
      const normalized = this.normalize(root);
      if (this.roots.has(normalized.sourceId)) {
        throw rootError("skill_root_snapshot_duplicate", `skill root snapshot duplicates ${normalized.sourceId}`);
      }
      this.roots.set(normalized.sourceId, normalized);
    }
    for (const revision of snapshot.revisions.slice(-this.maximumRevisions)) {
      if (revision.revisionAfter !== revision.revisionBefore + 1 || revision.revisionAfter > snapshot.revision) {
        throw rootError("skill_root_revision_invalid", `skill root revision ${revision.revisionId} is invalid`);
      }
      this.revisions.set(revision.revisionAfter, cloneJson(revision));
    }
    for (const failure of snapshot.failures.slice(-this.maximumFailures)) this.failures.set(failure.failureId, cloneJson(failure));
  }

  private commit(mutationsValue: readonly SkillRootMutation[], expectedRevision: number, metadata: JsonObject): SkillRootRevision {
    if (expectedRevision !== this.revisionValue) {
      throw rootError("skill_root_revision_conflict", `skill root revision ${expectedRevision} does not match ${this.revisionValue}`, {
        expected_revision: expectedRevision,
        actual_revision: this.revisionValue,
      });
    }
    const mutations = mutationsValue.map((mutation) => ({
      ...mutation,
      sourceId: normalizeIdentifier(mutation.sourceId, "skill source id"),
      root: mutation.root ? this.normalize(mutation.root) : undefined,
    }));
    const duplicateMutations = duplicateValues(mutations.map((mutation) => mutation.sourceId));
    if (duplicateMutations.length) {
      throw rootError("skill_root_duplicate_mutation", `skill root mutation repeats ${duplicateMutations.join(", ")}`);
    }
    const next = new Map(this.roots);
    for (const mutation of mutations) {
      if (mutation.kind === "remove") {
        next.delete(mutation.sourceId);
        continue;
      }
      if (!mutation.root || mutation.root.sourceId !== mutation.sourceId) {
        throw rootError("skill_root_mutation_invalid", `skill root mutation ${mutation.sourceId} is invalid`);
      }
      next.set(mutation.sourceId, mutation.root);
    }
    this.assertNoPathConflicts([...next.values()]);
    const added: string[] = [];
    const updated: string[] = [];
    const removed: string[] = [];
    const unchanged: string[] = [];
    for (const sourceId of new Set([...this.roots.keys(), ...next.keys()])) {
      const before = this.roots.get(sourceId);
      const after = next.get(sourceId);
      if (!before && after) added.push(sourceId);
      else if (before && !after) removed.push(sourceId);
      else if (before && after && digest(before) !== digest(after)) updated.push(sourceId);
      else unchanged.push(sourceId);
    }
    const rejected = [...next.values()]
      .filter((root) => root.enabled && !existsSync(root.rootPath))
      .map((root) => this.recordFailure({
        sourceId: root.sourceId,
        pluginId: root.pluginId,
        code: "skill_root_missing",
        message: `skill root ${root.rootPath} does not exist`,
        fatal: false,
        metadata: { staged_revision: this.revisionValue + 1 },
      }));
    const committedAt = this.timestamp();
    const revisionBase = {
      revisionBefore: this.revisionValue,
      revisionAfter: this.revisionValue + 1,
      roots: [...next.values()].sort(compareRoots).map(cloneJson),
      added: added.sort(),
      updated: updated.sort(),
      removed: removed.sort(),
      unchanged: unchanged.sort(),
      rejected,
      committedAt,
      metadata: canonicalize(metadata) as JsonObject,
    };
    const revisionId = deterministicId("skill-root-revision", revisionBase, 40);
    const revision: SkillRootRevision = { revisionId, ...revisionBase, digest: digest({ revisionId, ...revisionBase }) };
    this.roots.clear();
    for (const [sourceId, root] of next) this.roots.set(sourceId, root);
    this.revisionValue += 1;
    this.revisions.set(this.revisionValue, revision);
    while (this.revisions.size > this.maximumRevisions) {
      const oldest = [...this.revisions.keys()].sort((left, right) => left - right)[0];
      if (oldest === undefined) break;
      this.revisions.delete(oldest);
    }
    return cloneJson(revision);
  }

  private normalize(rootValue: SkillSourceRoot): SkillSourceRoot {
    const root = cloneJson(rootValue);
    root.sourceId = normalizeIdentifier(root.sourceId, "skill source id");
    root.rootPath = resolve(this.workspaceRoot, root.rootPath);
    if (!Number.isSafeInteger(root.priority)) {
      throw rootError("skill_root_priority_invalid", `skill root ${root.sourceId} priority must be an integer`);
    }
    if (!Number.isSafeInteger(root.maximumDepth) || root.maximumDepth < 0 || root.maximumDepth > 100) {
      throw rootError("skill_root_depth_invalid", `skill root ${root.sourceId} maximum depth is invalid`);
    }
    if (!Number.isSafeInteger(root.revision) || root.revision < 0) {
      throw rootError("skill_root_source_revision_invalid", `skill root ${root.sourceId} source revision is invalid`);
    }
    this.assertContained(root.rootPath, root.sourceId);
    root.includePatterns = [...new Set(root.includePatterns ?? [])].sort();
    root.excludePatterns = [...new Set(root.excludePatterns ?? [])].sort();
    root.metadata = canonicalize(root.metadata ?? {}) as JsonObject;
    return root;
  }

  private validateFilesystemRoot(root: SkillSourceRoot, requireDirectory: boolean): void {
    this.assertContained(root.rootPath, root.sourceId);
    if (!existsSync(root.rootPath)) throw rootError("skill_root_missing", `skill root ${root.rootPath} does not exist`);
    const real = realpathSync(root.rootPath);
    this.assertContained(real, root.sourceId);
    if (requireDirectory && !statSync(real).isDirectory()) {
      throw rootError("skill_root_not_directory", `skill root ${root.rootPath} is not a directory`);
    }
  }

  private assertNoPathConflicts(roots: SkillSourceRoot[]): void {
    const enabled = roots.filter((root) => root.enabled);
    for (let leftIndex = 0; leftIndex < enabled.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < enabled.length; rightIndex += 1) {
        const left = enabled[leftIndex]!;
        const right = enabled[rightIndex]!;
        if (left.sourceId !== right.sourceId && left.rootPath.toLowerCase() === right.rootPath.toLowerCase() && left.priority === right.priority) {
          throw rootError("skill_root_path_priority_conflict", `skill sources ${left.sourceId} and ${right.sourceId} claim ${left.rootPath} at the same priority`);
        }
      }
    }
  }

  private assertContained(pathValue: string, label: string): void {
    const target = resolve(pathValue);
    const pathRelative = relative(this.workspaceRoot, target);
    if (
      pathRelative.startsWith("..")
      || isAbsolute(pathRelative)
      || target.toLowerCase() !== this.workspaceRoot.toLowerCase()
        && !target.toLowerCase().startsWith(`${this.workspaceRoot.toLowerCase()}${sep}`)
    ) {
      throw rootError("skill_root_outside_workspace", `skill source ${label} path ${target} is outside workspace ${this.workspaceRoot}`);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function compareRoots(left: SkillSourceRoot, right: SkillSourceRoot): number {
  return right.priority - left.priority || left.sourceId.localeCompare(right.sourceId) || left.rootPath.localeCompare(right.rootPath);
}

function assertUniqueSourceIds(roots: SkillSourceRoot[]): void {
  const duplicates = duplicateValues(roots.map((root) => root.sourceId));
  if (duplicates.length) throw rootError("skill_root_duplicate_source", `skill source ids are duplicated: ${duplicates.join(", ")}`);
}

function duplicateValues(values: string[]): string[] {
  const seen = new Set<string>();
  const duplicates = new Set<string>();
  for (const value of values) {
    if (seen.has(value)) duplicates.add(value);
    seen.add(value);
  }
  return [...duplicates].sort();
}

function errorCode(error: unknown): string {
  if (error && typeof error === "object") {
    const code = (error as { code?: unknown }).code;
    if (typeof code === "string" && code) return code;
  }
  return error instanceof Error && error.name ? error.name : "skill_root_failed";
}

function rootError(code: string, message: string, details: JsonObject = {}): Error {
  return Object.assign(new Error(message), { name: "SkillRootRuntimeError", code, details: canonicalize(details) });
}
