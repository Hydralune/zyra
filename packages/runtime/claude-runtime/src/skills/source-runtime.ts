import { lstat, readdir, readFile, realpath, stat } from "node:fs/promises";
import { dirname, relative, resolve, sep } from "node:path";

import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest, normalizeIdentifier } from "../e02/index.ts";
import type { SkillSourceFile, SkillSourceRoot } from "./contracts-v2.ts";

export interface SkillDiscoveryError extends JsonObject {
  source_id: string;
  path: string;
  code: string;
  message: string;
  fatal: boolean;
}

export interface SkillDiscoveryResult {
  discoveryId: string;
  roots: SkillSourceRoot[];
  files: SkillSourceFile[];
  errors: SkillDiscoveryError[];
  visitedDirectories: number;
  skippedEntries: number;
  discoveredAt: string;
  digest: string;
}

export interface SkillSourceRuntimeOptions {
  manifestNames?: string[];
  maximumFiles?: number;
  maximumManifestBytes?: number;
  allowOutsideWorkspace?: boolean;
  workspaceRoot?: string;
  now?: () => Date;
}

export class SkillSourceRuntime {
  private readonly manifestNames: string[];
  private readonly maximumFiles: number;
  private readonly maximumManifestBytes: number;
  private readonly allowOutsideWorkspace: boolean;
  private readonly workspaceRoot: string | null;
  private readonly now: () => Date;

  constructor(options: SkillSourceRuntimeOptions = {}) {
    this.manifestNames = options.manifestNames ?? ["SKILL.md", "skill.md"];
    this.maximumFiles = options.maximumFiles ?? 100_000;
    this.maximumManifestBytes = options.maximumManifestBytes ?? 4 * 1024 * 1024;
    this.allowOutsideWorkspace = options.allowOutsideWorkspace ?? false;
    this.workspaceRoot = options.workspaceRoot ? resolve(options.workspaceRoot) : null;
    this.now = options.now ?? (() => new Date());
  }

  async discover(rootsValue: readonly SkillSourceRoot[]): Promise<SkillDiscoveryResult> {
    const roots = rootsValue.map(normalizeRoot).filter((root) => root.enabled).sort(compareRoot);
    const files: SkillSourceFile[] = [];
    const errors: SkillDiscoveryError[] = [];
    let visitedDirectories = 0;
    let skippedEntries = 0;
    const seenRealPaths = new Set<string>();
    const discoveredAt = this.now().toISOString();
    for (const root of roots) {
      const rootPath = resolve(root.rootPath);
      try {
        this.assertAllowedRoot(rootPath);
        const rootRealPath = await realpath(rootPath);
        const queue: { path: string; depth: number }[] = [{ path: rootRealPath, depth: 0 }];
        while (queue.length) {
          const current = queue.shift()!;
          if (seenRealPaths.has(current.path)) continue;
          seenRealPaths.add(current.path);
          visitedDirectories += 1;
          let entries;
          try {
            entries = await readdir(current.path, { withFileTypes: true });
          } catch (error) {
            errors.push(discoveryError(root, current.path, "directory_read_failed", error, false));
            continue;
          }
          entries.sort((left, right) => left.name.localeCompare(right.name));
          for (const entry of entries) {
            const entryPath = resolve(current.path, entry.name);
            const relativePath = normalizeRelative(relative(rootRealPath, entryPath));
            if (matchesAny(root.excludePatterns, relativePath)) {
              skippedEntries += 1;
              continue;
            }
            if (entry.isDirectory()) {
              if (!root.recursive || current.depth >= root.maximumDepth) {
                skippedEntries += 1;
                continue;
              }
              queue.push({ path: entryPath, depth: current.depth + 1 });
              continue;
            }
            if (entry.isSymbolicLink()) {
              if (!root.followSymlinks) {
                skippedEntries += 1;
                continue;
              }
              try {
                const target = await realpath(entryPath);
                const targetStat = await stat(target);
                if (targetStat.isDirectory() && root.recursive && current.depth < root.maximumDepth) {
                  this.assertWithinRoot(target, rootRealPath, root);
                  queue.push({ path: target, depth: current.depth + 1 });
                } else if (targetStat.isFile() && this.manifestNames.includes(entry.name)) {
                  const source = await this.readSource(root, rootRealPath, entryPath, target, discoveredAt);
                  if (source) files.push(source);
                }
              } catch (error) {
                errors.push(discoveryError(root, entryPath, "symlink_resolution_failed", error, false));
              }
              continue;
            }
            if (!entry.isFile() || !this.manifestNames.includes(entry.name)) continue;
            if (root.includePatterns.length && !matchesAny(root.includePatterns, relativePath)) {
              skippedEntries += 1;
              continue;
            }
            try {
              const source = await this.readSource(root, rootRealPath, entryPath, entryPath, discoveredAt);
              if (source) files.push(source);
            } catch (error) {
              errors.push(discoveryError(root, entryPath, "manifest_read_failed", error, false));
            }
            if (files.length > this.maximumFiles) {
              throw new Error(`skill discovery exceeds ${this.maximumFiles} manifests`);
            }
          }
        }
      } catch (error) {
        errors.push(discoveryError(root, rootPath, "source_root_failed", error, true));
      }
    }
    const unique = deduplicate(files);
    const base = {
      roots,
      files: unique,
      errors,
      visited_directories: visitedDirectories,
      skipped_entries: skippedEntries,
      discovered_at: discoveredAt,
    };
    return {
      discoveryId: deterministicId("skill-discovery", base, 32),
      roots,
      files: unique,
      errors,
      visitedDirectories,
      skippedEntries,
      discoveredAt,
      digest: digest(base),
    };
  }

  private async readSource(
    root: SkillSourceRoot,
    rootRealPath: string,
    manifestPath: string,
    realManifestPath: string,
    discoveredAt: string,
  ): Promise<SkillSourceFile | null> {
    this.assertWithinRoot(realManifestPath, rootRealPath, root);
    const metadata = await lstat(realManifestPath);
    if (metadata.size > this.maximumManifestBytes) {
      throw new Error(`skill manifest exceeds ${this.maximumManifestBytes} bytes`);
    }
    const bytes = await readFile(realManifestPath);
    const relativePath = normalizeRelative(relative(rootRealPath, manifestPath));
    return {
      sourceId: root.sourceId,
      sourceKind: root.kind,
      sourcePriority: root.priority,
      pluginId: root.pluginId,
      skillDirectory: dirname(manifestPath),
      manifestPath,
      relativePath,
      realPath: realManifestPath,
      sizeBytes: metadata.size,
      modifiedAtMs: metadata.mtimeMs,
      inode: typeof metadata.ino === "number" ? String(metadata.ino) : null,
      contentDigest: digest(bytes.toString("base64")),
      discoveredAt,
      metadata: {
        source_revision: root.revision,
        followed_symlink: resolve(manifestPath) !== resolve(realManifestPath),
      },
    };
  }

  private assertAllowedRoot(rootPath: string): void {
    if (!this.workspaceRoot || this.allowOutsideWorkspace) return;
    const normalized = rootPath.toLowerCase();
    const workspace = this.workspaceRoot.toLowerCase();
    if (normalized !== workspace && !normalized.startsWith(`${workspace}${sep}`)) {
      throw new Error(`skill root ${rootPath} is outside workspace ${this.workspaceRoot}`);
    }
  }

  private assertWithinRoot(path: string, root: string, source: SkillSourceRoot): void {
    if (source.followSymlinks && this.allowOutsideWorkspace) return;
    const normalized = resolve(path).toLowerCase();
    const rootValue = resolve(root).toLowerCase();
    if (normalized !== rootValue && !normalized.startsWith(`${rootValue}${sep}`)) {
      throw new Error(`skill source escapes root through ${path}`);
    }
  }
}

function normalizeRoot(root: SkillSourceRoot): SkillSourceRoot {
  if (!Number.isSafeInteger(root.priority)) throw new Error(`skill source ${root.sourceId} priority must be integer`);
  if (!Number.isSafeInteger(root.maximumDepth) || root.maximumDepth < 0 || root.maximumDepth > 100) throw new Error(`skill source ${root.sourceId} maximumDepth is invalid`);
  return cloneJson({
    ...root,
    sourceId: normalizeIdentifier(root.sourceId, "skill source id"),
    rootPath: resolve(root.rootPath),
    includePatterns: root.includePatterns ?? [],
    excludePatterns: root.excludePatterns ?? [],
    metadata: root.metadata ?? {},
  });
}

function compareRoot(left: SkillSourceRoot, right: SkillSourceRoot): number {
  if (left.priority !== right.priority) return right.priority - left.priority;
  return left.sourceId.localeCompare(right.sourceId);
}

function normalizeRelative(value: string): string {
  return value.split(sep).join("/");
}

function matchesAny(patterns: readonly string[], value: string): boolean {
  return patterns.some((pattern) => globMatch(pattern, value));
}

function globMatch(pattern: string, value: string): boolean {
  const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*\*/g, "\u0000").replace(/\*/g, "[^/]*").replace(/\?/g, "[^/]").replace(/\u0000/g, ".*");
  return new RegExp(`^${escaped}$`, "i").test(value);
}

function deduplicate(files: SkillSourceFile[]): SkillSourceFile[] {
  const byPath = new Map<string, SkillSourceFile>();
  for (const file of files.sort((left, right) => right.sourcePriority - left.sourcePriority || left.sourceId.localeCompare(right.sourceId))) {
    const key = resolve(file.realPath).toLowerCase();
    if (!byPath.has(key)) byPath.set(key, file);
  }
  return [...byPath.values()].sort((left, right) => left.sourceId.localeCompare(right.sourceId) || left.relativePath.localeCompare(right.relativePath));
}

function discoveryError(
  root: SkillSourceRoot,
  path: string,
  code: string,
  error: unknown,
  fatal: boolean,
): SkillDiscoveryError {
  return {
    source_id: root.sourceId,
    path,
    code,
    message: error instanceof Error ? error.message : String(error),
    fatal,
  };
}
