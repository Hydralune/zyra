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

export interface DefinitionWatchTarget {
  watchId: string;
  root: string;
  source: E03AgentDefinition["source"];
  state: "active" | "paused" | "backoff" | "closed";
  debounceMs: number;
  maximumBatchSize: number;
  failureCount: number;
  nextScanAt: string;
  lastScanAt: string | null;
  lastGeneration: string | null;
  createdAt: string;
  closedAt: string | null;
  revision: number;
  digest: string;
}
export interface DefinitionWatchChange {
  changeId: string;
  watchId: string;
  path: string;
  kind: "created" | "modified" | "removed" | "renamed";
  priorPath: string | null;
  contentDigest: string | null;
  observedAt: string;
  sequence: number;
  previousDigest: string;
  digest: string;
}
export interface DefinitionWatchBatch {
  batchId: string;
  watchId: string;
  state: "pending" | "claimed" | "applied" | "failed" | "expired";
  changeIds: string[];
  generation: string;
  claimantId: string | null;
  attempt: number;
  claimedAt: string | null;
  completedAt: string | null;
  expiresAt: string;
  failure: string | null;
  revision: number;
  digest: string;
}
function assertWatchTarget(value: DefinitionWatchTarget): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "definition_watch_digest",
      `definition watch ${value.watchId} is corrupt`,
    );
  if (
    !value.watchId ||
    !value.root ||
    !Number.isSafeInteger(value.debounceMs) ||
    value.debounceMs < 0 ||
    !Number.isSafeInteger(value.maximumBatchSize) ||
    value.maximumBatchSize < 1 ||
    !Number.isSafeInteger(value.failureCount) ||
    value.failureCount < 0 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.nextScanAt))
  )
    throw new E03RuntimeError(
      "definition_watch",
      `definition watch ${value.watchId} is invalid`,
    );
}
function assertWatchChange(value: DefinitionWatchChange): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "definition_watch_change_digest",
      `definition watch change ${value.changeId} is corrupt`,
    );
  if (
    !value.changeId ||
    !value.watchId ||
    !value.path ||
    !Number.isSafeInteger(value.sequence) ||
    value.sequence < 1
  )
    throw new E03RuntimeError(
      "definition_watch_change",
      `definition watch change ${value.changeId} is invalid`,
    );
}
function assertWatchBatch(value: DefinitionWatchBatch): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "definition_watch_batch_digest",
      `definition watch batch ${value.batchId} is corrupt`,
    );
  if (
    !value.batchId ||
    !value.watchId ||
    !value.changeIds.length ||
    !value.generation ||
    !Number.isSafeInteger(value.attempt) ||
    value.attempt < 0 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.expiresAt))
  )
    throw new E03RuntimeError(
      "definition_watch_batch",
      `definition watch batch ${value.batchId} is invalid`,
    );
  if (value.state === "claimed" && (!value.claimantId || !value.claimedAt))
    throw new E03RuntimeError(
      "definition_watch_batch_claim",
      `claimed definition watch batch ${value.batchId} lacks claimant`,
    );
}
export type TrustedDefinitionSourceKind =
  | "builtin"
  | "user"
  | "project"
  | "plugin"
  | "remote";

export interface DefinitionTrustAnchor {
  anchorId: string;
  issuer: string;
  publicKeyDigest: string;
  allowedSourceKinds: TrustedDefinitionSourceKind[];
  allowedNamespaces: string[];
  state: "pending" | "active" | "suspended" | "revoked";
  notBefore: string;
  notAfter: string;
  createdAt: string;
  revision: number;
  digest: string;
}

export interface DefinitionSignatureEnvelope {
  envelopeId: string;
  definitionName: string;
  sourceId: string;
  sourceKind: TrustedDefinitionSourceKind;
  namespace: string;
  contentDigest: string;
  anchorId: string;
  signatureDigest: string;
  state: "submitted" | "verified" | "rejected" | "expired" | "revoked";
  submittedAt: string;
  verifiedAt: string;
  errorCode: string;
  revision: number;
  digest: string;
}

export interface DefinitionTrustDecision {
  decisionId: string;
  envelopeId: string;
  anchorId: string;
  verifierId: string;
  accepted: boolean;
  observedContentDigest: string;
  observedSignatureDigest: string;
  reasonCode: string;
  decidedAt: string;
  previousDigest: string;
  digest: string;
}

export interface DefinitionTrustSnapshot {
  anchors: DefinitionTrustAnchor[];
  envelopes: DefinitionSignatureEnvelope[];
  decisions: DefinitionTrustDecision[];
  activeAnchorByIssuer: [string, string][];
  envelopeBySourceDigest: [string, string][];
}

function assertDefinitionTrustAnchor(value: DefinitionTrustAnchor): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.anchorId ||
    !value.issuer ||
    !value.publicKeyDigest ||
    !value.allowedSourceKinds.length ||
    !value.allowedNamespaces.length ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "definition_trust_anchor_corrupt",
      `definition trust anchor ${value.anchorId || "<empty>"} is corrupt`,
    );
}

function assertDefinitionSignatureEnvelope(
  value: DefinitionSignatureEnvelope,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.envelopeId ||
    !value.definitionName ||
    !value.sourceId ||
    !value.namespace ||
    !value.contentDigest ||
    !value.anchorId ||
    !value.signatureDigest ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "definition_signature_envelope_corrupt",
      `definition signature envelope ${value.envelopeId || "<empty>"} is corrupt`,
    );
}

function assertDefinitionTrustDecision(value: DefinitionTrustDecision): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.decisionId ||
    !value.envelopeId ||
    !value.anchorId ||
    !value.verifierId ||
    !value.observedContentDigest ||
    !value.observedSignatureDigest ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "definition_trust_decision_corrupt",
      `definition trust decision ${value.decisionId || "<empty>"} is corrupt`,
    );
}

export class AgentDefinitionTrustRuntime {
  private anchors = new Map<string, DefinitionTrustAnchor>();
  private envelopes = new Map<string, DefinitionSignatureEnvelope>();
  private decisions = new Map<string, DefinitionTrustDecision[]>();
  private activeAnchorByIssuer = new Map<string, string>();
  private envelopeBySourceDigest = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  registerAnchor(input: {
    anchorId?: string;
    issuer: string;
    publicKeyDigest: string;
    allowedSourceKinds: readonly TrustedDefinitionSourceKind[];
    allowedNamespaces: readonly string[];
    notBefore: string;
    notAfter: string;
  }): DefinitionTrustAnchor {
    if (this.activeAnchorByIssuer.has(input.issuer))
      throw new E03RuntimeError(
        "definition_trust_anchor_issuer_active",
        `definition trust issuer ${input.issuer} already has an anchor`,
      );
    if (Date.parse(input.notAfter) <= Date.parse(input.notBefore))
      throw new E03RuntimeError(
        "definition_trust_anchor_validity",
        "definition trust anchor validity window is invalid",
      );
    const anchorId = input.anchorId ?? createId("definition-trust-anchor");
    const payload = {
      anchorId,
      issuer: input.issuer,
      publicKeyDigest: input.publicKeyDigest,
      allowedSourceKinds: [...new Set(input.allowedSourceKinds)].sort(),
      allowedNamespaces: [...new Set(input.allowedNamespaces)].sort(),
      state: "pending" as const,
      notBefore: input.notBefore,
      notAfter: input.notAfter,
      createdAt: this.clock.now(),
      revision: 1,
    };
    const anchor = { ...payload, digest: digest(payload) };
    assertDefinitionTrustAnchor(anchor);
    this.anchors.set(anchorId, anchor);
    return structuredClone(anchor);
  }

  activateAnchor(
    anchorId: string,
    expectedRevision: number,
  ): DefinitionTrustAnchor {
    const anchor = this.requireAnchor(anchorId);
    this.assertAnchorRevision(anchor, expectedRevision);
    if (anchor.state !== "pending")
      throw new E03RuntimeError(
        "definition_trust_anchor_activate_state",
        `definition trust anchor ${anchorId} is ${anchor.state}`,
      );
    const now = this.clock.now();
    if (
      Date.parse(anchor.notBefore) > Date.parse(now) ||
      Date.parse(anchor.notAfter) <= Date.parse(now)
    )
      throw new E03RuntimeError(
        "definition_trust_anchor_outside_validity",
        `definition trust anchor ${anchorId} is outside validity`,
      );
    const next = this.transitionAnchor(anchor, { state: "active" });
    this.activeAnchorByIssuer.set(anchor.issuer, anchorId);
    return next;
  }

  submit(input: {
    envelopeId?: string;
    definitionName: string;
    sourceId: string;
    sourceKind: TrustedDefinitionSourceKind;
    namespace: string;
    contentDigest: string;
    anchorId: string;
    signatureDigest: string;
  }): DefinitionSignatureEnvelope {
    const anchor = this.requireAnchor(input.anchorId);
    if (
      anchor.state !== "active" ||
      !anchor.allowedSourceKinds.includes(input.sourceKind) ||
      !anchor.allowedNamespaces.some(
        (value) => value === "*" || value === input.namespace,
      )
    )
      throw new E03RuntimeError(
        "definition_signature_source_denied",
        `definition source ${input.sourceId} is not trusted`,
      );
    const index = this.sourceDigestKey(input.sourceId, input.contentDigest);
    const duplicateId = this.envelopeBySourceDigest.get(index);
    if (duplicateId) return structuredClone(this.requireEnvelope(duplicateId));
    const envelopeId =
      input.envelopeId ?? createId("definition-signature-envelope");
    const payload = {
      envelopeId,
      definitionName: input.definitionName,
      sourceId: input.sourceId,
      sourceKind: input.sourceKind,
      namespace: input.namespace,
      contentDigest: input.contentDigest,
      anchorId: input.anchorId,
      signatureDigest: input.signatureDigest,
      state: "submitted" as const,
      submittedAt: this.clock.now(),
      verifiedAt: "",
      errorCode: "",
      revision: 1,
    };
    const envelope = { ...payload, digest: digest(payload) };
    assertDefinitionSignatureEnvelope(envelope);
    this.envelopes.set(envelopeId, envelope);
    this.decisions.set(envelopeId, []);
    this.envelopeBySourceDigest.set(index, envelopeId);
    return structuredClone(envelope);
  }

  verify(input: {
    envelopeId: string;
    expectedRevision: number;
    verifierId: string;
    observedContentDigest: string;
    observedSignatureDigest: string;
  }): DefinitionTrustDecision {
    const envelope = this.requireEnvelope(input.envelopeId);
    this.assertEnvelopeRevision(envelope, input.expectedRevision);
    if (envelope.state !== "submitted")
      throw new E03RuntimeError(
        "definition_signature_verify_state",
        `definition signature envelope ${envelope.envelopeId} is ${envelope.state}`,
      );
    const anchor = this.requireAnchor(envelope.anchorId);
    const accepted =
      anchor.state === "active" &&
      Date.parse(anchor.notAfter) > Date.parse(this.clock.now()) &&
      input.observedContentDigest === envelope.contentDigest &&
      input.observedSignatureDigest === envelope.signatureDigest;
    const entries = this.decisionEntries(envelope.envelopeId);
    const payload = {
      decisionId: createId("definition-trust-decision"),
      envelopeId: envelope.envelopeId,
      anchorId: anchor.anchorId,
      verifierId: input.verifierId,
      accepted,
      observedContentDigest: input.observedContentDigest,
      observedSignatureDigest: input.observedSignatureDigest,
      reasonCode: accepted ? "signature_verified" : "signature_mismatch",
      decidedAt: this.clock.now(),
      previousDigest: entries.at(-1)?.digest ?? "",
    };
    const decision = { ...payload, digest: digest(payload) };
    assertDefinitionTrustDecision(decision);
    entries.push(decision);
    this.decisions.set(envelope.envelopeId, entries);
    this.transitionEnvelope(envelope, {
      state: accepted ? "verified" : "rejected",
      verifiedAt: decision.decidedAt,
      errorCode: accepted ? "" : decision.reasonCode,
    });
    return structuredClone(decision);
  }

  revokeAnchor(
    anchorId: string,
    expectedRevision: number,
  ): {
    anchor: DefinitionTrustAnchor;
    envelopes: DefinitionSignatureEnvelope[];
  } {
    const anchor = this.requireAnchor(anchorId);
    this.assertAnchorRevision(anchor, expectedRevision);
    if (!["active", "suspended"].includes(anchor.state))
      throw new E03RuntimeError(
        "definition_trust_anchor_revoke_state",
        `definition trust anchor ${anchorId} is ${anchor.state}`,
      );
    const next = this.transitionAnchor(anchor, { state: "revoked" });
    this.activeAnchorByIssuer.delete(anchor.issuer);
    const envelopes: DefinitionSignatureEnvelope[] = [];
    for (const envelope of [...this.envelopes.values()])
      if (envelope.anchorId === anchorId && envelope.state === "verified")
        envelopes.push(
          this.transitionEnvelope(envelope, {
            state: "revoked",
            errorCode: "trust_anchor_revoked",
          }),
        );
    return { anchor: next, envelopes };
  }

  snapshot(): DefinitionTrustSnapshot {
    return {
      anchors: [...this.anchors.values()].map((value) =>
        structuredClone(value),
      ),
      envelopes: [...this.envelopes.values()].map((value) =>
        structuredClone(value),
      ),
      decisions: [...this.decisions.values()]
        .flat()
        .map((value) => structuredClone(value)),
      activeAnchorByIssuer: [...this.activeAnchorByIssuer.entries()],
      envelopeBySourceDigest: [...this.envelopeBySourceDigest.entries()],
    };
  }

  restore(snapshot: DefinitionTrustSnapshot): void {
    const anchors = new Map<string, DefinitionTrustAnchor>();
    const envelopes = new Map<string, DefinitionSignatureEnvelope>();
    const decisions = new Map<string, DefinitionTrustDecision[]>();
    for (const value of snapshot.anchors) {
      assertDefinitionTrustAnchor(value);
      anchors.set(value.anchorId, structuredClone(value));
    }
    for (const value of snapshot.envelopes) {
      assertDefinitionSignatureEnvelope(value);
      if (!anchors.has(value.anchorId) || envelopes.has(value.envelopeId))
        throw new E03RuntimeError(
          "definition_trust_restore_envelope",
          `envelope ${value.envelopeId} invalid`,
        );
      envelopes.set(value.envelopeId, structuredClone(value));
      decisions.set(value.envelopeId, []);
    }
    for (const value of snapshot.decisions) {
      assertDefinitionTrustDecision(value);
      const entries = decisions.get(value.envelopeId);
      if (!entries || value.previousDigest !== (entries.at(-1)?.digest ?? ""))
        throw new E03RuntimeError(
          "definition_trust_restore_decision_chain",
          `decision ${value.decisionId} breaks chain`,
        );
      entries.push(structuredClone(value));
    }
    const activeAnchorByIssuer = new Map(snapshot.activeAnchorByIssuer);
    const envelopeBySourceDigest = new Map(snapshot.envelopeBySourceDigest);
    if (
      activeAnchorByIssuer.size !== snapshot.activeAnchorByIssuer.length ||
      envelopeBySourceDigest.size !== snapshot.envelopeBySourceDigest.length
    )
      throw new E03RuntimeError(
        "definition_trust_restore_index_duplicate",
        "definition trust indexes duplicate",
      );
    for (const [issuer, anchorId] of activeAnchorByIssuer) {
      const value = anchors.get(anchorId);
      if (!value || value.issuer !== issuer || value.state !== "active")
        throw new E03RuntimeError(
          "definition_trust_restore_anchor_index",
          `anchor index ${issuer} invalid`,
        );
    }
    for (const [index, envelopeId] of envelopeBySourceDigest) {
      const value = envelopes.get(envelopeId);
      if (
        !value ||
        index !== this.sourceDigestKey(value.sourceId, value.contentDigest)
      )
        throw new E03RuntimeError(
          "definition_trust_restore_envelope_index",
          `envelope index ${index} invalid`,
        );
    }
    this.anchors = anchors;
    this.envelopes = envelopes;
    this.decisions = decisions;
    this.activeAnchorByIssuer = activeAnchorByIssuer;
    this.envelopeBySourceDigest = envelopeBySourceDigest;
  }

  private sourceDigestKey(sourceId: string, contentDigest: string): string {
    return `${sourceId}\u0000${contentDigest}`;
  }

  private decisionEntries(envelopeId: string): DefinitionTrustDecision[] {
    return this.decisions.get(envelopeId) ?? [];
  }

  private requireAnchor(id: string): DefinitionTrustAnchor {
    const value = this.anchors.get(id);
    if (!value)
      throw new E03RuntimeError(
        "definition_trust_anchor_missing",
        `anchor ${id} missing`,
      );
    assertDefinitionTrustAnchor(value);
    return value;
  }

  private requireEnvelope(id: string): DefinitionSignatureEnvelope {
    const value = this.envelopes.get(id);
    if (!value)
      throw new E03RuntimeError(
        "definition_signature_envelope_missing",
        `envelope ${id} missing`,
      );
    assertDefinitionSignatureEnvelope(value);
    return value;
  }

  private assertAnchorRevision(
    value: DefinitionTrustAnchor,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "definition_trust_anchor_stale_revision",
        `anchor ${value.anchorId} stale`,
      );
  }

  private assertEnvelopeRevision(
    value: DefinitionSignatureEnvelope,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "definition_signature_envelope_stale_revision",
        `envelope ${value.envelopeId} stale`,
      );
  }

  private transitionAnchor(
    value: DefinitionTrustAnchor,
    patch: Partial<
      Omit<DefinitionTrustAnchor, "anchorId" | "revision" | "digest">
    >,
  ): DefinitionTrustAnchor {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      anchorId: value.anchorId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertDefinitionTrustAnchor(next);
    this.anchors.set(next.anchorId, next);
    return structuredClone(next);
  }

  private transitionEnvelope(
    value: DefinitionSignatureEnvelope,
    patch: Partial<
      Omit<DefinitionSignatureEnvelope, "envelopeId" | "revision" | "digest">
    >,
  ): DefinitionSignatureEnvelope {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      envelopeId: value.envelopeId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertDefinitionSignatureEnvelope(next);
    this.envelopes.set(next.envelopeId, next);
    return structuredClone(next);
  }
}

export class DefinitionWatchRuntime {
  private watches = new Map<string, DefinitionWatchTarget>();
  private changes = new Map<string, DefinitionWatchChange[]>();
  private batches = new Map<string, DefinitionWatchBatch>();
  private pendingByWatch = new Map<string, string>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  register(input: {
    root: string;
    source: E03AgentDefinition["source"];
    debounceMs?: number;
    maximumBatchSize?: number;
  }): DefinitionWatchTarget {
    const root = resolve(input.root);
    const existing = [...this.watches.values()].find(
      (value) =>
        value.root === root &&
        value.source === input.source &&
        value.state !== "closed",
    );
    if (existing) return structuredClone(existing);
    const payload = {
      watchId: createId("definition-watch"),
      root,
      source: input.source,
      state: "active" as const,
      debounceMs: input.debounceMs ?? 250,
      maximumBatchSize: input.maximumBatchSize ?? 256,
      failureCount: 0,
      nextScanAt: this.clock.now(),
      lastScanAt: null,
      lastGeneration: null,
      createdAt: this.clock.now(),
      closedAt: null,
      revision: 1,
    };
    const watch = { ...payload, digest: digest(payload) };
    assertWatchTarget(watch);
    this.watches.set(watch.watchId, watch);
    return structuredClone(watch);
  }
  observe(input: {
    watchId: string;
    path: string;
    kind: DefinitionWatchChange["kind"];
    priorPath?: string | null;
    contentDigest?: string | null;
  }): DefinitionWatchChange {
    const watch = this.requireWatch(input.watchId);
    if (watch.state !== "active")
      throw new E03RuntimeError(
        "definition_watch_observe_state",
        `definition watch ${watch.watchId} is ${watch.state}`,
      );
    const path = resolve(input.path);
    if (
      relative(watch.root, path).startsWith(`..${sep}`) ||
      relative(watch.root, path) === ".."
    )
      throw new E03RuntimeError(
        "definition_watch_path_escape",
        `definition watch change ${path} escapes ${watch.root}`,
      );
    const entries = this.changes.get(watch.watchId) ?? [];
    const last = entries[entries.length - 1];
    if (
      last &&
      last.path === path &&
      last.kind === input.kind &&
      last.contentDigest === (input.contentDigest ?? null)
    )
      return structuredClone(last);
    const payload = {
      changeId: createId("definition-watch-change"),
      watchId: watch.watchId,
      path,
      kind: input.kind,
      priorPath: input.priorPath ? resolve(input.priorPath) : null,
      contentDigest: input.contentDigest ?? null,
      observedAt: this.clock.now(),
      sequence: entries.length + 1,
      previousDigest: last?.digest ?? "root",
    };
    const change = { ...payload, digest: digest(payload) };
    assertWatchChange(change);
    entries.push(change);
    this.changes.set(watch.watchId, entries);
    this.transitionWatch(watch, {
      nextScanAt: new Date(
        Date.parse(this.clock.now()) + watch.debounceMs,
      ).toISOString(),
    });
    return structuredClone(change);
  }
  flush(
    watchId: string,
    expectedRevision: number,
    ttlMs: number,
  ): DefinitionWatchBatch | null {
    const watch = this.requireWatch(watchId);
    this.assertWatchRevision(watch, expectedRevision);
    if (watch.state !== "active")
      throw new E03RuntimeError(
        "definition_watch_flush_state",
        `definition watch ${watchId} is ${watch.state}`,
      );
    if (Date.parse(watch.nextScanAt) > Date.parse(this.clock.now()))
      return null;
    const pendingId = this.pendingByWatch.get(watchId);
    if (pendingId) return structuredClone(this.requireBatch(pendingId));
    const generationStart = watch.lastGeneration
      ? (this.changes.get(watchId) ?? []).findIndex(
          (value) => value.digest === watch.lastGeneration,
        ) + 1
      : 0;
    const available = (this.changes.get(watchId) ?? []).slice(
      Math.max(0, generationStart),
      Math.max(0, generationStart) + watch.maximumBatchSize,
    );
    if (!available.length) {
      this.transitionWatch(watch, {
        lastScanAt: this.clock.now(),
        nextScanAt: new Date(
          Date.parse(this.clock.now()) + watch.debounceMs,
        ).toISOString(),
      });
      return null;
    }
    if (!Number.isSafeInteger(ttlMs) || ttlMs < 1)
      throw new E03RuntimeError(
        "definition_watch_batch_ttl",
        "definition watch batch TTL is invalid",
      );
    const generation = digest(available.map((value) => value.digest));
    const payload = {
      batchId: createId("definition-watch-batch"),
      watchId,
      state: "pending" as const,
      changeIds: available.map((value) => value.changeId),
      generation,
      claimantId: null,
      attempt: 0,
      claimedAt: null,
      completedAt: null,
      expiresAt: new Date(Date.parse(this.clock.now()) + ttlMs).toISOString(),
      failure: null,
      revision: 1,
    };
    const batch = { ...payload, digest: digest(payload) };
    assertWatchBatch(batch);
    this.batches.set(batch.batchId, batch);
    this.pendingByWatch.set(watchId, batch.batchId);
    return structuredClone(batch);
  }
  claim(
    batchId: string,
    expectedRevision: number,
    claimantId: string,
  ): DefinitionWatchBatch {
    const batch = this.requireBatch(batchId);
    this.assertBatchRevision(batch, expectedRevision);
    if (batch.state !== "pending" && batch.state !== "failed")
      throw new E03RuntimeError(
        "definition_watch_batch_claim_state",
        `definition watch batch ${batchId} is ${batch.state}`,
      );
    if (!claimantId.trim())
      throw new E03RuntimeError(
        "definition_watch_batch_claimant",
        "definition watch batch claimant is required",
      );
    if (Date.parse(batch.expiresAt) <= Date.parse(this.clock.now()))
      return this.transitionBatch(batch, {
        state: "expired",
        completedAt: this.clock.now(),
      });
    return this.transitionBatch(batch, {
      state: "claimed",
      claimantId: claimantId.trim(),
      attempt: batch.attempt + 1,
      claimedAt: this.clock.now(),
      failure: null,
    });
  }
  complete(
    batchId: string,
    expectedRevision: number,
    result: { ok: boolean; error?: string | null },
  ): { batch: DefinitionWatchBatch; watch: DefinitionWatchTarget } {
    const batch = this.requireBatch(batchId);
    this.assertBatchRevision(batch, expectedRevision);
    if (batch.state !== "claimed")
      throw new E03RuntimeError(
        "definition_watch_batch_complete_state",
        `definition watch batch ${batchId} is ${batch.state}`,
      );
    const watch = this.requireWatch(batch.watchId);
    const nextBatch = this.transitionBatch(batch, {
      state: result.ok ? "applied" : "failed",
      completedAt: result.ok ? this.clock.now() : null,
      failure: result.ok
        ? null
        : result.error?.trim() || "definition reload failed",
      claimantId: result.ok ? batch.claimantId : null,
    });
    let nextWatch: DefinitionWatchTarget;
    if (result.ok) {
      const lastChangeId = batch.changeIds[batch.changeIds.length - 1]!;
      const lastChange = (this.changes.get(batch.watchId) ?? []).find(
        (value) => value.changeId === lastChangeId,
      )!;
      nextWatch = this.transitionWatch(watch, {
        state: "active",
        failureCount: 0,
        lastScanAt: this.clock.now(),
        lastGeneration: lastChange.digest,
        nextScanAt: new Date(
          Date.parse(this.clock.now()) + watch.debounceMs,
        ).toISOString(),
      });
      this.pendingByWatch.delete(batch.watchId);
    } else {
      const failureCount = watch.failureCount + 1;
      nextWatch = this.transitionWatch(watch, {
        state: "backoff",
        failureCount,
        nextScanAt: new Date(
          Date.parse(this.clock.now()) +
            Math.min(60_000, watch.debounceMs * 2 ** failureCount),
        ).toISOString(),
      });
    }
    return { batch: nextBatch, watch: nextWatch };
  }
  resume(watchId: string, expectedRevision: number): DefinitionWatchTarget {
    const watch = this.requireWatch(watchId);
    this.assertWatchRevision(watch, expectedRevision);
    if (watch.state !== "paused" && watch.state !== "backoff")
      throw new E03RuntimeError(
        "definition_watch_resume_state",
        `definition watch ${watchId} is ${watch.state}`,
      );
    return this.transitionWatch(watch, {
      state: "active",
      nextScanAt: this.clock.now(),
    });
  }
  close(watchId: string, expectedRevision: number): DefinitionWatchTarget {
    const watch = this.requireWatch(watchId);
    this.assertWatchRevision(watch, expectedRevision);
    if (this.pendingByWatch.has(watchId))
      throw new E03RuntimeError(
        "definition_watch_pending_batch",
        `definition watch ${watchId} has pending batch`,
      );
    if (watch.state === "closed") return structuredClone(watch);
    return this.transitionWatch(watch, {
      state: "closed",
      closedAt: this.clock.now(),
    });
  }
  snapshot(): {
    watches: DefinitionWatchTarget[];
    changes: DefinitionWatchChange[];
    batches: DefinitionWatchBatch[];
    pendingByWatch: Array<[string, string]>;
  } {
    return {
      watches: [...this.watches.values()].map((value) =>
        structuredClone(value),
      ),
      changes: [...this.changes.values()]
        .flat()
        .map((value) => structuredClone(value)),
      batches: [...this.batches.values()].map((value) =>
        structuredClone(value),
      ),
      pendingByWatch: [...this.pendingByWatch.entries()].map(
        ([watchId, batchId]) => [watchId, batchId],
      ),
    };
  }
  restore(snapshot: {
    watches: readonly DefinitionWatchTarget[];
    changes: readonly DefinitionWatchChange[];
    batches: readonly DefinitionWatchBatch[];
    pendingByWatch: ReadonlyArray<readonly [string, string]>;
  }): void {
    const watches = new Map<string, DefinitionWatchTarget>();
    const changes = new Map<string, DefinitionWatchChange[]>();
    const batches = new Map<string, DefinitionWatchBatch>();
    const pendingByWatch = new Map<string, string>();
    for (const value of snapshot.watches) {
      assertWatchTarget(value);
      if (watches.has(value.watchId))
        throw new E03RuntimeError(
          "definition_watch_restore_duplicate",
          `duplicate definition watch ${value.watchId}`,
        );
      watches.set(value.watchId, structuredClone(value));
    }
    for (const value of snapshot.changes) {
      assertWatchChange(value);
      if (!watches.has(value.watchId))
        throw new E03RuntimeError(
          "definition_watch_change_restore",
          `definition watch change ${value.changeId} has no watch`,
        );
      const entries = changes.get(value.watchId) ?? [];
      if (
        value.sequence !== entries.length + 1 ||
        value.previousDigest !== (entries[entries.length - 1]?.digest ?? "root")
      )
        throw new E03RuntimeError(
          "definition_watch_change_chain",
          `definition watch change ${value.changeId} breaks chain`,
        );
      entries.push(structuredClone(value));
      changes.set(value.watchId, entries);
    }
    for (const value of snapshot.batches) {
      assertWatchBatch(value);
      const available = changes.get(value.watchId) ?? [];
      if (
        batches.has(value.batchId) ||
        !watches.has(value.watchId) ||
        value.changeIds.some(
          (id) => !available.some((change) => change.changeId === id),
        )
      )
        throw new E03RuntimeError(
          "definition_watch_batch_restore",
          `definition watch batch ${value.batchId} is invalid`,
        );
      batches.set(value.batchId, structuredClone(value));
    }
    for (const [watchId, batchId] of snapshot.pendingByWatch) {
      const batch = batches.get(batchId);
      if (
        !batch ||
        batch.watchId !== watchId ||
        (batch.state !== "pending" &&
          batch.state !== "claimed" &&
          batch.state !== "failed") ||
        pendingByWatch.has(watchId)
      )
        throw new E03RuntimeError(
          "definition_watch_pending_restore",
          `definition watch pending index ${watchId} is invalid`,
        );
      pendingByWatch.set(watchId, batchId);
    }
    this.watches = watches;
    this.changes = changes;
    this.batches = batches;
    this.pendingByWatch = pendingByWatch;
  }
  private requireWatch(id: string): DefinitionWatchTarget {
    const value = this.watches.get(id);
    if (!value)
      throw new E03RuntimeError(
        "definition_watch_missing",
        `definition watch ${id} does not exist`,
      );
    assertWatchTarget(value);
    return value;
  }
  private requireBatch(id: string): DefinitionWatchBatch {
    const value = this.batches.get(id);
    if (!value)
      throw new E03RuntimeError(
        "definition_watch_batch_missing",
        `definition watch batch ${id} does not exist`,
      );
    assertWatchBatch(value);
    return value;
  }
  private assertWatchRevision(
    value: DefinitionWatchTarget,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "definition_watch_stale_revision",
        `definition watch ${value.watchId} revision is stale`,
      );
  }
  private assertBatchRevision(
    value: DefinitionWatchBatch,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "definition_watch_batch_stale_revision",
        `definition watch batch ${value.batchId} revision is stale`,
      );
  }
  private transitionWatch(
    value: DefinitionWatchTarget,
    patch: Partial<
      Omit<DefinitionWatchTarget, "watchId" | "revision" | "digest">
    >,
  ): DefinitionWatchTarget {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      watchId: value.watchId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertWatchTarget(next);
    this.watches.set(next.watchId, next);
    return structuredClone(next);
  }
  private transitionBatch(
    value: DefinitionWatchBatch,
    patch: Partial<
      Omit<DefinitionWatchBatch, "batchId" | "revision" | "digest">
    >,
  ): DefinitionWatchBatch {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      batchId: value.batchId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertWatchBatch(next);
    this.batches.set(next.batchId, next);
    return structuredClone(next);
  }
}

export interface DefinitionVisibilityRelease {
  releaseId: string;
  definitionId: string;
  definitionName: string;
  definitionDigest: string;
  sourceDigest: string;
  generation: number;
  consumerIds: string[];
  requiredAcknowledgements: number;
  acknowledgedConsumerIds: string[];
  rejectedConsumerIds: string[];
  state:
    | "staged"
    | "publishing"
    | "visible"
    | "invalidating"
    | "invalidated"
    | "failed";
  visibilityFence: string;
  invalidationReason: string;
  createdAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}

export interface DefinitionVisibilityAck {
  ackId: string;
  releaseId: string;
  consumerId: string;
  generation: number;
  action: "load" | "invalidate";
  accepted: boolean;
  observedDefinitionDigest: string;
  cacheDigest: string;
  errorCode: string;
  previousConsumerDigest: string;
  acknowledgedAt: string;
  revision: number;
  digest: string;
}

export interface DefinitionVisibilitySnapshot {
  releases: DefinitionVisibilityRelease[];
  acknowledgements: DefinitionVisibilityAck[];
  activeReleaseIdByDefinition: Array<[string, string]>;
  latestAckIdByReleaseConsumerAction: Array<[string, string]>;
  digest: string;
}

function assertDefinitionVisibilityRelease(
  value: DefinitionVisibilityRelease,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.releaseId ||
    !value.definitionId ||
    !value.definitionName ||
    !value.definitionDigest ||
    !value.sourceDigest ||
    value.generation < 1 ||
    !value.consumerIds.length ||
    new Set(value.consumerIds).size !== value.consumerIds.length ||
    value.requiredAcknowledgements < 1 ||
    value.requiredAcknowledgements > value.consumerIds.length ||
    value.acknowledgedConsumerIds.some((id) =>
      value.rejectedConsumerIds.includes(id),
    ) ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "definition_visibility_release_corrupt",
      `definition visibility release ${value.releaseId || "<empty>"} is corrupt`,
    );
  if (value.state === "visible" && !value.visibilityFence)
    throw new E03RuntimeError(
      "definition_visibility_fence_missing",
      "visible definition release requires fence",
    );
  if (
    ["invalidating", "invalidated"].includes(value.state) &&
    !value.invalidationReason
  )
    throw new E03RuntimeError(
      "definition_visibility_invalidation_reason_missing",
      "definition invalidation requires reason",
    );
}

function assertDefinitionVisibilityAck(value: DefinitionVisibilityAck): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.ackId ||
    !value.releaseId ||
    !value.consumerId ||
    value.generation < 1 ||
    !value.observedDefinitionDigest ||
    !value.cacheDigest ||
    (!value.accepted && !value.errorCode) ||
    !value.previousConsumerDigest ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "definition_visibility_ack_corrupt",
      `definition visibility ack ${value.ackId || "<empty>"} is corrupt`,
    );
}

export class DefinitionVisibilityRuntime {
  private releases = new Map<string, DefinitionVisibilityRelease>();
  private acknowledgements = new Map<string, DefinitionVisibilityAck>();
  private activeReleaseIdByDefinition = new Map<string, string>();
  private latestAckIdByReleaseConsumerAction = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  stage(input: {
    definition: E03AgentDefinition;
    sourceDigest: string;
    consumerIds: string[];
    requiredAcknowledgements: number;
  }): DefinitionVisibilityRelease {
    const definitionId = `${input.definition.name}@${input.definition.version}`;
    const activeId = this.activeReleaseIdByDefinition.get(definitionId);
    const active = activeId ? this.releases.get(activeId) : undefined;
    if (active && !["invalidated", "failed"].includes(active.state)) {
      if (active.definitionDigest !== input.definition.digest)
        throw new E03RuntimeError(
          "definition_visibility_active_release_conflict",
          `definition ${definitionId} already has active release`,
        );
      return structuredClone(active);
    }
    const consumers = [...new Set(input.consumerIds)].sort();
    const now = this.clock.now();
    const payload = {
      releaseId: createId("definition-visibility-release"),
      definitionId,
      definitionName: input.definition.name,
      definitionDigest: input.definition.digest,
      sourceDigest: input.sourceDigest,
      generation: (active?.generation ?? 0) + 1,
      consumerIds: consumers,
      requiredAcknowledgements: input.requiredAcknowledgements,
      acknowledgedConsumerIds: [] as string[],
      rejectedConsumerIds: [] as string[],
      state: "staged" as const,
      visibilityFence: "",
      invalidationReason: "",
      createdAt: now,
      updatedAt: now,
      revision: 1,
    };
    const release = { ...payload, digest: digest(payload) };
    assertDefinitionVisibilityRelease(release);
    this.releases.set(release.releaseId, release);
    this.activeReleaseIdByDefinition.set(
      release.definitionId,
      release.releaseId,
    );
    return structuredClone(release);
  }

  publish(input: {
    releaseId: string;
    expectedRevision: number;
  }): DefinitionVisibilityRelease {
    const release = this.requireRelease(input.releaseId);
    this.assertReleaseRevision(release, input.expectedRevision);
    if (release.state !== "staged")
      throw new E03RuntimeError(
        "definition_visibility_publish_invalid_state",
        `cannot publish definition release from ${release.state}`,
      );
    return this.transitionRelease(release, {
      state: "publishing",
      updatedAt: this.clock.now(),
    });
  }

  acknowledge(input: {
    releaseId: string;
    expectedRevision: number;
    generation: number;
    consumerId: string;
    action: "load" | "invalidate";
    accepted: boolean;
    observedDefinitionDigest: string;
    cacheDigest: string;
    errorCode?: string;
  }): {
    release: DefinitionVisibilityRelease;
    acknowledgement: DefinitionVisibilityAck;
  } {
    const release = this.requireRelease(input.releaseId);
    this.assertReleaseRevision(release, input.expectedRevision);
    const expectedState =
      input.action === "load" ? "publishing" : "invalidating";
    if (
      release.state !== expectedState ||
      release.generation !== input.generation
    )
      throw new E03RuntimeError(
        "definition_visibility_ack_state_stale",
        "definition visibility acknowledgement state or generation is stale",
      );
    if (!release.consumerIds.includes(input.consumerId))
      throw new E03RuntimeError(
        "definition_visibility_consumer_unknown",
        `definition visibility consumer ${input.consumerId} is unknown`,
      );
    if (input.observedDefinitionDigest !== release.definitionDigest)
      throw new E03RuntimeError(
        "definition_visibility_digest_mismatch",
        "definition visibility consumer observed another definition digest",
      );
    const key = this.ackKey(release.releaseId, input.consumerId, input.action);
    const priorId = this.latestAckIdByReleaseConsumerAction.get(key);
    const prior = priorId ? this.acknowledgements.get(priorId) : undefined;
    if (prior?.accepted) {
      if (prior.cacheDigest !== input.cacheDigest)
        throw new E03RuntimeError(
          "definition_visibility_ack_conflict",
          `definition visibility consumer ${input.consumerId} changed cache digest`,
        );
      return {
        release: structuredClone(release),
        acknowledgement: structuredClone(prior),
      };
    }
    const payload = {
      ackId: createId("definition-visibility-ack"),
      releaseId: release.releaseId,
      consumerId: input.consumerId,
      generation: release.generation,
      action: input.action,
      accepted: input.accepted,
      observedDefinitionDigest: input.observedDefinitionDigest,
      cacheDigest: input.cacheDigest,
      errorCode: input.errorCode ?? "",
      previousConsumerDigest:
        prior?.digest ?? digest("definition-visibility-ack-root"),
      acknowledgedAt: this.clock.now(),
      revision: (prior?.revision ?? 0) + 1,
    };
    const acknowledgement = { ...payload, digest: digest(payload) };
    assertDefinitionVisibilityAck(acknowledgement);
    this.acknowledgements.set(acknowledgement.ackId, acknowledgement);
    this.latestAckIdByReleaseConsumerAction.set(key, acknowledgement.ackId);
    const accepted = new Set(release.acknowledgedConsumerIds);
    const rejected = new Set(release.rejectedConsumerIds);
    if (input.accepted) {
      accepted.add(input.consumerId);
      rejected.delete(input.consumerId);
    } else {
      rejected.add(input.consumerId);
      accepted.delete(input.consumerId);
    }
    const next = this.transitionRelease(release, {
      acknowledgedConsumerIds: [...accepted].sort(),
      rejectedConsumerIds: [...rejected].sort(),
      state:
        !input.accepted && input.action === "load" ? "failed" : release.state,
      updatedAt: this.clock.now(),
    });
    return { release: next, acknowledgement: structuredClone(acknowledgement) };
  }

  makeVisible(input: {
    releaseId: string;
    expectedRevision: number;
    visibilityFence: string;
  }): DefinitionVisibilityRelease {
    const release = this.requireRelease(input.releaseId);
    this.assertReleaseRevision(release, input.expectedRevision);
    if (release.state === "visible") {
      if (release.visibilityFence !== input.visibilityFence)
        throw new E03RuntimeError(
          "definition_visibility_fence_conflict",
          "definition release is visible under another fence",
        );
      return structuredClone(release);
    }
    if (release.state !== "publishing")
      throw new E03RuntimeError(
        "definition_visibility_activate_invalid_state",
        "definition release is not publishing",
      );
    if (
      release.acknowledgedConsumerIds.length < release.requiredAcknowledgements
    )
      throw new E03RuntimeError(
        "definition_visibility_quorum_missing",
        "definition release acknowledgement quorum is missing",
      );
    if (!input.visibilityFence)
      throw new E03RuntimeError(
        "definition_visibility_fence_required",
        "definition visibility fence is required",
      );
    return this.transitionRelease(release, {
      state: "visible",
      visibilityFence: input.visibilityFence,
      updatedAt: this.clock.now(),
    });
  }

  invalidate(input: {
    releaseId: string;
    expectedRevision: number;
    reason: string;
  }): DefinitionVisibilityRelease {
    const release = this.requireRelease(input.releaseId);
    this.assertReleaseRevision(release, input.expectedRevision);
    if (release.state !== "visible" && release.state !== "failed")
      throw new E03RuntimeError(
        "definition_visibility_invalidate_invalid_state",
        `cannot invalidate definition release from ${release.state}`,
      );
    if (!input.reason)
      throw new E03RuntimeError(
        "definition_visibility_invalidation_reason_required",
        "definition invalidation requires reason",
      );
    return this.transitionRelease(release, {
      state: "invalidating",
      acknowledgedConsumerIds: [],
      rejectedConsumerIds: [],
      invalidationReason: input.reason,
      updatedAt: this.clock.now(),
    });
  }

  completeInvalidation(input: {
    releaseId: string;
    expectedRevision: number;
  }): DefinitionVisibilityRelease {
    const release = this.requireRelease(input.releaseId);
    this.assertReleaseRevision(release, input.expectedRevision);
    if (release.state !== "invalidating")
      throw new E03RuntimeError(
        "definition_visibility_invalidation_invalid_state",
        "definition release is not invalidating",
      );
    if (
      release.acknowledgedConsumerIds.length < release.requiredAcknowledgements
    )
      throw new E03RuntimeError(
        "definition_visibility_invalidation_quorum_missing",
        "definition invalidation acknowledgement quorum is missing",
      );
    const next = this.transitionRelease(release, {
      state: "invalidated",
      updatedAt: this.clock.now(),
    });
    this.activeReleaseIdByDefinition.delete(release.definitionId);
    return next;
  }

  snapshot(): DefinitionVisibilitySnapshot {
    const payload = {
      releases: [...this.releases.values()].map((value) =>
        structuredClone(value),
      ),
      acknowledgements: [...this.acknowledgements.values()].map((value) =>
        structuredClone(value),
      ),
      activeReleaseIdByDefinition: [
        ...this.activeReleaseIdByDefinition.entries(),
      ],
      latestAckIdByReleaseConsumerAction: [
        ...this.latestAckIdByReleaseConsumerAction.entries(),
      ],
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(snapshot: DefinitionVisibilitySnapshot): void {
    const { digest: expected, ...payload } = snapshot;
    if (digest(payload) !== expected)
      throw new E03RuntimeError(
        "definition_visibility_snapshot_corrupt",
        "definition visibility snapshot digest mismatch",
      );
    const releases = new Map<string, DefinitionVisibilityRelease>();
    const acknowledgements = new Map<string, DefinitionVisibilityAck>();
    for (const value of payload.releases) {
      assertDefinitionVisibilityRelease(value);
      if (releases.has(value.releaseId))
        throw new E03RuntimeError(
          "definition_visibility_snapshot_release_duplicate",
          `duplicate definition visibility release ${value.releaseId}`,
        );
      releases.set(value.releaseId, structuredClone(value));
    }
    for (const value of payload.acknowledgements) {
      assertDefinitionVisibilityAck(value);
      if (!releases.has(value.releaseId))
        throw new E03RuntimeError(
          "definition_visibility_snapshot_ack_orphaned",
          `definition visibility ack ${value.ackId} is orphaned`,
        );
      acknowledgements.set(value.ackId, structuredClone(value));
    }
    const activeIndex = new Map(payload.activeReleaseIdByDefinition);
    for (const [definitionId, releaseId] of activeIndex) {
      const value = releases.get(releaseId);
      if (
        !value ||
        value.definitionId !== definitionId ||
        ["invalidated"].includes(value.state)
      )
        throw new E03RuntimeError(
          "definition_visibility_snapshot_active_index_corrupt",
          `definition visibility active release ${releaseId} is invalid`,
        );
    }
    const ackIndex = new Map(payload.latestAckIdByReleaseConsumerAction);
    for (const ackId of ackIndex.values())
      if (!acknowledgements.has(ackId))
        throw new E03RuntimeError(
          "definition_visibility_snapshot_ack_index_corrupt",
          `definition visibility ack index references ${ackId}`,
        );
    this.releases = releases;
    this.acknowledgements = acknowledgements;
    this.activeReleaseIdByDefinition = activeIndex;
    this.latestAckIdByReleaseConsumerAction = ackIndex;
  }

  private ackKey(
    releaseId: string,
    consumerId: string,
    action: string,
  ): string {
    return `${releaseId}:${consumerId}:${action}`;
  }

  private requireRelease(id: string): DefinitionVisibilityRelease {
    const value = this.releases.get(id);
    if (!value)
      throw new E03RuntimeError(
        "definition_visibility_release_missing",
        `definition visibility release ${id} does not exist`,
      );
    assertDefinitionVisibilityRelease(value);
    return value;
  }

  private assertReleaseRevision(
    value: DefinitionVisibilityRelease,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "definition_visibility_release_stale_revision",
        `definition visibility release ${value.releaseId} revision is stale`,
      );
  }

  private transitionRelease(
    value: DefinitionVisibilityRelease,
    patch: Partial<
      Omit<DefinitionVisibilityRelease, "releaseId" | "revision" | "digest">
    >,
  ): DefinitionVisibilityRelease {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      releaseId: value.releaseId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertDefinitionVisibilityRelease(next);
    this.releases.set(next.releaseId, next);
    return structuredClone(next);
  }
}
