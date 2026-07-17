import { readFile, realpath, stat } from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";

import type { JsonObject, JsonValue } from "../contracts.ts";
import { canonicalize, cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type { PermissionMode, PermissionRuleRecord, PermissionRuleSource } from "../e02/contracts.ts";
import { PermissionRuleParser } from "./rule-parser.ts";

export interface PermissionSettingsSource {
  sourceId: string;
  source: PermissionRuleSource;
  path: string;
  required: boolean;
  managed: boolean;
  priority: number;
  revision: number;
  workspaceRoot: string;
  maximumBytes: number;
  metadata: JsonObject;
}

export interface PermissionSettingsDocument {
  documentId: string;
  sourceId: string;
  source: PermissionRuleSource;
  path: string;
  realPath: string;
  sourceRevision: number;
  contentDigest: string;
  mode: PermissionMode | null;
  rules: PermissionRuleRecord[];
  disabledRuleIds: string[];
  hookConfiguration: JsonObject;
  approvalConfiguration: JsonObject;
  loadedAt: string;
  metadata: JsonObject;
}

export interface PermissionSettingsFailure {
  sourceId: string;
  path: string;
  code: string;
  message: string;
  fatal: boolean;
  occurredAt: string;
}

export interface PermissionSettingsRevision {
  revisionId: string;
  revisionBefore: number;
  revisionAfter: number;
  documents: PermissionSettingsDocument[];
  effectiveRules: PermissionRuleRecord[];
  effectiveMode: PermissionMode | null;
  failures: PermissionSettingsFailure[];
  added: string[];
  updated: string[];
  removed: string[];
  unchanged: string[];
  committedAt: string;
  digest: string;
  metadata: JsonObject;
}

export interface PermissionSettingsSnapshot {
  version: "zyra.permission-settings/v1";
  revision: number;
  documents: PermissionSettingsDocument[];
  revisions: PermissionSettingsRevision[];
  digest: string;
  capturedAt: string;
}

const sourceOrder: Record<PermissionRuleSource, number> = {
  managed: 900,
  policy: 800,
  session: 700,
  "standing-grant": 650,
  workspace: 600,
  project: 500,
  user: 400,
  plugin: 300,
  command: 200,
};

export class PermissionSettingsRuntime {
  private readonly parser = new PermissionRuleParser();
  private readonly documents = new Map<string, PermissionSettingsDocument>();
  private readonly revisions = new Map<number, PermissionSettingsRevision>();
  private readonly now: () => Date;
  private readonly maximumRevisions: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumRevisions?: number; snapshot?: PermissionSettingsSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumRevisions = options.maximumRevisions ?? 100;
    if (options.snapshot) this.restore(options.snapshot);
  }

  async load(
    sourcesValue: readonly PermissionSettingsSource[],
    expectedRevision = this.revision,
    metadata: JsonObject = {},
  ): Promise<PermissionSettingsRevision> {
    if (expectedRevision !== this.revision) throw settingsError("settings_revision_conflict", `permission settings revision ${expectedRevision} does not match ${this.revision}`);
    const sources = sourcesValue.map(normalizeSource).sort(compareSource);
    const seen = new Set<string>();
    const next = new Map<string, PermissionSettingsDocument>();
    const failures: PermissionSettingsFailure[] = [];
    for (const source of sources) {
      if (seen.has(source.sourceId)) throw settingsError("duplicate_settings_source", `duplicate permission settings source ${source.sourceId}`);
      seen.add(source.sourceId);
      try {
        const document = await this.loadSource(source);
        next.set(source.sourceId, document);
      } catch (error) {
        const failure: PermissionSettingsFailure = {
          sourceId: source.sourceId,
          path: source.path,
          code: errorCode(error),
          message: error instanceof Error ? error.message : String(error),
          fatal: source.required || source.managed,
          occurredAt: this.timestamp(),
        };
        failures.push(failure);
        const prior = this.documents.get(source.sourceId);
        if (prior && !failure.fatal) next.set(source.sourceId, cloneJson(prior));
      }
    }
    const fatal = failures.filter((failure) => failure.fatal);
    if (fatal.length) throw settingsError("required_settings_load_failed", `${fatal.length} required permission settings sources failed`, { failures: canonicalize(fatal) as JsonValue });
    const added: string[] = [];
    const updated: string[] = [];
    const removed: string[] = [];
    const unchanged: string[] = [];
    for (const id of new Set([...this.documents.keys(), ...next.keys()])) {
      const before = this.documents.get(id);
      const after = next.get(id);
      if (!before && after) added.push(id);
      else if (before && !after) removed.push(id);
      else if (before && after && before.contentDigest !== after.contentDigest) updated.push(id);
      else if (after) unchanged.push(id);
    }
    const effective = computeEffective([...next.values()]);
    const committedAt = this.timestamp();
    const base = {
      revisionBefore: this.revision,
      revisionAfter: this.revision + 1,
      documents: [...next.values()].sort(compareDocument).map(cloneJson),
      effectiveRules: effective.rules,
      effectiveMode: effective.mode,
      failures,
      added: added.sort(),
      updated: updated.sort(),
      removed: removed.sort(),
      unchanged: unchanged.sort(),
      committedAt,
      metadata: cloneJson(metadata),
    };
    const revisionId = deterministicId("permission-settings-revision", base, 40);
    const revision: PermissionSettingsRevision = { revisionId, ...base, digest: digest({ revisionId, ...base }) };
    this.documents.clear();
    for (const [id, document] of next) this.documents.set(id, document);
    this.revision += 1;
    this.revisions.set(this.revision, revision);
    this.trim();
    return cloneJson(revision);
  }

  effective(revision = this.revision): { rules: PermissionRuleRecord[]; mode: PermissionMode | null; digest: string } {
    const record = this.revisions.get(revision);
    if (!record) {
      if (revision === 0) return { rules: [], mode: null, digest: digest({ rules: [], mode: null }) };
      throw settingsError("settings_revision_not_found", `permission settings revision ${revision} was not found`);
    }
    return {
      rules: record.effectiveRules.map(cloneJson),
      mode: record.effectiveMode,
      digest: digest({ rules: record.effectiveRules, mode: record.effectiveMode }),
    };
  }

  get(sourceId: string): PermissionSettingsDocument | null {
    const document = this.documents.get(sourceId);
    return document ? cloneJson(document) : null;
  }

  history(): PermissionSettingsRevision[] {
    return [...this.revisions.values()].sort((left, right) => left.revisionAfter - right.revisionAfter).map(cloneJson);
  }

  snapshot(): PermissionSettingsSnapshot {
    const withoutDigest = {
      version: "zyra.permission-settings/v1" as const,
      revision: this.revision,
      documents: [...this.documents.values()].sort(compareDocument).map(cloneJson),
      revisions: this.history(),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: PermissionSettingsSnapshot): void {
    if (snapshot.version !== "zyra.permission-settings/v1") throw settingsError("unsupported_settings_snapshot", "unsupported permission settings snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw settingsError("settings_snapshot_digest_mismatch", "permission settings snapshot digest mismatch");
    this.documents.clear();
    this.revisions.clear();
    this.revision = snapshot.revision;
    for (const document of snapshot.documents) this.documents.set(document.sourceId, cloneJson(document));
    for (const revision of snapshot.revisions) this.revisions.set(revision.revisionAfter, cloneJson(revision));
    if (this.revision > 0 && !this.revisions.has(this.revision)) throw settingsError("settings_head_missing", "permission settings snapshot lacks head revision");
  }

  private async loadSource(source: PermissionSettingsSource): Promise<PermissionSettingsDocument> {
    const workspaceRoot = resolve(source.workspaceRoot);
    const path = resolve(workspaceRoot, source.path);
    if (!contains(workspaceRoot, path)) throw settingsError("settings_path_escape", `permission settings ${source.path} escapes workspace`);
    const realPath = await realpath(path);
    if (!contains(workspaceRoot, realPath)) throw settingsError("settings_symlink_escape", `permission settings ${source.path} resolves outside workspace`);
    const stats = await stat(realPath);
    if (!stats.isFile()) throw settingsError("settings_not_file", `permission settings ${source.path} is not a regular file`);
    if (stats.size > source.maximumBytes) throw settingsError("settings_oversized", `permission settings ${source.path} exceeds ${source.maximumBytes} bytes`);
    const content = await readFile(realPath, "utf8");
    let value: JsonObject;
    try {
      const parsed = JSON.parse(content) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("root must be object");
      value = canonicalize(parsed) as JsonObject;
    } catch (error) {
      throw settingsError("settings_json_invalid", `invalid permission settings ${source.path}: ${error instanceof Error ? error.message : String(error)}`);
    }
    const rawRules = Array.isArray(value.rules) ? value.rules : [];
    const rules = rawRules.map((rule, index) => this.parser.parse(
      typeof rule === "string" ? rule : canonicalize(rule) as JsonObject,
      {
        source: source.source,
        priority: source.priority - index,
        now: this.timestamp(),
        metadata: { settings_source_id: source.sourceId, settings_path: source.path },
      },
    ));
    const mode = modeValue(value.mode);
    const loadedAt = this.timestamp();
    const contentDigest = digest(content);
    const documentBase = {
      sourceId: source.sourceId,
      source: source.source,
      path: source.path,
      realPath,
      sourceRevision: source.revision,
      contentDigest,
      mode,
      rules,
      disabledRuleIds: stringArray(value.disabled_rule_ids ?? value.disabledRuleIds),
      hookConfiguration: objectValue(value.hooks),
      approvalConfiguration: objectValue(value.approval),
      loadedAt,
      metadata: { ...cloneJson(source.metadata), directory: dirname(realPath) },
    };
    return {
      documentId: deterministicId("permission-settings-document", documentBase, 40),
      ...documentBase,
    };
  }

  private trim(): void {
    while (this.revisions.size > this.maximumRevisions) {
      const oldest = [...this.revisions.keys()].sort((left, right) => left - right)[0];
      if (oldest === undefined || oldest === this.revision) break;
      this.revisions.delete(oldest);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function normalizeSource(value: PermissionSettingsSource): PermissionSettingsSource {
  const source = cloneJson(value);
  if (!source.sourceId || !source.path || !source.workspaceRoot) throw settingsError("settings_source_incomplete", "permission settings source identity is incomplete");
  if (!Number.isSafeInteger(source.revision) || source.revision < 0) throw settingsError("settings_source_revision_invalid", `settings source ${source.sourceId} revision is invalid`);
  if (!Number.isSafeInteger(source.maximumBytes) || source.maximumBytes < 1) source.maximumBytes = 4 * 1024 * 1024;
  return source;
}

function compareSource(left: PermissionSettingsSource, right: PermissionSettingsSource): number {
  return sourceOrder[right.source] - sourceOrder[left.source] || right.priority - left.priority || left.sourceId.localeCompare(right.sourceId);
}

function compareDocument(left: PermissionSettingsDocument, right: PermissionSettingsDocument): number {
  return sourceOrder[right.source] - sourceOrder[left.source] || left.sourceId.localeCompare(right.sourceId);
}

function computeEffective(documents: PermissionSettingsDocument[]): { rules: PermissionRuleRecord[]; mode: PermissionMode | null } {
  const ordered = [...documents].sort(compareDocument);
  const disabled = new Set(ordered.flatMap((document) => document.disabledRuleIds));
  const byId = new Map<string, PermissionRuleRecord>();
  for (const document of ordered) {
    for (const rule of document.rules) {
      if (disabled.has(rule.ruleId)) continue;
      if (!byId.has(rule.ruleId)) byId.set(rule.ruleId, cloneJson(rule));
    }
  }
  const mode = ordered.find((document) => document.mode !== null)?.mode ?? null;
  return {
    rules: [...byId.values()].sort((left, right) => right.priority - left.priority || left.ruleId.localeCompare(right.ruleId)),
    mode,
  };
}

function modeValue(value: JsonValue | undefined): PermissionMode | null {
  const modes: PermissionMode[] = ["default", "acceptEdits", "dontAsk", "plan", "bypassPermissions", "auto", "sealed"];
  if (value === undefined || value === null || value === "") return null;
  if (typeof value !== "string" || !modes.includes(value as PermissionMode)) throw settingsError("settings_mode_invalid", `invalid permission mode ${String(value)}`);
  return value as PermissionMode;
}

function stringArray(value: JsonValue | undefined): string[] {
  if (!Array.isArray(value)) return [];
  const output: string[] = [];
  for (const item of value) {
    if (typeof item !== "string" || !item.trim()) throw settingsError("settings_string_array_invalid", "settings string list contains invalid value");
    output.push(item);
  }
  return [...new Set(output)].sort();
}

function objectValue(value: JsonValue | undefined): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? cloneJson(value as JsonObject) : {};
}

function contains(parent: string, child: string): boolean {
  const value = relative(parent, child);
  return value === "" || (!value.startsWith("..") && !resolve(value).startsWith("/"));
}

function errorCode(error: unknown): string {
  return error && typeof error === "object" && "code" in error && typeof error.code === "string" ? error.code : "settings_load_failed";
}

function settingsError(code: string, message: string, details: JsonObject = {}): Error {
  return Object.assign(new Error(message), { name: "PermissionSettingsError", code, details });
}
