import { lstat, readFile, readdir, realpath } from "node:fs/promises";
import {
  basename,
  dirname,
  extname,
  join,
  relative,
  resolve,
  sep,
} from "node:path";

import type { JsonObject } from "../contracts.ts";
import {
  createId,
  E03RuntimeError,
  digest,
  requireString,
  type E03Clock,
  type E03AgentDefinition,
  SystemE03Clock,
} from "../e03/contracts.ts";
import {
  AgentDefinitionRegistry,
  type DefinitionInput,
} from "./definition-registry.ts";

export interface AgentDefinitionDirectory {
  root: string;
  source: E03AgentDefinition["source"];
  maximumDepth?: number;
  maximumFiles?: number;
  followSymlinks?: boolean;
}

export interface LoadedDefinitionFile {
  path: string;
  relativePath: string;
  source: E03AgentDefinition["source"];
  definition: E03AgentDefinition;
  contentDigest: string;
}

export interface DefinitionLoadReport {
  roots: string[];
  loaded: LoadedDefinitionFile[];
  ignored: Array<{ path: string; reason: string }>;
  errors: Array<{ path: string; code: string; error: string }>;
  generation: string;
}

export class AgentDefinitionLoader {
  constructor(private readonly registry: AgentDefinitionRegistry) {}

  async loadDirectory(
    specification: AgentDefinitionDirectory,
  ): Promise<DefinitionLoadReport> {
    const root = resolve(specification.root);
    const maximumDepth = specification.maximumDepth ?? 8;
    const maximumFiles = specification.maximumFiles ?? 2_048;
    if (maximumDepth < 0 || maximumDepth > 64)
      throw new E03RuntimeError(
        "invalid_definition_root",
        "maximum definition depth is out of range",
      );
    if (maximumFiles < 1 || maximumFiles > 100_000)
      throw new E03RuntimeError(
        "invalid_definition_root",
        "maximum definition file count is out of range",
      );
    const rootReal = await realpath(root).catch(() => root);
    const report: DefinitionLoadReport = {
      roots: [rootReal],
      loaded: [],
      ignored: [],
      errors: [],
      generation: "",
    };
    const queue: Array<{ path: string; depth: number }> = [
      { path: rootReal, depth: 0 },
    ];
    const visited = new Set<string>();
    while (queue.length) {
      const current = queue.shift()!;
      let currentReal: string;
      try {
        currentReal = await realpath(current.path);
      } catch (error) {
        report.errors.push({
          path: current.path,
          code: "unreadable_path",
          error: String(error),
        });
        continue;
      }
      if (!within(rootReal, currentReal)) {
        report.errors.push({
          path: current.path,
          code: "definition_path_escape",
          error: `resolved path escapes ${rootReal}`,
        });
        continue;
      }
      if (visited.has(currentReal)) continue;
      visited.add(currentReal);
      let entries;
      try {
        entries = await readdir(currentReal, { withFileTypes: true });
      } catch (error) {
        report.errors.push({
          path: current.path,
          code: "unreadable_directory",
          error: String(error),
        });
        continue;
      }
      entries.sort((left, right) => left.name.localeCompare(right.name));
      for (const entry of entries) {
        const path = join(currentReal, entry.name);
        if (entry.isDirectory()) {
          if (current.depth >= maximumDepth)
            report.ignored.push({ path, reason: "maximum_depth" });
          else queue.push({ path, depth: current.depth + 1 });
          continue;
        }
        if (entry.isSymbolicLink()) {
          if (!specification.followSymlinks) {
            report.ignored.push({ path, reason: "symlink" });
            continue;
          }
          const target = await realpath(path).catch(() => "");
          if (!target || !within(rootReal, target)) {
            report.errors.push({
              path,
              code: "symlink_escape",
              error: "symlink target escapes definition root",
            });
            continue;
          }
          const stats = await lstat(target);
          if (stats.isDirectory())
            queue.push({ path: target, depth: current.depth + 1 });
          else
            await this.loadFile(
              path,
              target,
              rootReal,
              specification.source,
              report,
            );
          continue;
        }
        if (!entry.isFile()) {
          report.ignored.push({ path, reason: "unsupported_entry" });
          continue;
        }
        if (![".md", ".json"].includes(extname(entry.name).toLowerCase())) {
          report.ignored.push({ path, reason: "unsupported_extension" });
          continue;
        }
        await this.loadFile(path, path, rootReal, specification.source, report);
        if (report.loaded.length + report.errors.length > maximumFiles)
          throw new E03RuntimeError(
            "definition_file_limit",
            `definition directory exceeds ${maximumFiles} files`,
          );
      }
    }
    report.generation = digest({
      roots: report.roots,
      loaded: report.loaded.map((item) => [
        item.relativePath,
        item.definition.digest,
      ]),
      errors: report.errors,
    });
    return report;
  }

  validate(
    input: DefinitionInput,
    source: E03AgentDefinition["source"],
    path = "<memory>",
  ): E03AgentDefinition {
    const value = {
      ...input,
      source,
      metadata: { ...asObject(input.metadata), definition_path: path },
    };
    const staging = new AgentDefinitionRegistry();
    return staging.register(value);
  }

  async loadDirectories(
    specifications: readonly AgentDefinitionDirectory[],
  ): Promise<DefinitionLoadReport> {
    const reports: DefinitionLoadReport[] = [];
    for (const specification of specifications)
      reports.push(await this.loadDirectory(specification));
    return {
      roots: reports.flatMap((report) => report.roots),
      loaded: reports.flatMap((report) => report.loaded),
      ignored: reports.flatMap((report) => report.ignored),
      errors: reports.flatMap((report) => report.errors),
      generation: digest(reports.map((report) => report.generation)),
    };
  }

  private async loadFile(
    displayPath: string,
    actualPath: string,
    root: string,
    source: E03AgentDefinition["source"],
    report: DefinitionLoadReport,
  ): Promise<void> {
    let content: string;
    try {
      content = await readFile(actualPath, "utf8");
    } catch (error) {
      report.errors.push({
        path: displayPath,
        code: "unreadable_file",
        error: String(error),
      });
      return;
    }
    try {
      const parsed =
        extname(displayPath).toLowerCase() === ".json"
          ? parseJson(content, displayPath)
          : parseMarkdown(content, displayPath);
      const definition = this.validate(parsed, source, displayPath);
      this.registry.register(definition);
      report.loaded.push({
        path: displayPath,
        relativePath: relative(root, displayPath).replaceAll(sep, "/"),
        source,
        definition,
        contentDigest: digest(content),
      });
    } catch (error) {
      report.errors.push({
        path: displayPath,
        code:
          error instanceof E03RuntimeError ? error.code : "invalid_definition",
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }
}

function parseJson(content: string, path: string): DefinitionInput {
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch (error) {
    throw new E03RuntimeError(
      "invalid_definition_json",
      `${path}: ${String(error)}`,
    );
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
    throw new E03RuntimeError(
      "invalid_definition_json",
      `${path} must contain an object`,
    );
  return parsed as DefinitionInput;
}

function parseMarkdown(content: string, path: string): DefinitionInput {
  const normalized = content.replaceAll("\r", "");
  if (!normalized.startsWith("---\n")) {
    return {
      name: basename(path, extname(path)),
      description: firstParagraph(normalized),
      systemPrompt: normalized,
    };
  }
  const closing = normalized.indexOf("\n---\n", 4);
  if (closing < 0)
    throw new E03RuntimeError(
      "invalid_definition_frontmatter",
      `${path} has unterminated frontmatter`,
    );
  const header = normalized.slice(4, closing);
  const body = normalized.slice(closing + 5).trim();
  const fields: Record<string, unknown> = {};
  for (const [index, line] of header.split("\n").entries()) {
    if (!line.trim() || line.trimStart().startsWith("#")) continue;
    const separator = line.indexOf(":");
    if (separator < 1)
      throw new E03RuntimeError(
        "invalid_definition_frontmatter",
        `${path}:${index + 2} is not key: value`,
      );
    const key = line.slice(0, separator).trim();
    const raw = line.slice(separator + 1).trim();
    if (key in fields)
      throw new E03RuntimeError(
        "invalid_definition_frontmatter",
        `${path} repeats ${key}`,
      );
    fields[key] = parseScalar(raw);
  }
  const name = requireString(
    fields.name ?? basename(path, extname(path)),
    "agent.name",
    1,
    128,
  );
  const description = requireString(
    fields.description ?? firstParagraph(body),
    "agent.description",
    3,
    4_096,
  );
  return {
    ...fields,
    name,
    description,
    systemPrompt: body,
    tools: normalizeList(fields.tools),
    deniedTools: normalizeList(fields.denied_tools ?? fields.deniedTools),
    skills: normalizeList(fields.skills),
    mcpServers: normalizeList(fields.mcp_servers ?? fields.mcpServers),
  } as DefinitionInput;
}

function parseScalar(raw: string): unknown {
  if (!raw) return "";
  if (raw === "true") return true;
  if (raw === "false") return false;
  if (/^-?\d+$/.test(raw)) return Number(raw);
  if (raw.startsWith("[") && raw.endsWith("]"))
    return raw
      .slice(1, -1)
      .split(",")
      .map((item) => stripQuotes(item.trim()))
      .filter(Boolean);
  return stripQuotes(raw);
}

function stripQuotes(value: string): string {
  if (
    (value.startsWith('"') && value.endsWith('"')) ||
    (value.startsWith("'") && value.endsWith("'"))
  )
    return value.slice(1, -1);
  return value;
}

function normalizeList(value: unknown): string[] {
  if (Array.isArray(value))
    return value
      .map(String)
      .map((item) => item.trim())
      .filter(Boolean);
  if (typeof value === "string")
    return value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  return [];
}

function firstParagraph(content: string): string {
  const paragraph =
    content
      .split(/\n\s*\n/)
      .map((item) => item.replace(/^#+\s*/gm, "").trim())
      .find(Boolean) ?? "";
  return paragraph.slice(0, 4_096);
}

function within(root: string, candidate: string): boolean {
  const normalizedRoot = resolve(root);
  const normalizedCandidate = resolve(candidate);
  return (
    normalizedCandidate === normalizedRoot ||
    normalizedCandidate.startsWith(`${normalizedRoot}${sep}`)
  );
}

function asObject(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

export type DefinitionDiagnosticSeverity = "info" | "warning" | "error";

export interface DefinitionDiagnostic {
  diagnosticId: string;
  path: string;
  relativePath: string;
  source: E03AgentDefinition["source"];
  severity: DefinitionDiagnosticSeverity;
  code: string;
  message: string;
  field: string | null;
  line: number | null;
  column: number | null;
  contentDigest: string | null;
  generation: string;
  digest: string;
}

export interface DefinitionInventoryEntry {
  path: string;
  relativePath: string;
  source: E03AgentDefinition["source"];
  agentId: string;
  definitionName: string;
  contentDigest: string;
  definitionDigest: string;
  generation: string;
  active: boolean;
  loadedAt: string;
  digest: string;
}

export interface DefinitionInventorySnapshot {
  snapshotId: string;
  root: string;
  generation: string;
  entries: DefinitionInventoryEntry[];
  diagnosticIds: string[];
  createdAt: string;
  previousSnapshotId: string | null;
  digest: string;
}

export interface DefinitionInventoryDiff {
  diffId: string;
  root: string;
  fromSnapshotId: string | null;
  toSnapshotId: string;
  addedPaths: string[];
  changedPaths: string[];
  removedPaths: string[];
  unchangedPaths: string[];
  conflictingAgentIds: string[];
  createdAt: string;
  digest: string;
}

function assertDefinitionInventoryEntry(entry: DefinitionInventoryEntry): void {
  const { digest: checksum, ...payload } = entry;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "definition_inventory_entry_digest",
      `definition inventory entry ${entry.relativePath} digest is invalid`,
    );
  if (
    !entry.path ||
    !entry.relativePath ||
    !entry.agentId ||
    !entry.definitionName ||
    !entry.contentDigest ||
    !entry.definitionDigest ||
    !entry.generation
  )
    throw new E03RuntimeError(
      "definition_inventory_entry_identity",
      "definition inventory entry identity is incomplete",
    );
}

function assertDefinitionDiagnostic(diagnostic: DefinitionDiagnostic): void {
  const { digest: checksum, ...payload } = diagnostic;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "definition_diagnostic_digest",
      `definition diagnostic ${diagnostic.diagnosticId} digest is invalid`,
    );
  if (
    !diagnostic.diagnosticId ||
    !diagnostic.path ||
    !diagnostic.relativePath ||
    !diagnostic.code ||
    !diagnostic.message ||
    !diagnostic.generation
  )
    throw new E03RuntimeError(
      "definition_diagnostic_identity",
      "definition diagnostic identity is incomplete",
    );
  if (
    diagnostic.line !== null &&
    (!Number.isSafeInteger(diagnostic.line) || diagnostic.line < 1)
  )
    throw new E03RuntimeError(
      "definition_diagnostic_location",
      "definition diagnostic line is invalid",
    );
  if (
    diagnostic.column !== null &&
    (!Number.isSafeInteger(diagnostic.column) || diagnostic.column < 1)
  )
    throw new E03RuntimeError(
      "definition_diagnostic_location",
      "definition diagnostic column is invalid",
    );
}

function assertDefinitionInventorySnapshot(
  snapshot: DefinitionInventorySnapshot,
): void {
  const { digest: checksum, ...payload } = snapshot;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "definition_inventory_snapshot_digest",
      `definition inventory snapshot ${snapshot.snapshotId} digest is invalid`,
    );
  if (!snapshot.snapshotId || !snapshot.root || !snapshot.generation)
    throw new E03RuntimeError(
      "definition_inventory_snapshot_identity",
      "definition inventory snapshot identity is incomplete",
    );
  const paths = new Set<string>();
  for (const entry of snapshot.entries) {
    assertDefinitionInventoryEntry(entry);
    if (paths.has(entry.path))
      throw new E03RuntimeError(
        "definition_inventory_snapshot_duplicate",
        `definition inventory snapshot contains duplicate path ${entry.path}`,
      );
    paths.add(entry.path);
  }
}

export class DefinitionInventoryRuntime {
  private snapshots = new Map<string, DefinitionInventorySnapshot>();
  private diagnostics = new Map<string, DefinitionDiagnostic>();
  private latestByRoot = new Map<string, string>();

  capture(input: {
    root: string;
    report: DefinitionLoadReport;
    loadedAt: string;
  }): { snapshot: DefinitionInventorySnapshot; diff: DefinitionInventoryDiff } {
    const root = resolve(input.root);
    const previousId = this.latestByRoot.get(root) ?? null;
    const previous = previousId ? this.requireSnapshot(previousId) : null;
    const entries = input.report.loaded
      .map((loaded) => {
        const payload = {
          path: resolve(loaded.path),
          relativePath: loaded.relativePath,
          source: loaded.source,
          agentId: loaded.definition.name,
          definitionName: loaded.definition.name,
          contentDigest: loaded.contentDigest,
          definitionDigest: loaded.definition.digest,
          generation: input.report.generation,
          active: true,
          loadedAt: input.loadedAt,
        };
        const entry = { ...payload, digest: digest(payload) };
        assertDefinitionInventoryEntry(entry);
        return entry;
      })
      .sort((left, right) => left.path.localeCompare(right.path));
    const diagnostics: DefinitionDiagnostic[] = [];
    for (const ignored of input.report.ignored)
      diagnostics.push(
        this.makeDiagnostic({
          path: ignored.path,
          root,
          source: "project",
          severity: "info",
          code: "definition_ignored",
          message: ignored.reason,
          generation: input.report.generation,
        }),
      );
    for (const error of input.report.errors)
      diagnostics.push(
        this.makeDiagnostic({
          path: error.path,
          root,
          source: "project",
          severity: "error",
          code: error.code,
          message: error.error,
          generation: input.report.generation,
        }),
      );
    for (const diagnostic of diagnostics)
      this.diagnostics.set(diagnostic.diagnosticId, diagnostic);
    const payload = {
      snapshotId: `definition-inventory-${digest({ root, generation: input.report.generation }).slice(0, 24)}`,
      root,
      generation: input.report.generation,
      entries,
      diagnosticIds: diagnostics.map((diagnostic) => diagnostic.diagnosticId),
      createdAt: input.loadedAt,
      previousSnapshotId: previousId,
    };
    const snapshot = { ...payload, digest: digest(payload) };
    assertDefinitionInventorySnapshot(snapshot);
    this.snapshots.set(snapshot.snapshotId, structuredClone(snapshot));
    this.latestByRoot.set(root, snapshot.snapshotId);
    const diff = this.compare(previous, snapshot);
    return { snapshot: structuredClone(snapshot), diff };
  }

  compare(
    from: DefinitionInventorySnapshot | null,
    to: DefinitionInventorySnapshot,
  ): DefinitionInventoryDiff {
    assertDefinitionInventorySnapshot(to);
    if (from) {
      assertDefinitionInventorySnapshot(from);
      if (from.root !== to.root)
        throw new E03RuntimeError(
          "definition_inventory_diff_root",
          "definition inventory snapshots belong to different roots",
        );
    }
    const left = new Map(
      (from?.entries ?? []).map((entry) => [entry.path, entry]),
    );
    const right = new Map(to.entries.map((entry) => [entry.path, entry]));
    const addedPaths: string[] = [];
    const changedPaths: string[] = [];
    const removedPaths: string[] = [];
    const unchangedPaths: string[] = [];
    for (const [path, entry] of right) {
      const prior = left.get(path);
      if (!prior) addedPaths.push(path);
      else if (
        prior.contentDigest !== entry.contentDigest ||
        prior.definitionDigest !== entry.definitionDigest ||
        prior.active !== entry.active
      )
        changedPaths.push(path);
      else unchangedPaths.push(path);
    }
    for (const path of left.keys())
      if (!right.has(path)) removedPaths.push(path);
    const agents = new Map<string, number>();
    for (const entry of to.entries)
      agents.set(entry.agentId, (agents.get(entry.agentId) ?? 0) + 1);
    const conflictingAgentIds = [...agents]
      .filter(([, count]) => count > 1)
      .map(([agentId]) => agentId)
      .sort();
    const payload = {
      diffId: `definition-diff-${digest({ from: from?.snapshotId ?? null, to: to.snapshotId }).slice(0, 24)}`,
      root: to.root,
      fromSnapshotId: from?.snapshotId ?? null,
      toSnapshotId: to.snapshotId,
      addedPaths: addedPaths.sort(),
      changedPaths: changedPaths.sort(),
      removedPaths: removedPaths.sort(),
      unchangedPaths: unchangedPaths.sort(),
      conflictingAgentIds,
      createdAt: to.createdAt,
    };
    return { ...payload, digest: digest(payload) };
  }

  latest(root: string): DefinitionInventorySnapshot | null {
    const snapshotId = this.latestByRoot.get(resolve(root));
    return snapshotId
      ? structuredClone(this.requireSnapshot(snapshotId))
      : null;
  }

  diagnosticsFor(snapshotId: string): DefinitionDiagnostic[] {
    const snapshot = this.requireSnapshot(snapshotId);
    return snapshot.diagnosticIds.map((diagnosticId) => {
      const diagnostic = this.diagnostics.get(diagnosticId);
      if (!diagnostic)
        throw new E03RuntimeError(
          "definition_diagnostic_missing",
          `definition diagnostic ${diagnosticId} does not exist`,
        );
      assertDefinitionDiagnostic(diagnostic);
      return structuredClone(diagnostic);
    });
  }

  snapshot(): {
    inventories: DefinitionInventorySnapshot[];
    diagnostics: DefinitionDiagnostic[];
  } {
    return {
      inventories: [...this.snapshots.values()].map((snapshot) =>
        structuredClone(snapshot),
      ),
      diagnostics: [...this.diagnostics.values()].map((diagnostic) =>
        structuredClone(diagnostic),
      ),
    };
  }

  restore(input: {
    inventories: readonly DefinitionInventorySnapshot[];
    diagnostics: readonly DefinitionDiagnostic[];
  }): void {
    const snapshots = new Map<string, DefinitionInventorySnapshot>();
    const diagnostics = new Map<string, DefinitionDiagnostic>();
    const latestByRoot = new Map<string, string>();
    for (const raw of input.diagnostics) {
      const diagnostic = structuredClone(raw);
      assertDefinitionDiagnostic(diagnostic);
      if (diagnostics.has(diagnostic.diagnosticId))
        throw new E03RuntimeError(
          "definition_diagnostic_restore_duplicate",
          `duplicate definition diagnostic ${diagnostic.diagnosticId}`,
        );
      diagnostics.set(diagnostic.diagnosticId, diagnostic);
    }
    const ordered = [...input.inventories].sort((left, right) =>
      left.createdAt.localeCompare(right.createdAt),
    );
    for (const raw of ordered) {
      const snapshot = structuredClone(raw);
      assertDefinitionInventorySnapshot(snapshot);
      if (snapshots.has(snapshot.snapshotId))
        throw new E03RuntimeError(
          "definition_inventory_restore_duplicate",
          `duplicate definition inventory ${snapshot.snapshotId}`,
        );
      if (
        snapshot.previousSnapshotId !== null &&
        !snapshots.has(snapshot.previousSnapshotId)
      )
        throw new E03RuntimeError(
          "definition_inventory_restore_parent",
          `definition inventory ${snapshot.snapshotId} has a missing parent`,
        );
      for (const diagnosticId of snapshot.diagnosticIds)
        if (!diagnostics.has(diagnosticId))
          throw new E03RuntimeError(
            "definition_inventory_restore_diagnostic",
            `definition inventory ${snapshot.snapshotId} has a missing diagnostic`,
          );
      snapshots.set(snapshot.snapshotId, snapshot);
      latestByRoot.set(snapshot.root, snapshot.snapshotId);
    }
    this.snapshots = snapshots;
    this.diagnostics = diagnostics;
    this.latestByRoot = latestByRoot;
  }

  private makeDiagnostic(input: {
    path: string;
    root: string;
    source: E03AgentDefinition["source"];
    severity: DefinitionDiagnosticSeverity;
    code: string;
    message: string;
    generation: string;
    field?: string | null;
    line?: number | null;
    column?: number | null;
    contentDigest?: string | null;
  }): DefinitionDiagnostic {
    const payload = {
      diagnosticId: `definition-diagnostic-${digest(input).slice(0, 24)}`,
      path: resolve(input.path),
      relativePath: relative(resolve(input.root), resolve(input.path)),
      source: input.source,
      severity: input.severity,
      code: input.code.trim(),
      message: input.message.trim(),
      field: input.field ?? null,
      line: input.line ?? null,
      column: input.column ?? null,
      contentDigest: input.contentDigest ?? null,
      generation: input.generation,
    };
    const diagnostic = { ...payload, digest: digest(payload) };
    assertDefinitionDiagnostic(diagnostic);
    return diagnostic;
  }

  private requireSnapshot(snapshotId: string): DefinitionInventorySnapshot {
    const snapshot = this.snapshots.get(snapshotId);
    if (!snapshot)
      throw new E03RuntimeError(
        "definition_inventory_snapshot_missing",
        `definition inventory snapshot ${snapshotId} does not exist`,
      );
    assertDefinitionInventorySnapshot(snapshot);
    return snapshot;
  }
}

export type DefinitionReloadState =
  | "planned"
  | "validated"
  | "applied"
  | "rolled-back"
  | "rejected";

export interface DefinitionReloadPlan {
  planId: string;
  root: string;
  fromGeneration: string | null;
  toGeneration: string;
  state: DefinitionReloadState;
  addedAgentIds: string[];
  changedAgentIds: string[];
  removedAgentIds: string[];
  blockingDiagnosticIds: string[];
  inventorySnapshotId: string;
  createdAt: string;
  validatedAt: string | null;
  appliedAt: string | null;
  digest: string;
}

function assertDefinitionReloadPlan(plan: DefinitionReloadPlan): void {
  const { digest: checksum, ...payload } = plan;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "definition_reload_plan_digest",
      `definition reload plan ${plan.planId} digest is invalid`,
    );
  if (
    !plan.planId ||
    !plan.root ||
    !plan.toGeneration ||
    !plan.inventorySnapshotId
  )
    throw new E03RuntimeError(
      "definition_reload_plan_identity",
      "definition reload plan identity is incomplete",
    );
  if (plan.state === "applied" && plan.appliedAt === null)
    throw new E03RuntimeError(
      "definition_reload_plan_state",
      "applied definition reload plan requires appliedAt",
    );
}

export class DefinitionReloadRuntime {
  private plans = new Map<string, DefinitionReloadPlan>();
  private reports = new Map<string, DefinitionLoadReport>();

  constructor(
    private readonly inventory: DefinitionInventoryRuntime,
    private readonly registry: AgentDefinitionRegistry,
  ) {}

  stage(input: {
    root: string;
    report: DefinitionLoadReport;
    loadedAt: string;
  }): DefinitionReloadPlan {
    const previous = this.inventory.latest(input.root);
    const { snapshot, diff } = this.inventory.capture(input);
    const diagnostics = this.inventory.diagnosticsFor(snapshot.snapshotId);
    const blockingDiagnosticIds = diagnostics
      .filter((diagnostic) => diagnostic.severity === "error")
      .map((diagnostic) => diagnostic.diagnosticId);
    const previousByPath = new Map(
      (previous?.entries ?? []).map((entry) => [entry.path, entry]),
    );
    const nextByPath = new Map(
      snapshot.entries.map((entry) => [entry.path, entry]),
    );
    const payload = {
      planId: `definition-reload-${digest({ root: snapshot.root, generation: snapshot.generation }).slice(0, 24)}`,
      root: snapshot.root,
      fromGeneration: previous?.generation ?? null,
      toGeneration: snapshot.generation,
      state: (blockingDiagnosticIds.length
        ? "rejected"
        : "planned") as DefinitionReloadState,
      addedAgentIds: diff.addedPaths.map(
        (path) => nextByPath.get(path)!.agentId,
      ),
      changedAgentIds: diff.changedPaths.map(
        (path) => nextByPath.get(path)!.agentId,
      ),
      removedAgentIds: diff.removedPaths.map(
        (path) => previousByPath.get(path)!.agentId,
      ),
      blockingDiagnosticIds,
      inventorySnapshotId: snapshot.snapshotId,
      createdAt: input.loadedAt,
      validatedAt: null,
      appliedAt: null,
    };
    const plan = { ...payload, digest: digest(payload) };
    assertDefinitionReloadPlan(plan);
    this.plans.set(plan.planId, plan);
    this.reports.set(plan.planId, structuredClone(input.report));
    return structuredClone(plan);
  }

  validate(planId: string, validatedAt: string): DefinitionReloadPlan {
    const plan = this.requirePlan(planId);
    if (plan.state !== "planned")
      throw new E03RuntimeError(
        "definition_reload_validate_state",
        `definition reload plan ${planId} is not planned`,
      );
    if (plan.blockingDiagnosticIds.length)
      throw new E03RuntimeError(
        "definition_reload_blocked",
        `definition reload plan ${planId} has blocking diagnostics`,
      );
    const next = this.reseal(plan, {
      state: "validated",
      validatedAt,
    });
    this.plans.set(planId, next);
    return structuredClone(next);
  }

  apply(planId: string, appliedAt: string): DefinitionReloadPlan {
    const plan = this.requirePlan(planId);
    if (plan.state !== "validated")
      throw new E03RuntimeError(
        "definition_reload_apply_state",
        `definition reload plan ${planId} is not validated`,
      );
    const report = this.reports.get(planId);
    if (!report)
      throw new E03RuntimeError(
        "definition_reload_report_missing",
        `definition reload plan ${planId} has no load report`,
      );
    const ids = new Set<string>();
    for (const loaded of report.loaded) {
      if (ids.has(loaded.definition.name))
        throw new E03RuntimeError(
          "definition_reload_duplicate_agent",
          `definition reload plan ${planId} contains duplicate agent ${loaded.definition.name}`,
        );
      ids.add(loaded.definition.name);
      this.registry.register(loaded.definition);
    }
    const next = this.reseal(plan, { state: "applied", appliedAt });
    this.plans.set(planId, next);
    return structuredClone(next);
  }

  rollback(planId: string, rolledBackAt: string): DefinitionReloadPlan {
    const plan = this.requirePlan(planId);
    if (plan.state !== "applied")
      throw new E03RuntimeError(
        "definition_reload_rollback_state",
        `definition reload plan ${planId} is not applied`,
      );
    const next = this.reseal(plan, {
      state: "rolled-back",
      appliedAt: rolledBackAt,
    });
    this.plans.set(planId, next);
    return structuredClone(next);
  }

  get(planId: string): DefinitionReloadPlan {
    return structuredClone(this.requirePlan(planId));
  }

  snapshot(): {
    plans: DefinitionReloadPlan[];
    reports: Array<[string, DefinitionLoadReport]>;
  } {
    return {
      plans: [...this.plans.values()].map((plan) => structuredClone(plan)),
      reports: [...this.reports.entries()].map(([key, report]) => [
        key,
        structuredClone(report),
      ]),
    };
  }

  restore(input: {
    plans: readonly DefinitionReloadPlan[];
    reports: readonly (readonly [string, DefinitionLoadReport])[];
  }): void {
    const plans = new Map<string, DefinitionReloadPlan>();
    const reports = new Map<string, DefinitionLoadReport>();
    for (const plan of input.plans) {
      assertDefinitionReloadPlan(plan);
      if (plans.has(plan.planId))
        throw new E03RuntimeError(
          "definition_reload_restore_duplicate",
          `duplicate definition reload plan ${plan.planId}`,
        );
      plans.set(plan.planId, structuredClone(plan));
    }
    for (const [planId, report] of input.reports) {
      if (!plans.has(planId))
        throw new E03RuntimeError(
          "definition_reload_restore_orphan",
          `definition reload report ${planId} has no plan`,
        );
      if (reports.has(planId))
        throw new E03RuntimeError(
          "definition_reload_restore_report_duplicate",
          `duplicate definition reload report ${planId}`,
        );
      reports.set(planId, structuredClone(report));
    }
    this.plans = plans;
    this.reports = reports;
  }

  private requirePlan(planId: string): DefinitionReloadPlan {
    const plan = this.plans.get(planId);
    if (!plan)
      throw new E03RuntimeError(
        "definition_reload_plan_missing",
        `definition reload plan ${planId} does not exist`,
      );
    assertDefinitionReloadPlan(plan);
    return plan;
  }

  private reseal(
    plan: DefinitionReloadPlan,
    patch: Partial<Omit<DefinitionReloadPlan, "planId" | "digest">>,
  ): DefinitionReloadPlan {
    const { digest: _, ...prior } = plan;
    const payload = { ...prior, ...patch, planId: plan.planId };
    const next = { ...payload, digest: digest(payload) };
    assertDefinitionReloadPlan(next);
    return next;
  }
}

export type DefinitionSourceState =
  | "discovered"
  | "stable"
  | "changed"
  | "missing"
  | "quarantined";
export interface DefinitionSourceObservation {
  observationId: string;
  root: string;
  path: string;
  relativePath: string;
  source: E03AgentDefinition["source"];
  state: DefinitionSourceState;
  size: number;
  modifiedAt: string;
  contentDigest: string | null;
  previousDigest: string | null;
  symlink: boolean;
  quarantineReason: string | null;
  observedAt: string;
  revision: number;
  digest: string;
}
export interface DefinitionLoadTransaction {
  transactionId: string;
  generation: string;
  roots: string[];
  observationIds: string[];
  state: "open" | "validated" | "committed" | "rolled_back" | "failed";
  addedNames: string[];
  changedNames: string[];
  removedNames: string[];
  quarantinedPaths: string[];
  expectedRegistryRevision: number;
  committedRegistryRevision: number | null;
  error: string | null;
  openedAt: string;
  validatedAt: string | null;
  completedAt: string | null;
  revision: number;
  digest: string;
}
function assertSourceObservation(value: DefinitionSourceObservation): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "definition_source_observation_digest",
      `definition source observation ${value.observationId} is corrupt`,
    );
  if (!value.observationId || !value.root || !value.path || !value.relativePath)
    throw new E03RuntimeError(
      "definition_source_observation_identity",
      "definition source observation identity is required",
    );
  if (
    !Number.isSafeInteger(value.size) ||
    value.size < 0 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "definition_source_observation_revision",
      `definition source observation ${value.observationId} is invalid`,
    );
  if (value.state === "quarantined" && !value.quarantineReason)
    throw new E03RuntimeError(
      "definition_source_observation_quarantine",
      `quarantined source ${value.path} requires a reason`,
    );
}
function assertLoadTransaction(value: DefinitionLoadTransaction): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "definition_load_transaction_digest",
      `definition load transaction ${value.transactionId} is corrupt`,
    );
  if (!value.transactionId || !value.generation || !value.roots.length)
    throw new E03RuntimeError(
      "definition_load_transaction_identity",
      "definition load transaction identity is required",
    );
  if (
    !Number.isSafeInteger(value.expectedRegistryRevision) ||
    value.expectedRegistryRevision < 0 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "definition_load_transaction_revision",
      `definition load transaction ${value.transactionId} is invalid`,
    );
  if (
    (value.state === "committed" ||
      value.state === "rolled_back" ||
      value.state === "failed") &&
    value.completedAt === null
  )
    throw new E03RuntimeError(
      "definition_load_transaction_completion",
      `definition load transaction ${value.transactionId} lacks completion time`,
    );
}
export class AgentDefinitionSourceCustody {
  private observations = new Map<string, DefinitionSourceObservation>();
  private latestByPath = new Map<string, string>();
  private transactions = new Map<string, DefinitionLoadTransaction>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  observe(input: {
    root: string;
    path: string;
    relativePath: string;
    source: E03AgentDefinition["source"];
    size: number;
    modifiedAt: string;
    contentDigest?: string | null;
    symlink?: boolean;
    exists?: boolean;
  }): DefinitionSourceObservation {
    const path = resolve(input.path);
    const root = resolve(input.root);
    if (
      relative(root, path).startsWith(`..${sep}`) ||
      relative(root, path) === ".."
    )
      throw new E03RuntimeError(
        "definition_source_path_escape",
        `definition source ${path} escapes ${root}`,
      );
    if (
      !Number.isSafeInteger(input.size) ||
      input.size < 0 ||
      Number.isNaN(Date.parse(input.modifiedAt))
    )
      throw new E03RuntimeError(
        "definition_source_metadata",
        `definition source ${path} metadata is invalid`,
      );
    const previousId = this.latestByPath.get(path);
    const previous = previousId ? this.requireObservation(previousId) : null;
    const exists = input.exists ?? true;
    const state: DefinitionSourceState = !exists
      ? "missing"
      : !input.contentDigest
        ? "discovered"
        : previous?.contentDigest === input.contentDigest
          ? "stable"
          : previous
            ? "changed"
            : "discovered";
    const payload = {
      observationId: createId("definition-source-observation"),
      root,
      path,
      relativePath: input.relativePath.replaceAll("\\", "/"),
      source: input.source,
      state,
      size: input.size,
      modifiedAt: input.modifiedAt,
      contentDigest: input.contentDigest ?? null,
      previousDigest: previous?.contentDigest ?? null,
      symlink: input.symlink ?? false,
      quarantineReason: null,
      observedAt: this.clock.now(),
      revision: 1,
    };
    const observation = { ...payload, digest: digest(payload) };
    assertSourceObservation(observation);
    this.observations.set(observation.observationId, observation);
    this.latestByPath.set(path, observation.observationId);
    return structuredClone(observation);
  }
  quarantine(
    observationId: string,
    expectedRevision: number,
    reason: string,
  ): DefinitionSourceObservation {
    const observation = this.requireObservation(observationId);
    this.assertObservationRevision(observation, expectedRevision);
    if (observation.state === "missing")
      throw new E03RuntimeError(
        "definition_source_quarantine_state",
        `missing definition source ${observation.path} cannot be quarantined`,
      );
    if (!reason.trim())
      throw new E03RuntimeError(
        "definition_source_quarantine_reason",
        "definition source quarantine reason is required",
      );
    return this.transitionObservation(observation, {
      state: "quarantined",
      quarantineReason: reason.trim(),
    });
  }
  stabilize(
    observationId: string,
    expectedRevision: number,
    contentDigest: string,
  ): DefinitionSourceObservation {
    const observation = this.requireObservation(observationId);
    this.assertObservationRevision(observation, expectedRevision);
    if (observation.state === "missing" || observation.state === "quarantined")
      throw new E03RuntimeError(
        "definition_source_stabilize_state",
        `definition source ${observation.path} is ${observation.state}`,
      );
    if (!/^[a-f0-9]{32,128}$/i.test(contentDigest))
      throw new E03RuntimeError(
        "definition_source_content_digest",
        "definition source digest is invalid",
      );
    return this.transitionObservation(observation, {
      state: "stable",
      contentDigest,
      quarantineReason: null,
    });
  }
  begin(input: {
    roots: readonly string[];
    expectedRegistryRevision: number;
  }): DefinitionLoadTransaction {
    const roots = [
      ...new Set(input.roots.map((value) => resolve(value))),
    ].sort();
    if (
      !roots.length ||
      !Number.isSafeInteger(input.expectedRegistryRevision) ||
      input.expectedRegistryRevision < 0
    )
      throw new E03RuntimeError(
        "definition_load_transaction_input",
        "definition load transaction input is invalid",
      );
    const visible = [...this.latestByPath.values()]
      .map((id) => this.requireObservation(id))
      .filter((value) => roots.includes(value.root));
    const generation = digest(
      visible.map((value) => [value.path, value.contentDigest, value.state]),
    );
    const existing = [...this.transactions.values()].find(
      (value) =>
        value.generation === generation &&
        value.state !== "rolled_back" &&
        value.state !== "failed",
    );
    if (existing) return structuredClone(existing);
    const payload = {
      transactionId: createId("definition-load-transaction"),
      generation,
      roots,
      observationIds: visible.map((value) => value.observationId).sort(),
      state: "open" as const,
      addedNames: [],
      changedNames: [],
      removedNames: [],
      quarantinedPaths: visible
        .filter((value) => value.state === "quarantined")
        .map((value) => value.path)
        .sort(),
      expectedRegistryRevision: input.expectedRegistryRevision,
      committedRegistryRevision: null,
      error: null,
      openedAt: this.clock.now(),
      validatedAt: null,
      completedAt: null,
      revision: 1,
    };
    const transaction = { ...payload, digest: digest(payload) };
    assertLoadTransaction(transaction);
    this.transactions.set(transaction.transactionId, transaction);
    return structuredClone(transaction);
  }
  validate(input: {
    transactionId: string;
    expectedRevision: number;
    addedNames: readonly string[];
    changedNames: readonly string[];
    removedNames: readonly string[];
    allowQuarantined?: boolean;
  }): DefinitionLoadTransaction {
    const transaction = this.requireTransaction(input.transactionId);
    this.assertTransactionRevision(transaction, input.expectedRevision);
    if (transaction.state !== "open")
      throw new E03RuntimeError(
        "definition_load_transaction_validate_state",
        `definition load transaction ${transaction.transactionId} is ${transaction.state}`,
      );
    const observations = transaction.observationIds.map((id) =>
      this.requireObservation(id),
    );
    if (
      observations.some(
        (value) => value.state === "discovered" || value.state === "changed",
      )
    )
      throw new E03RuntimeError(
        "definition_load_transaction_unstable",
        "definition load transaction includes unstable sources",
      );
    if (
      !input.allowQuarantined &&
      observations.some((value) => value.state === "quarantined")
    )
      throw new E03RuntimeError(
        "definition_load_transaction_quarantined",
        "definition load transaction includes quarantined sources",
      );
    const added = new Set(input.addedNames);
    const changed = new Set(input.changedNames);
    const removed = new Set(input.removedNames);
    if ([added, changed, removed].some((set) => set.has("")))
      throw new E03RuntimeError(
        "definition_load_transaction_names",
        "definition load transaction names are invalid",
      );
    for (const name of added)
      if (changed.has(name) || removed.has(name))
        throw new E03RuntimeError(
          "definition_load_transaction_overlap",
          `definition ${name} has conflicting load actions`,
        );
    for (const name of changed)
      if (removed.has(name))
        throw new E03RuntimeError(
          "definition_load_transaction_overlap",
          `definition ${name} has conflicting load actions`,
        );
    return this.transitionTransaction(transaction, {
      state: "validated",
      addedNames: [...added].sort(),
      changedNames: [...changed].sort(),
      removedNames: [...removed].sort(),
      validatedAt: this.clock.now(),
    });
  }
  commit(
    transactionId: string,
    expectedRevision: number,
    committedRegistryRevision: number,
  ): DefinitionLoadTransaction {
    const transaction = this.requireTransaction(transactionId);
    this.assertTransactionRevision(transaction, expectedRevision);
    if (transaction.state !== "validated")
      throw new E03RuntimeError(
        "definition_load_transaction_commit_state",
        `definition load transaction ${transactionId} is ${transaction.state}`,
      );
    if (
      !Number.isSafeInteger(committedRegistryRevision) ||
      committedRegistryRevision <= transaction.expectedRegistryRevision
    )
      throw new E03RuntimeError(
        "definition_load_transaction_registry_revision",
        "definition registry revision did not advance",
      );
    return this.transitionTransaction(transaction, {
      state: "committed",
      committedRegistryRevision,
      completedAt: this.clock.now(),
    });
  }
  rollback(
    transactionId: string,
    expectedRevision: number,
    error?: string,
  ): DefinitionLoadTransaction {
    const transaction = this.requireTransaction(transactionId);
    this.assertTransactionRevision(transaction, expectedRevision);
    if (transaction.state === "committed")
      throw new E03RuntimeError(
        "definition_load_transaction_rollback_state",
        `committed definition load transaction ${transactionId} cannot roll back`,
      );
    if (transaction.state === "rolled_back" || transaction.state === "failed")
      return structuredClone(transaction);
    return this.transitionTransaction(transaction, {
      state: error ? "failed" : "rolled_back",
      error: error?.trim() || null,
      completedAt: this.clock.now(),
    });
  }
  current(path: string): DefinitionSourceObservation | null {
    const id = this.latestByPath.get(resolve(path));
    return id ? structuredClone(this.requireObservation(id)) : null;
  }
  snapshot(): {
    observations: DefinitionSourceObservation[];
    latestByPath: Array<[string, string]>;
    transactions: DefinitionLoadTransaction[];
  } {
    return {
      observations: [...this.observations.values()].map((value) =>
        structuredClone(value),
      ),
      latestByPath: [...this.latestByPath.entries()].map(([path, id]) => [
        path,
        id,
      ]),
      transactions: [...this.transactions.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }
  restore(snapshot: {
    observations: readonly DefinitionSourceObservation[];
    latestByPath: ReadonlyArray<readonly [string, string]>;
    transactions: readonly DefinitionLoadTransaction[];
  }): void {
    const observations = new Map<string, DefinitionSourceObservation>();
    const latestByPath = new Map<string, string>();
    const transactions = new Map<string, DefinitionLoadTransaction>();
    for (const value of snapshot.observations) {
      assertSourceObservation(value);
      if (observations.has(value.observationId))
        throw new E03RuntimeError(
          "definition_source_restore_duplicate",
          `duplicate source observation ${value.observationId}`,
        );
      observations.set(value.observationId, structuredClone(value));
    }
    for (const [path, id] of snapshot.latestByPath) {
      const observation = observations.get(id);
      if (!observation || observation.path !== path || latestByPath.has(path))
        throw new E03RuntimeError(
          "definition_source_restore_latest",
          `definition source latest index ${path} is invalid`,
        );
      latestByPath.set(path, id);
    }
    for (const value of snapshot.transactions) {
      assertLoadTransaction(value);
      if (
        transactions.has(value.transactionId) ||
        value.observationIds.some((id) => !observations.has(id))
      )
        throw new E03RuntimeError(
          "definition_load_transaction_restore",
          `definition load transaction ${value.transactionId} is invalid`,
        );
      transactions.set(value.transactionId, structuredClone(value));
    }
    this.observations = observations;
    this.latestByPath = latestByPath;
    this.transactions = transactions;
  }
  private requireObservation(id: string): DefinitionSourceObservation {
    const value = this.observations.get(id);
    if (!value)
      throw new E03RuntimeError(
        "definition_source_observation_missing",
        `definition source observation ${id} does not exist`,
      );
    assertSourceObservation(value);
    return value;
  }
  private requireTransaction(id: string): DefinitionLoadTransaction {
    const value = this.transactions.get(id);
    if (!value)
      throw new E03RuntimeError(
        "definition_load_transaction_missing",
        `definition load transaction ${id} does not exist`,
      );
    assertLoadTransaction(value);
    return value;
  }
  private assertObservationRevision(
    value: DefinitionSourceObservation,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "definition_source_observation_stale_revision",
        `definition source observation ${value.observationId} revision is stale`,
      );
  }
  private assertTransactionRevision(
    value: DefinitionLoadTransaction,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "definition_load_transaction_stale_revision",
        `definition load transaction ${value.transactionId} revision is stale`,
      );
  }
  private transitionObservation(
    value: DefinitionSourceObservation,
    patch: Partial<
      Omit<DefinitionSourceObservation, "observationId" | "revision" | "digest">
    >,
  ): DefinitionSourceObservation {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      observationId: value.observationId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertSourceObservation(next);
    this.observations.set(next.observationId, next);
    this.latestByPath.set(next.path, next.observationId);
    return structuredClone(next);
  }
  private transitionTransaction(
    value: DefinitionLoadTransaction,
    patch: Partial<
      Omit<DefinitionLoadTransaction, "transactionId" | "revision" | "digest">
    >,
  ): DefinitionLoadTransaction {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      transactionId: value.transactionId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertLoadTransaction(next);
    this.transactions.set(next.transactionId, next);
    return structuredClone(next);
  }
}
