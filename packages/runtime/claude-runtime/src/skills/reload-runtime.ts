import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type {
  SkillDescriptor,
  SkillReloadScan,
  SkillSourceFile,
  SkillSourceRoot,
  SkillRevision,
} from "./contracts-v2.ts";
import { SkillFrontmatterRuntime } from "./frontmatter-runtime.ts";
import { SkillRegistryRuntime } from "./registry-runtime.ts";
import { SkillSourceRuntime } from "./source-runtime.ts";

export class SkillReloadRuntime {
  private readonly sources: SkillSourceRuntime;
  private readonly parser: SkillFrontmatterRuntime;
  private readonly registry: SkillRegistryRuntime;
  private readonly now: () => Date;
  private readonly scans = new Map<string, SkillReloadScan>();
  private readonly lastSources = new Map<string, SkillSourceFile>();
  private lastTimestamp: string | null = null;

  constructor(options: {
    sources: SkillSourceRuntime;
    parser: SkillFrontmatterRuntime;
    registry: SkillRegistryRuntime;
    now?: () => Date;
  }) {
    this.sources = options.sources;
    this.parser = options.parser;
    this.registry = options.registry;
    this.now = options.now ?? (() => new Date());
  }

  static commitAtomicReplacement<T>(input: {
    currentRevision: number;
    expectedRevision: number;
    staged: readonly T[];
    replace: (staged: readonly T[], expectedRevision: number) => number;
  }): number {
    if (!Number.isSafeInteger(input.currentRevision) || input.currentRevision < 0) {
      throw new Error("atomic reload current revision is invalid");
    }
    if (input.expectedRevision !== input.currentRevision) {
      throw new Error(
        `atomic reload revision ${input.expectedRevision} does not match ${input.currentRevision}`,
      );
    }
    const staged = [...input.staged];
    const revision = input.replace(staged, input.expectedRevision);
    if (revision !== input.currentRevision + 1) {
      throw new Error(
        `atomic reload replacement returned revision ${revision}; expected ${input.currentRevision + 1}`,
      );
    }
    return revision;
  }

  async scan(roots: SkillSourceRoot[]): Promise<SkillReloadScan> {
    const startedAt = this.timestamp();
    const discovery = await this.sources.discover(roots);
    const descriptors: SkillDescriptor[] = [];
    const errors: JsonObject[] = discovery.errors.map(cloneJson);
    for (const source of discovery.files) {
      try {
        descriptors.push(await this.parser.parse(source));
      } catch (error) {
        errors.push({
          source_id: source.sourceId,
          path: source.manifestPath,
          code: "skill_parse_failed",
          message: error instanceof Error ? error.message : String(error),
          fatal: false,
        });
      }
    }
    const current = new Map(discovery.files.map((source) => [sourceKey(source), source]));
    const addedPaths: string[] = [];
    const updatedPaths: string[] = [];
    const removedPaths: string[] = [];
    const unchangedPaths: string[] = [];
    for (const key of new Set([...this.lastSources.keys(), ...current.keys()])) {
      const before = this.lastSources.get(key);
      const after = current.get(key);
      if (!before && after) addedPaths.push(after.manifestPath);
      else if (before && !after) removedPaths.push(before.manifestPath);
      else if (before && after && before.contentDigest !== after.contentDigest) updatedPaths.push(after.manifestPath);
      else if (after) unchangedPaths.push(after.manifestPath);
    }
    const completedAt = this.timestamp();
    const base = {
      baseRevision: this.registry.revision,
      roots: roots.map(cloneJson),
      sources: discovery.files.map(cloneJson),
      descriptors: descriptors.map(cloneJson),
      errors,
      addedPaths: addedPaths.sort(),
      updatedPaths: updatedPaths.sort(),
      removedPaths: removedPaths.sort(),
      unchangedPaths: unchangedPaths.sort(),
      startedAt,
      completedAt,
    };
    const scan: SkillReloadScan = {
      scanId: deterministicId("skill-reload-scan", base, 32),
      ...base,
      digest: digest(base),
    };
    this.scans.set(scan.scanId, scan);
    return cloneJson(scan);
  }

  commit(scanId: string, expectedRevision: number, metadata: JsonObject = {}): SkillRevision {
    const scan = this.scans.get(scanId);
    if (!scan) throw new Error(`skill reload scan ${scanId} was not found`);
    if (scan.baseRevision !== expectedRevision || expectedRevision !== this.registry.revision) {
      throw new Error(`skill reload scan ${scanId} is stale: base ${scan.baseRevision}, expected ${expectedRevision}, current ${this.registry.revision}`);
    }
    const fatal = scan.errors.filter((error) => error.fatal === true);
    if (fatal.length) throw new Error(`skill reload scan ${scanId} has ${fatal.length} fatal errors`);
    const revision = this.registry.commitRevision({
      baseRevision: expectedRevision,
      descriptors: scan.descriptors,
      metadata: {
        ...metadata,
        scan_id: scanId,
        scan_digest: scan.digest,
        parse_error_count: scan.errors.length,
      },
    });
    this.lastSources.clear();
    for (const source of scan.sources) this.lastSources.set(sourceKey(source), cloneJson(source));
    this.scans.delete(scanId);
    return revision;
  }

  discard(scanId: string): boolean {
    return this.scans.delete(scanId);
  }

  get(scanId: string): SkillReloadScan | null {
    const value = this.scans.get(scanId);
    return value ? cloneJson(value) : null;
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function sourceKey(source: SkillSourceFile): string {
  return `${source.sourceId}\0${source.realPath.toLowerCase()}`;
}
