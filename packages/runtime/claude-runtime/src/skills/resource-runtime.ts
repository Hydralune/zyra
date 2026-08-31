import { lstat, readFile, realpath } from "node:fs/promises";
import { relative, resolve, sep } from "node:path";

import { cloneJson, digest, monotonicNow } from "../e02/index.ts";
import type {
  SkillDescriptor,
  SkillResourceContent,
  SkillResourceDescriptor,
} from "./contracts-v2.ts";

export class SkillResourceRuntime {
  private readonly workspaceRoot: string;
  private readonly allowOutsideWorkspace: boolean;
  private readonly maximumTotalBytes: number;
  private readonly now: () => Date;
  private readonly cache = new Map<string, SkillResourceContent>();
  private lastTimestamp: string | null = null;

  constructor(options: {
    workspaceRoot: string;
    allowOutsideWorkspace?: boolean;
    maximumTotalBytes?: number;
    now?: () => Date;
  }) {
    this.workspaceRoot = resolve(options.workspaceRoot);
    this.allowOutsideWorkspace = options.allowOutsideWorkspace ?? false;
    this.maximumTotalBytes = options.maximumTotalBytes ?? 32 * 1024 * 1024;
    this.now = options.now ?? (() => new Date());
  }

  async load(skill: SkillDescriptor, selected?: string[]): Promise<SkillResourceContent[]> {
    const selection = selected ? new Set(selected) : null;
    const descriptors = skill.resources.filter((resource) =>
      resource.required
      || !selection
      || selection.has(resource.resourceId)
      || selection.has(resource.path)
    );
    const output: SkillResourceContent[] = [];
    let totalBytes = 0;
    for (const resource of descriptors) {
      try {
        const content = await this.loadOne(skill, resource);
        totalBytes += content.sizeBytes;
        if (totalBytes > this.maximumTotalBytes) throw new Error(`skill resources exceed ${this.maximumTotalBytes} bytes`);
        output.push(content);
      } catch (error) {
        if (resource.required) throw error;
      }
    }
    return output;
  }

  clear(skillId?: string): void {
    if (!skillId) {
      this.cache.clear();
      return;
    }
    for (const [key, value] of this.cache) if (value.skillId === skillId) this.cache.delete(key);
  }

  private async loadOne(skill: SkillDescriptor, resource: SkillResourceDescriptor): Promise<SkillResourceContent> {
    const requested = resolve(skill.source.skillDirectory, resource.path);
    const real = await realpath(requested);
    this.assertPath(real, skill.source.skillDirectory);
    const metadata = await lstat(real);
    if (!metadata.isFile()) throw new Error(`skill resource ${resource.path} is not a file`);
    if (metadata.size > resource.maximumBytes) throw new Error(`skill resource ${resource.path} exceeds ${resource.maximumBytes} bytes`);
    const cacheKey = `${real}\0${metadata.mtimeMs}\0${metadata.size}`;
    const cached = this.cache.get(cacheKey);
    if (cached) return cloneJson(cached);
    const bytes = await readFile(real);
    const contentDigest = digest(bytes.toString("base64"));
    if (resource.digest && resource.digest !== contentDigest) throw new Error(`skill resource ${resource.path} digest mismatch`);
    const textKind = resource.kind === "markdown" || resource.kind === "text" || resource.kind === "json" || resource.kind === "yaml";
    let text: string | null = null;
    let bytesBase64: string | null = null;
    if (textKind) {
      text = new TextDecoder(resource.charset ?? "utf-8", { fatal: true }).decode(bytes).replace(/\r\n?/g, "\n");
      if (resource.kind === "json") JSON.parse(text);
    } else {
      bytesBase64 = bytes.toString("base64");
    }
    const content: SkillResourceContent = {
      resourceId: resource.resourceId,
      skillId: skill.skillId,
      path: resource.path,
      kind: resource.kind,
      mediaType: resource.mediaType,
      sizeBytes: metadata.size,
      digest: contentDigest,
      text,
      bytesBase64,
      tokenEstimate: text ? estimateTokens(text) : 0,
      loadedAt: this.timestamp(),
      metadata: {
        real_path_digest: digest(real),
        modified_at_ms: metadata.mtimeMs,
      },
    };
    this.cache.set(cacheKey, content);
    return cloneJson(content);
  }

  private assertPath(path: string, skillDirectory: string): void {
    const normalized = resolve(path).toLowerCase();
    const skillRoot = resolve(skillDirectory).toLowerCase();
    const workspace = this.workspaceRoot.toLowerCase();
    const insideSkill = normalized === skillRoot || normalized.startsWith(`${skillRoot}${sep}`);
    const insideWorkspace = normalized === workspace || normalized.startsWith(`${workspace}${sep}`);
    if (!insideSkill) throw new Error(`skill resource escapes skill directory: ${relative(skillDirectory, path)}`);
    if (!this.allowOutsideWorkspace && !insideWorkspace) throw new Error(`skill resource is outside workspace: ${path}`);
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function estimateTokens(value: string): number {
  const pieces = value.match(/[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu) ?? [];
  return pieces.reduce((total, piece) => total + Math.max(1, Math.ceil(piece.length / 4)), 0);
}
