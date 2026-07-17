#!/usr/bin/env bun

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";

type Json = Record<string, unknown>;

interface Arguments {
  candidate: string;
  output?: string;
  worktree: boolean;
  allowDirty: boolean;
  skipMutationEvidence: boolean;
}

interface ChangedFile {
  path: string;
  text: string;
  lines: string[];
  changedLines: Set<number>;
}

interface FingerprintedUnit {
  id: string;
  path: string;
  executableLines: number;
  tokenCount: number;
  fingerprints: Set<string>;
}

interface DuplicatePair {
  retainedUnit: string;
  deductedUnit: string;
  similarity: number;
  lengthRatio: number;
  deductedLines: number;
}

interface TestCaseUnit extends FingerprintedUnit {
  title: string;
  failureCase: boolean;
}

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const workspaceRoot = resolve(repoRoot, "..");
const manifestRoot = join(
  workspaceRoot,
  "docs/remediations/M1-R01-claude-source-custody/manifests",
);
const sourceManifestPath = join(manifestRoot, "execution-02-source-manifest.jsonl");
const pythonManifestPath = join(manifestRoot, "execution-02-python-owner-baseline.jsonl");
const targetManifestPath = join(manifestRoot, "execution-02-target-custody-map.jsonl");
const mutationManifestPath = join(manifestRoot, "execution-02-mutation-manifest.jsonl");
const profilePath = join(manifestRoot, "execution-02-gate-profile.json");
const receiptPath = join(manifestRoot, "execution-02-baseline-receipt.json");
const mutationEvidencePath = join(
  repoRoot,
  "docs/reviews/evidence/M1-R01-v3/execution-02/mutation-results.json",
);
const cleanroomEvidencePath = join(
  repoRoot,
  "docs/reviews/evidence/M1-R01-v3/execution-02/cleanroom-result.json",
);
const requiredProbeEvidence = [
  "runtime-origin-result.json",
  "write-path-result.json",
  "same-session-resume-result.json",
  "lost-ack-result.json",
  "disable-result.json",
] as const;

const args = parseArguments();
const failures: string[] = [];
const checks: Json = {};

function parseArguments(): Arguments {
  const value: Arguments = {
    candidate: "HEAD",
    worktree: false,
    allowDirty: false,
    skipMutationEvidence: false,
  };
  for (let index = 2; index < process.argv.length; index += 1) {
    const argument = process.argv[index];
    if (argument === "--candidate") value.candidate = process.argv[++index] ?? "";
    else if (argument === "--output") value.output = process.argv[++index];
    else if (argument === "--worktree") value.worktree = true;
    else if (argument === "--allow-dirty") value.allowDirty = true;
    else if (argument === "--skip-mutation-evidence") value.skipMutationEvidence = true;
    else throw new Error(`unknown argument ${argument}`);
  }
  if (!value.candidate) throw new Error("--candidate requires a commit");
  return value;
}

function fail(message: string): void {
  failures.push(message);
}

function gitText(cwd: string, command: readonly string[], trim = true): string {
  const output = execFileSync("git", [...command], {
    cwd,
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
  });
  return trim ? output.trim() : output;
}

function gitBytes(cwd: string, command: readonly string[]): Buffer {
  return execFileSync("git", [...command], {
    cwd,
    encoding: "buffer",
    maxBuffer: 256 * 1024 * 1024,
  }) as Buffer;
}

function sha256(value: Uint8Array | string): string {
  return createHash("sha256").update(value).digest("hex");
}

function readJson(path: string): Json {
  return JSON.parse(readFileSync(path, "utf8")) as Json;
}

function readJsonLines(path: string): Json[] {
  return readFileSync(path, "utf8")
    .split(/\r?\n/)
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line) as Json);
}

function executableLine(line: string): boolean {
  const value = line.trim();
  return Boolean(value)
    && !value.startsWith("//")
    && !value.startsWith("/*")
    && !value.startsWith("*")
    && value !== "*/";
}

function pythonExecutableLine(line: string): boolean {
  const value = line.trim();
  return Boolean(value)
    && !value.startsWith("#")
    && !value.startsWith('\"\"\"')
    && !value.startsWith("'''");
}

const candidateTextCache = new Map<string, string>();
function candidateText(path: string): string {
  const cached = candidateTextCache.get(path);
  if (cached !== undefined) return cached;
  const text = args.worktree
    ? readFileSync(join(repoRoot, path), "utf8")
    : gitBytes(repoRoot, ["show", `${args.candidate}:${path}`]).toString("utf8");
  candidateTextCache.set(path, text);
  return text;
}

let cachedCandidatePaths: string[] | null = null;
let cachedCandidatePathSet: Set<string> | null = null;
function candidatePaths(): string[] {
  if (cachedCandidatePaths) return cachedCandidatePaths;
  cachedCandidatePaths = !args.worktree
    ? gitText(repoRoot, ["ls-tree", "-r", "--name-only", args.candidate])
      .split(/\r?\n/)
      .filter(Boolean)
    : gitText(repoRoot, ["ls-files", "--cached", "--others", "--exclude-standard"])
      .split(/\r?\n/)
      .filter((path) => path && existsSync(join(repoRoot, path)) && statSync(join(repoRoot, path)).isFile());
  return cachedCandidatePaths;
}

function candidateExists(path: string): boolean {
  cachedCandidatePathSet ??= new Set(candidatePaths());
  return cachedCandidatePathSet.has(path);
}

function changedLineNumbers(baseline: string, path: string, untracked: boolean): Set<number> {
  if (untracked) {
    const lines = candidateText(path).split(/\r?\n/);
    return new Set(lines.map((_, index) => index + 1));
  }
  const target = args.worktree ? undefined : args.candidate;
  const command = target
    ? ["diff", "--unified=0", baseline, target, "--", path]
    : ["diff", "--unified=0", baseline, "--", path];
  const diff = gitText(repoRoot, command, false);
  const output = new Set<number>();
  for (const line of diff.split(/\r?\n/)) {
    const match = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/.exec(line);
    if (!match) continue;
    const start = Number(match[1]);
    const length = match[2] === undefined ? 1 : Number(match[2]);
    for (let offset = 0; offset < length; offset += 1) output.add(start + offset);
  }
  return output;
}

function pathUnder(path: string, roots: readonly string[]): boolean {
  return roots.some((root) => path === root || path.startsWith(`${root}/`));
}

function productionTypeScript(path: string, roots: readonly string[], excluded: readonly string[]): boolean {
  return /\.tsx?$/.test(path)
    && pathUnder(path, roots)
    && !pathUnder(path, excluded)
    && !/(^|\/)(test|tests|fixtures|generated|vendor|source-pool)(\/|$)/.test(path)
    && !/\.test\.tsx?$/.test(path);
}

function testTypeScript(path: string, roots: readonly string[]): boolean {
  return /\.tsx?$/.test(path) && pathUnder(path, roots);
}

function normalizeTokens(text: string): string[] {
  const scanner = ts.createScanner(ts.ScriptTarget.Latest, true, ts.LanguageVariant.Standard, text);
  const tokens: string[] = [];
  while (true) {
    const kind = scanner.scan();
    if (kind === ts.SyntaxKind.EndOfFileToken) break;
    if (
      kind === ts.SyntaxKind.StringLiteral
      || kind === ts.SyntaxKind.NoSubstitutionTemplateLiteral
      || kind === ts.SyntaxKind.NumericLiteral
      || kind === ts.SyntaxKind.BigIntLiteral
    ) {
      tokens.push("literal");
    } else if (kind === ts.SyntaxKind.Identifier) {
      tokens.push("identifier");
    } else {
      tokens.push(scanner.getTokenText());
    }
  }
  return tokens;
}

function winnow(tokens: readonly string[]): Set<string> {
  if (tokens.length < 5) return new Set();
  const shingles: string[] = [];
  for (let index = 0; index <= tokens.length - 5; index += 1) {
    shingles.push(createHash("sha1").update(tokens.slice(index, index + 5).join("\u001f")).digest("hex").slice(0, 16));
  }
  if (shingles.length <= 8) return new Set(shingles);
  const fingerprints = new Set<string>();
  for (let start = 0; start <= shingles.length - 8; start += 1) {
    const window = shingles.slice(start, start + 8);
    let selected = window[0]!;
    for (const value of window) if (value <= selected) selected = value;
    fingerprints.add(selected);
  }
  return fingerprints;
}

function jaccard(left: ReadonlySet<string>, right: ReadonlySet<string>): number {
  if (!left.size || !right.size) return 0;
  let intersection = 0;
  for (const value of left) if (right.has(value)) intersection += 1;
  return intersection / (left.size + right.size - intersection);
}

function duplicateUnits(units: readonly FingerprintedUnit[]): { deduction: number; pairs: DuplicatePair[] } {
  const eligible = units
    .filter((unit) => unit.tokenCount >= 20 && unit.executableLines >= 3)
    .sort((left, right) => left.id.localeCompare(right.id));
  const fingerprintFrequency = new Map<string, number>();
  for (const unit of eligible) {
    for (const fingerprint of unit.fingerprints) {
      fingerprintFrequency.set(fingerprint, (fingerprintFrequency.get(fingerprint) ?? 0) + 1);
    }
  }
  const selectedFingerprints = (unit: FingerprintedUnit): string[] => [...unit.fingerprints]
    .sort((left, right) =>
      (fingerprintFrequency.get(left) ?? 0) - (fingerprintFrequency.get(right) ?? 0)
      || left.localeCompare(right))
    .slice(0, 12);
  const deducted = new Set<string>();
  const pairs: DuplicatePair[] = [];
  const priorByFingerprint = new Map<string, number[]>();
  for (let rightIndex = 0; rightIndex < eligible.length; rightIndex += 1) {
    const right = eligible[rightIndex]!;
    let best: { unit: FingerprintedUnit; similarity: number; ratio: number } | undefined;
    const candidateIndices = new Set<number>();
    for (const fingerprint of selectedFingerprints(right)) {
      for (const index of priorByFingerprint.get(fingerprint) ?? []) candidateIndices.add(index);
    }
    for (const leftIndex of [...candidateIndices].sort((left, rightValue) => left - rightValue)) {
      const left = eligible[leftIndex]!;
      if (deducted.has(left.id)) continue;
      const ratio = Math.min(left.tokenCount, right.tokenCount) / Math.max(left.tokenCount, right.tokenCount);
      if (ratio < 0.8) continue;
      const similarity = jaccard(left.fingerprints, right.fingerprints);
      if (similarity < 0.8) continue;
      if (!best || similarity > best.similarity) best = { unit: left, similarity, ratio };
    }
    for (const fingerprint of selectedFingerprints(right)) {
      const indices = priorByFingerprint.get(fingerprint) ?? [];
      indices.push(rightIndex);
      priorByFingerprint.set(fingerprint, indices);
    }
    if (!best) continue;
    deducted.add(right.id);
    pairs.push({
      retainedUnit: best.unit.id,
      deductedUnit: right.id,
      similarity: Number(best.similarity.toFixed(4)),
      lengthRatio: Number(best.ratio.toFixed(4)),
      deductedLines: right.executableLines,
    });
  }
  return {
    deduction: pairs.reduce((sum, pair) => sum + pair.deductedLines, 0),
    pairs,
  };
}

function changedProductionUnits(file: ChangedFile): FingerprintedUnit[] {
  const source = ts.createSourceFile(
    file.path,
    file.text,
    ts.ScriptTarget.Latest,
    true,
    file.path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const output: FingerprintedUnit[] = [];
  const covered = new Set<number>();
  const add = (node: ts.Node, label: string): void => {
    const start = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
    const end = source.getLineAndCharacterOfPosition(Math.max(node.getStart(source), node.end - 1)).line + 1;
    let executableLines = 0;
    for (let line = start; line <= end; line += 1) {
      covered.add(line);
      if (file.changedLines.has(line) && executableLine(file.lines[line - 1] ?? "")) executableLines += 1;
    }
    if (!executableLines) return;
    const tokens = normalizeTokens(node.getText(source));
    output.push({
      id: `${file.path}:${start}:${label}`,
      path: file.path,
      executableLines,
      tokenCount: tokens.length,
      fingerprints: winnow(tokens),
    });
  };
  const visit = (node: ts.Node): void => {
    if (
      ts.isFunctionDeclaration(node)
      || ts.isMethodDeclaration(node)
      || ts.isConstructorDeclaration(node)
      || ts.isGetAccessorDeclaration(node)
      || ts.isSetAccessorDeclaration(node)
    ) {
      const name = "name" in node && node.name ? node.name.getText(source) : ts.SyntaxKind[node.kind];
      add(node, name);
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  let start: number | null = null;
  const flush = (end: number): void => {
    if (start === null) return;
    const first = start;
    const text = file.lines.slice(first - 1, end).join("\n");
    const executableLines = [...file.changedLines]
      .filter((line) => line >= first && line <= end && executableLine(file.lines[line - 1] ?? ""))
      .length;
    const tokens = normalizeTokens(text);
    if (executableLines && tokens.length) {
      output.push({
        id: `${file.path}:${first}:module-surface`,
        path: file.path,
        executableLines,
        tokenCount: tokens.length,
        fingerprints: winnow(tokens),
      });
    }
    start = null;
  };
  for (let line = 1; line <= file.lines.length; line += 1) {
    if (covered.has(line) || !file.changedLines.has(line) || !executableLine(file.lines[line - 1] ?? "")) {
      flush(line - 1);
    } else if (start === null) {
      start = line;
    }
  }
  flush(file.lines.length);
  return output;
}

function testCaseUnits(path: string, text: string): TestCaseUnit[] {
  const source = ts.createSourceFile(
    path,
    text,
    ts.ScriptTarget.Latest,
    true,
    path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const lines = text.split(/\r?\n/);
  const output: TestCaseUnit[] = [];
  const visit = (node: ts.Node): void => {
    if (!ts.isCallExpression(node)) {
      ts.forEachChild(node, visit);
      return;
    }
    const expression = node.expression;
    const callName = ts.isIdentifier(expression)
      ? expression.text
      : ts.isPropertyAccessExpression(expression)
        ? expression.name.text
        : "";
    const titleNode = node.arguments[0];
    const callback = node.arguments[1];
    if (
      (callName === "test" || callName === "it")
      && titleNode
      && ts.isStringLiteralLike(titleNode)
      && callback
      && (ts.isArrowFunction(callback) || ts.isFunctionExpression(callback))
    ) {
      let ancestor: ts.Node | undefined = node.parent;
      let generated = false;
      while (ancestor) {
        if (ts.isForStatement(ancestor) || ts.isForInStatement(ancestor) || ts.isForOfStatement(ancestor)) {
          generated = true;
          break;
        }
        ancestor = ancestor.parent;
      }
      if (!generated) {
        const start = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
        const end = source.getLineAndCharacterOfPosition(Math.max(node.getStart(source), node.end - 1)).line + 1;
        const executableLines = lines.slice(start - 1, end).filter(executableLine).length;
        const body = callback.body.getText(source);
        const tokens = normalizeTokens(body);
        const title = titleNode.text;
        output.push({
          id: `${path}:${start}:${title}`,
          path,
          title,
          failureCase: /(?:fail|failure|crash|reject|deny|invalid|stale|tamper|timeout|cancel|abort|lost|duplicate|disable|recovery|poison|mutation|forbid|mismatch|conflict|expired|unknown|outside|partial|indeterminate)/i.test(title),
          executableLines,
          tokenCount: tokens.length,
          fingerprints: winnow(tokens),
        });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return output;
}

const declarationCache = new Map<string, boolean>();
function declarationExists(path: string, symbolValue: string): boolean {
  const cacheKey = `${path}::${symbolValue}`;
  const cached = declarationCache.get(cacheKey);
  if (cached !== undefined) return cached;
  const text = candidateText(path);
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const segments = symbolValue.split(".");
  const leaf = segments.at(-1) ?? symbolValue;
  const owner = segments.length > 1 ? segments.at(-2) ?? "" : "";
  let found = false;
  const visit = (node: ts.Node): void => {
    if (found) return;
    if (owner && ts.isClassDeclaration(node) && node.name?.text === owner) {
      for (const member of node.members) {
        const name = member.name?.getText(source).replace(/["']/g, "");
        if (name === leaf) {
          found = true;
          return;
        }
      }
    }
    if (!owner) {
      if (
        (ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node) || ts.isVariableDeclaration(node))
        && node.name?.getText(source) === leaf
      ) {
        found = true;
        return;
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  declarationCache.set(cacheKey, found);
  return found;
}

function uniqueRangeLines(rows: readonly Json[], pathField: string): Set<string> {
  const output = new Set<string>();
  for (const row of rows) {
    const start = Number(row.start_line);
    const end = Number(row.end_line);
    for (let line = start; line <= end; line += 1) {
      output.add(`${String(row[pathField])}:${line}`);
    }
  }
  return output;
}

function pythonExecutableLines(path: string): number {
  if (!existsSync(path)) return 0;
  return readFileSync(path, "utf8").split(/\r?\n/).filter((line) => {
    const value = line.trim();
    return Boolean(value) && !value.startsWith("#") && !value.startsWith('"""') && !value.startsWith("'''");
  }).length;
}

function nonEmptyString(row: Json, field: string): boolean {
  return typeof row[field] === "string" && String(row[field]).trim().length > 0;
}

function nonEmptyStringArray(row: Json, field: string): string[] {
  if (!Array.isArray(row[field])) return [];
  return (row[field] as unknown[])
    .filter((value): value is string => typeof value === "string" && value.trim().length > 0);
}

function commandArray(value: unknown): value is string[] {
  return Array.isArray(value)
    && value.length > 0
    && value.every((item) => typeof item === "string" && item.length > 0);
}

const profile = readJson(profilePath);
const receipt = readJson(receiptPath);
const thresholds = profile.thresholds as Json;
const sourceRows = readJsonLines(sourceManifestPath);
const pythonRows = readJsonLines(pythonManifestPath);
const targetRows = readJsonLines(targetManifestPath);
const mutationRows = readJsonLines(mutationManifestPath);
const productionRoots = (profile.production_roots as unknown[]).map(String);
const excludedRoots = (profile.excluded_production_prefixes as unknown[]).map(String);
const behaviorRoots = (profile.behavior_test_roots as unknown[]).map(String);
const adapterRoots = (profile.adapter_roots as unknown[]).map(String);
const baseline = String(profile.implementation_diff_baseline);
const dirty = gitText(repoRoot, ["status", "--porcelain=v1", "--untracked-files=all"], false)
  .trimEnd()
  .split(/\r?\n/)
  .filter(Boolean);

if (String(profile.execution_id) !== "E02" || String(profile.schema_version) !== "3.0") {
  fail("gate profile is not the frozen E02 schema-v3 profile");
}
for (const field of [
  "candidate_scope_paths",
  "forbidden_runtime_paths",
  "source_validator_command",
  "effective_loc_clone_command",
  "runtime_origin_probe_command",
  "write_path_probe_command",
  "same_session_resume_command",
  "lost_ack_command",
  "disable_command",
  "clean_dependency_path_command",
]) {
  if (!commandArray(profile[field])) fail(`gate profile lacks executable array ${field}`);
}
const profileCommands = profile.commands as Json;
for (const field of ["install", "typecheck", "build", "behavior_test", "built_entry", "candidate_gate", "mutation", "cleanroom"]) {
  if (!commandArray(profileCommands?.[field])) fail(`gate profile command ${field} is not a non-empty argv array`);
}
if (String((profile.required_toolchain as Json)?.bun) !== "1.2.15") fail("gate profile does not lock Bun 1.2.15");
if (String(receipt.verified_zyra_head) !== baseline) fail("baseline receipt and gate profile disagree");
if (receipt.clean_worktree !== true) fail("baseline receipt was not finalized from a clean worktree");
if (!nonEmptyString(receipt, "captured_at_utc") || !nonEmptyString(receipt, "verified_head_tree")) {
  fail("baseline receipt lacks capture time or verified tree");
}
if (!commandArray(receipt.manifest_generator_command) || !commandArray(receipt.schema_validator_command)) {
  fail("baseline receipt lacks reproducible generator or validator argv");
}
if (String(receipt.lockfile_sha256) !== sha256(readFileSync(join(repoRoot, "bun.lock")))) {
  fail("baseline receipt lockfile digest drifted");
}
const checkerSources = profile.checker_sources as Json;
for (const [path, expected] of Object.entries(checkerSources ?? {})) {
  if (!candidateExists(path) || sha256(candidateText(path)) !== String(expected)) {
    fail(`checker source digest drifted: ${path}`);
  }
}
if (!args.worktree) {
  const resolvedCandidate = gitText(repoRoot, ["rev-parse", args.candidate]);
  try {
    gitText(repoRoot, ["merge-base", "--is-ancestor", baseline, args.candidate]);
  } catch {
    fail(`candidate ${args.candidate} does not descend from verified baseline ${baseline}`);
  }
  if (String(profile.candidate_head_at_g0 ?? "") !== resolvedCandidate) {
    fail(`gate profile candidate binding ${String(profile.candidate_head_at_g0 ?? "<missing>")} does not equal ${resolvedCandidate}`);
  }
  if (String(receipt.g0_candidate_head ?? "") !== resolvedCandidate) {
    fail(`baseline receipt candidate binding ${String(receipt.g0_candidate_head ?? "<missing>")} does not equal ${resolvedCandidate}`);
  }
  if (String(receipt.g0_candidate_tree ?? "") !== gitText(repoRoot, ["show", "-s", "--format=%T", resolvedCandidate])) {
    fail("baseline receipt candidate tree does not bind the reviewed implementation tree");
  }
  const allowedControlPlaneCommits = nonEmptyStringArray(profile, "allowed_control_plane_commits");
  if (!allowedControlPlaneCommits.includes(resolvedCandidate)) {
    fail("reviewed implementation candidate is absent from allowed_control_plane_commits");
  }
  for (const commitValue of allowedControlPlaneCommits) {
    try {
      gitText(repoRoot, ["merge-base", "--is-ancestor", baseline, commitValue]);
      gitText(repoRoot, ["merge-base", "--is-ancestor", commitValue, resolvedCandidate]);
    } catch {
      fail(`allowed control-plane commit is outside baseline..candidate ancestry: ${commitValue}`);
    }
  }
}
if (!args.allowDirty && !args.worktree && dirty.length) {
  fail(`candidate gate requires a clean worktree; observed ${dirty.length} dirty paths`);
}

const frozenFiles = [sourceManifestPath, pythonManifestPath, targetManifestPath, mutationManifestPath, profilePath];
const receiptHashes = receipt.manifest_sha256 as Json;
for (const path of frozenFiles) {
  const name = relative(manifestRoot, path).replaceAll("\\", "/");
  const expected = String(receiptHashes[name] ?? "");
  const observed = sha256(readFileSync(path));
  if (!expected || expected !== observed) fail(`frozen manifest digest mismatch: ${name}`);
}

const sourceIds = sourceRows.map((row) => String(row.mapping_id));
if (new Set(sourceIds).size !== sourceIds.length) fail("source manifest repeats mapping_id");
const sourceByRepo = new Map<string, Json[]>();
const acceptedOwners = new Map<string, Set<string>>();
const sourceLineOwners = new Map<string, string>();
const sourceExecutableLines = new Set<string>();
for (const row of sourceRows) {
  if (row.schema_version !== "3.0" || row.execution_id !== "E02" || row.record_type !== "source_range") {
    fail(`invalid source record ${String(row.mapping_id)}`);
  }
  const id = String(row.mapping_id);
  for (const field of ["mapping_id", "source_repo", "source_snapshot", "source_path", "source_sha256", "source_symbol", "source_role", "migration_mode", "semantic_domain"]) {
    if (!nonEmptyString(row, field)) fail(`source record ${id} lacks ${field}`);
  }
  if (row.accepted !== true) fail(`E02 source record ${id} is not accepted`);
  const start = Number(row.start_line);
  const end = Number(row.end_line);
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start) {
    fail(`source record ${id} has an invalid range`);
  }
  const semanticDomain = String(row.semantic_domain);
  const role = String(row.source_role);
  const repo = String(row.source_repo);
  if (role === "primary") {
    const owners = acceptedOwners.get(semanticDomain) ?? new Set<string>();
    owners.add(repo);
    acceptedOwners.set(semanticDomain, owners);
  } else if (role === "supplementary" && !nonEmptyString(row, "supplementary_gap")) {
    fail(`supplementary source record ${id} lacks supplementary_gap`);
  }
  sourceByRepo.set(repo, [...(sourceByRepo.get(repo) ?? []), row]);
}
for (const [domain, owners] of acceptedOwners) {
  if (owners.size !== 1) fail(`semantic domain ${domain} has ${owners.size} primary source repositories`);
}
const sourceBlobCache = new Map<string, Buffer>();
for (const [repo, rows] of sourceByRepo) {
  const sourceRepo = join(workspaceRoot, repo);
  for (const row of rows) {
    const key = `${String(row.source_snapshot)}:${String(row.source_path)}`;
    try {
      const cacheKey = `${repo}:${key}`;
      let bytes = sourceBlobCache.get(cacheKey);
      if (!bytes) {
        bytes = gitBytes(sourceRepo, ["show", key]);
        sourceBlobCache.set(cacheKey, bytes);
      }
      if (sha256(bytes) !== String(row.source_sha256)) fail(`source blob digest drifted: ${repo}:${String(row.source_path)}`);
      const lines = bytes.toString("utf8").replaceAll("\r", "").split("\n");
      if (lines.at(-1) === "") lines.pop();
      const start = Number(row.start_line);
      const end = Number(row.end_line);
      if (end > lines.length) fail(`source range exceeds blob: ${String(row.mapping_id)}`);
      for (let line = start; line <= Math.min(end, lines.length); line += 1) {
        const lineKey = `${repo}:${key}:${line}`;
        const prior = sourceLineOwners.get(lineKey);
        if (prior) fail(`accepted source ranges overlap: ${prior} and ${String(row.mapping_id)} at ${lineKey}`);
        sourceLineOwners.set(lineKey, String(row.mapping_id));
        if (executableLine(lines[line - 1] ?? "")) sourceExecutableLines.add(lineKey);
      }
    } catch (error) {
      fail(`source blob unavailable: ${repo}:${String(row.source_path)} (${error instanceof Error ? error.message : String(error)})`);
    }
  }
}
const claudeRows = sourceByRepo.get("claude-code-best") ?? [];
const sourceCredit = sourceExecutableLines.size;
const claudeCredit = [...sourceExecutableLines].filter((key) => key.startsWith("claude-code-best:")).length;
if (sourceCredit < Number(thresholds.accepted_source_executable_sloc)) fail(`accepted source executable credit ${sourceCredit} < ${String(thresholds.accepted_source_executable_sloc)}`);
if (claudeCredit < Number(thresholds.claude_primary_source_executable_sloc)) fail(`Claude primary source credit ${claudeCredit} < ${String(thresholds.claude_primary_source_executable_sloc)}`);
if (new Set(claudeRows.map((row) => String(row.source_path))).size !== Number(thresholds.claude_primary_source_files)) {
  fail("Claude primary source file count drifted");
}
if (claudeRows.length !== Number(thresholds.claude_primary_source_ranges)) fail("Claude primary source range count drifted");

const pythonByDisposition = new Map<string, Json[]>();
const pythonBlobCache = new Map<string, Buffer>();
const pythonLineOwners = new Map<string, string>();
const pythonExecutableByDisposition = new Map<string, Set<string>>();
for (const row of pythonRows) {
  const ownerId = String(row.owner_id);
  if (row.schema_version !== "3.0" || row.execution_id !== "E02" || row.record_type !== "python_owner") {
    fail(`invalid Python owner record ${ownerId}`);
  }
  for (const field of [
    "owner_id",
    "verified_zyra_head",
    "python_path",
    "python_symbol",
    "python_sha256",
    "state_domain",
    "disposition",
    "default_entry",
  ]) {
    if (!nonEmptyString(row, field)) fail(`Python owner ${ownerId} lacks ${field}`);
  }
  const disposition = String(row.disposition);
  if (!new Set(["delete", "retain", "blocked"]).has(disposition)) {
    fail(`Python owner ${ownerId} has invalid disposition ${disposition}`);
  }
  if (disposition === "delete" && !nonEmptyString(row, "deletion_test_id")) {
    fail(`Python delete owner ${ownerId} lacks deletion_test_id`);
  }
  if (disposition === "retain") {
    if (!nonEmptyStringArray(row, "allowed_adapter_symbols").length) {
      fail(`Python retained owner ${ownerId} lacks bounded adapter symbols`);
    }
    if (row.call_direction !== "typescript-to-python-port-only") {
      fail(`Python retained owner ${ownerId} has an invalid call direction`);
    }
  }
  if (disposition === "blocked" && !nonEmptyString(row, "blocked_owner")) {
    fail(`Python blocked owner ${ownerId} lacks blocked_owner`);
  }
  pythonByDisposition.set(disposition, [...(pythonByDisposition.get(disposition) ?? []), row]);
  if (String(row.verified_zyra_head) !== baseline) fail(`Python owner baseline mismatch: ${ownerId}`);
  try {
    const path = String(row.python_path);
    let bytes = pythonBlobCache.get(path);
    if (!bytes) {
      bytes = gitBytes(repoRoot, ["show", `${baseline}:${path}`]);
      pythonBlobCache.set(path, bytes);
    }
    if (sha256(bytes) !== String(row.python_sha256)) fail(`Python owner blob digest drifted: ${String(row.python_path)}`);
    const lines = bytes.toString("utf8").replaceAll("\r", "").split("\n");
    if (lines.at(-1) === "") lines.pop();
    const start = Number(row.start_line);
    const end = Number(row.end_line);
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start || end > lines.length) {
      fail(`Python owner ${ownerId} has invalid range ${start}-${end}`);
    } else {
      const executable = pythonExecutableByDisposition.get(disposition) ?? new Set<string>();
      for (let line = start; line <= end; line += 1) {
        const lineKey = `${path}:${line}`;
        const prior = pythonLineOwners.get(lineKey);
        if (prior) fail(`Python owner ranges overlap: ${prior} and ${ownerId} at ${lineKey}`);
        pythonLineOwners.set(lineKey, ownerId);
        if (pythonExecutableLine(lines[line - 1] ?? "")) executable.add(lineKey);
      }
      pythonExecutableByDisposition.set(disposition, executable);
    }
  } catch {
    fail(`Python owner baseline path unavailable: ${String(row.python_path)}`);
  }
}
const dispositionThresholds: Record<string, number> = {
  delete: Number(thresholds.python_delete_executable_sloc),
  retain: Number(thresholds.python_retain_executable_sloc),
  blocked: Number(thresholds.python_blocked_executable_sloc),
};
for (const [disposition, expected] of Object.entries(dispositionThresholds)) {
  const lines = pythonExecutableByDisposition.get(disposition)?.size ?? 0;
  if (lines !== expected) fail(`Python ${disposition} frozen range total ${lines} != ${expected}`);
}
const deletedPaths = new Set((pythonByDisposition.get("delete") ?? []).map((row) => String(row.python_path)));
for (const path of deletedPaths) if (candidateExists(path)) fail(`frozen Python owner remains present: ${path}`);
for (const disposition of ["retain", "blocked"]) {
  for (const path of new Set((pythonByDisposition.get(disposition) ?? []).map((row) => String(row.python_path)))) {
    if (!candidateExists(path)) fail(`frozen Python ${disposition} path was removed: ${path}`);
  }
}

const targetIds = targetRows.map((row) => String(row.mapping_id));
if (targetRows.length !== sourceRows.length || new Set(targetIds).size !== targetRows.length) {
  fail("target custody map is not a one-to-one mapping corpus");
}
for (const id of sourceIds) if (!targetIds.includes(id)) fail(`accepted source mapping lacks custody mapping: ${id}`);
const sourceById = new Map(sourceRows.map((row) => [String(row.mapping_id), row]));
const frozenMutationIds = new Set(mutationRows.map((row) => String(row.mutation_id)));
const frozenMutationById = new Map(mutationRows.map((row) => [String(row.mutation_id), row]));
const mappingsPerSymbol = new Map<string, number>();
const targetsByBaseSource = new Map<string, Set<string>>();
const ownersByDomain = new Map<string, Set<string>>();
const behaviorContracts = new Set<string>();
const testIds = new Set<string>();
for (const path of candidatePaths().filter((path) => testTypeScript(path, behaviorRoots))) {
  for (const unit of testCaseUnits(path, candidateText(path))) testIds.add(unit.title);
}
for (const row of targetRows) {
  const id = String(row.mapping_id);
  if (row.schema_version !== "3.0" || row.execution_id !== "E02" || row.record_type !== "custody_mapping") {
    fail(`invalid custody mapping record ${id}`);
  }
  const targetPath = String(row.target_path);
  const targetSymbol = String(row.target_symbol);
  const source = sourceById.get(id);
  if (!source) fail(`custody mapping ${id} has no source record`);
  const semanticDomain = String(source?.semantic_domain ?? "");
  const canonicalOwner = String(row.canonical_owner_id ?? "");
  const domainOwners = ownersByDomain.get(semanticDomain) ?? new Set<string>();
  if (canonicalOwner) domainOwners.add(canonicalOwner);
  ownersByDomain.set(semanticDomain, domainOwners);
  const key = `${targetPath}::${targetSymbol}`;
  mappingsPerSymbol.set(key, (mappingsPerSymbol.get(key) ?? 0) + 1);
  const baseSource = String(row.source_symbol ?? "").replace(/#\d+\/\d+$/, "");
  const sourceTargets = targetsByBaseSource.get(baseSource) ?? new Set<string>();
  sourceTargets.add(key);
  targetsByBaseSource.set(baseSource, sourceTargets);
  if (!candidateExists(targetPath)) fail(`custody target is absent: ${id}:${targetPath}`);
  else {
    if (!declarationExists(targetPath, targetSymbol)) fail(`custody target symbol is absent: ${id}:${targetSymbol}`);
    const expectedTargetSha = sha256(candidateText(targetPath));
    if (String(row.target_sha256 ?? "") !== expectedTargetSha) fail(`custody target digest drifted: ${id}:${targetPath}`);
  }
  for (const field of [
    "source_behavior_claim",
    "target_behavior_claim",
    "adaptation",
    "semantic_equivalence",
    "state_store",
    "state_effect_kind",
    "canonical_owner_id",
    "default_entry_id",
    "default_callsite_path",
    "default_callsite_symbol",
    "state_effect_assertion",
    "runtime_origin_probe_id",
    "write_path_probe_id",
    "mapping_basis",
  ]) {
    if (!String(row[field] ?? "").trim()) fail(`custody mapping ${id} lacks ${field}`);
  }
  if (!new Set(["state", "event", "artifact", "permission", "tool", "checkpoint", "external_effect"]).has(String(row.state_effect_kind))) {
    fail(`custody mapping ${id} has invalid state_effect_kind`);
  }
  const callsitePath = String(row.default_callsite_path ?? "");
  const callsiteSymbol = String(row.default_callsite_symbol ?? "");
  if (!candidateExists(callsitePath) || !declarationExists(callsitePath, callsiteSymbol)) {
    fail(`custody mapping ${id} default callsite is not parseable`);
  }
  const contractId = String(row.behavior_contract_id ?? "");
  if (!contractId || behaviorContracts.has(contractId)) fail(`custody mapping ${id} lacks a unique behavior contract`);
  behaviorContracts.add(contractId);
  for (const field of ["success_test_ids", "failure_test_ids", "disable_test_ids", "mutation_ids"]) {
    const ids = nonEmptyStringArray(row, field);
    if (!ids.length) fail(`custody mapping ${id} lacks ${field}`);
    for (const referenced of ids) {
      if (field === "mutation_ids") {
        if (!frozenMutationIds.has(referenced)) {
          fail(`custody mapping ${id} references unknown mutation ${referenced}`);
        } else {
          const mutation = frozenMutationById.get(referenced)!;
          if (String(mutation.target_path) !== targetPath || String(mutation.target_symbol) !== targetSymbol) {
            fail(`custody mapping ${id} mutation ${referenced} disconnects another target`);
          }
        }
      } else if (!testIds.has(referenced)) {
        fail(`custody mapping ${id} references unknown behavior test ${referenced}`);
      }
    }
  }
  const assertionId = String(row.state_effect_assertion ?? "");
  if (!testIds.has(assertionId)) fail(`custody mapping ${id} state assertion is not executable: ${assertionId}`);
  if (!String(row.restore_probe_id ?? "").trim()) fail(`custody mapping ${id} lacks restore_probe_id`);
}
for (const [sourceSymbol, targets] of targetsByBaseSource) {
  if (targets.size > 2) fail(`source symbol ${sourceSymbol} is mechanically sprayed across ${targets.size} targets`);
}
for (const [domain, owners] of ownersByDomain) {
  if (owners.size !== 1) fail(`semantic domain ${domain} has ${owners.size} canonical target owners`);
}
const uniqueTargetSymbols = mappingsPerSymbol.size;
const maxMappings = Math.max(...mappingsPerSymbol.values());
if (uniqueTargetSymbols < Number(thresholds.source_to_target_unique_symbols_minimum)) {
  fail(`unique custody target symbols ${uniqueTargetSymbols} < ${String(thresholds.source_to_target_unique_symbols_minimum)}`);
}
if (maxMappings > Number(thresholds.source_to_target_max_mappings_per_symbol)) {
  fail(`one target symbol consolidates ${maxMappings} mappings, above the frozen maximum`);
}
const declaredBehaviorIds = new Set(targetRows.flatMap((row) => [
  ...((row.success_test_ids as unknown[] | undefined) ?? []).map(String),
  ...((row.failure_test_ids as unknown[] | undefined) ?? []).map(String),
  ...((row.disable_test_ids as unknown[] | undefined) ?? []).map(String),
]));
const uniqueStateAssertions = new Set(targetRows.map((row) => String(row.state_effect_assertion)));
if (uniqueStateAssertions.size < uniqueTargetSymbols) {
  fail(`target-specific state assertions ${uniqueStateAssertions.size} < unique custody targets ${uniqueTargetSymbols}`);
}
for (const id of declaredBehaviorIds) if (!testIds.has(id)) fail(`frozen custody behavior test id is not executable: ${id}`);

const allCandidatePaths = candidatePaths();
const productionPaths = allCandidatePaths.filter((path) => productionTypeScript(path, productionRoots, excludedRoots));
const finalProduction = productionPaths.reduce(
  (sum, path) => sum + candidateText(path).split(/\r?\n/).filter(executableLine).length,
  0,
);
const baselinePaths = new Set(gitText(repoRoot, ["ls-tree", "-r", "--name-only", baseline]).split(/\r?\n/).filter(Boolean));
const untracked = args.worktree
  ? new Set(gitText(repoRoot, ["ls-files", "--others", "--exclude-standard"]).split(/\r?\n/).filter(Boolean))
  : new Set<string>();
const changedProductionPaths = productionPaths.filter((path) => {
  if (!baselinePaths.has(path)) return true;
  const command = args.worktree
    ? ["diff", "--quiet", baseline, "--", path]
    : ["diff", "--quiet", baseline, args.candidate, "--", path];
  try {
    gitText(repoRoot, command);
    return false;
  } catch {
    return true;
  }
});
const changedFiles = changedProductionPaths.map((path): ChangedFile => {
  const text = candidateText(path);
  return {
    path,
    text,
    lines: text.split(/\r?\n/),
    changedLines: changedLineNumbers(baseline, path, untracked.has(path) || !baselinePaths.has(path)),
  };
});
let grossChangedProduction = 0;
for (const file of changedFiles) {
  for (const line of file.changedLines) if (executableLine(file.lines[line - 1] ?? "")) grossChangedProduction += 1;
}
const productionUnits = changedFiles.flatMap(changedProductionUnits);
const productionDuplicates = duplicateUnits(productionUnits);
const effectiveChangedProduction = grossChangedProduction - productionDuplicates.deduction;

const testPaths = allCandidatePaths.filter((path) => testTypeScript(path, behaviorRoots));
const testUnits = testPaths.flatMap((path) => testCaseUnits(path, candidateText(path)));
const testDuplicates = duplicateUnits(testUnits);
const grossBehaviorTests = testUnits.reduce((sum, unit) => sum + unit.executableLines, 0);
const effectiveBehaviorTests = grossBehaviorTests - testDuplicates.deduction;
const uniqueTestTitles = new Set(testUnits.map((unit) => unit.title));
const failureCases = testUnits.filter((unit) => unit.failureCase).length;

if (finalProduction < Number(thresholds.final_non_test_typescript_sloc)) {
  fail(`final non-test TypeScript ${finalProduction} < ${String(thresholds.final_non_test_typescript_sloc)}`);
}
if (effectiveChangedProduction < Number(thresholds.effective_changed_typescript_sloc)) {
  fail(`effective changed TypeScript ${effectiveChangedProduction} < ${String(thresholds.effective_changed_typescript_sloc)}`);
}
if (effectiveBehaviorTests < Number(thresholds.effective_behavior_test_sloc)) {
  fail(`effective behavioral test TypeScript ${effectiveBehaviorTests} < ${String(thresholds.effective_behavior_test_sloc)}`);
}
if (uniqueTestTitles.size < 120) fail(`unique explicit behavior cases ${uniqueTestTitles.size} < 120`);
if (failureCases < 45) fail(`explicit failure/crash cases ${failureCases} < 45`);

let adapterLines = 0;
const adapterBreakdown: Record<string, number> = {};
for (const root of adapterRoots) {
  const absolute = join(repoRoot, root);
  const count = pythonExecutableLines(absolute);
  adapterBreakdown[root] = count;
  adapterLines += count;
}
const adapterDenominator = effectiveChangedProduction + adapterLines;
const adapterRatio = adapterDenominator ? adapterLines / adapterDenominator : 1;
if (adapterLines > Number(thresholds.remaining_python_logical_adapter_sloc_maximum)) {
  fail(`remaining Python logical adapter SLOC ${adapterLines} > ${String(thresholds.remaining_python_logical_adapter_sloc_maximum)}`);
}
if (adapterRatio > Number(thresholds.adapter_ratio_maximum)) {
  fail(`Python adapter ratio ${adapterRatio.toFixed(4)} > ${String(thresholds.adapter_ratio_maximum)}`);
}

const forbiddenDependencies = (profile.forbidden_runtime_dependencies as unknown[]).map(String);
const dependencyFindings: string[] = [];
for (const path of [...productionPaths, ...adapterRoots.filter((root) => candidateExists(root))]) {
  const text = candidateText(path);
  for (const forbidden of forbiddenDependencies) {
    if (text.includes(forbidden)) dependencyFindings.push(`${path}:${forbidden}`);
  }
}
if (dependencyFindings.length) fail(`forbidden runtime dependencies found: ${dependencyFindings.slice(0, 10).join(", ")}`);

const deletedImportPatterns: Array<[string, RegExp]> = [
  ["ToolPermissionRuntime", /^\s*(?:from\s+[^\n]+\s+import[^\n]*\bToolPermissionRuntime\b|import\s+[^\n]*\bToolPermissionRuntime\b|class\s+ToolPermissionRuntime\b)/m],
  ["McpRuntime", /^\s*(?:from\s+[^\n]+\s+import[^\n]*\bMcpRuntime\b|import\s+[^\n]*\bMcpRuntime\b|class\s+McpRuntime\b)/m],
  ["SkillRuntime", /^\s*(?:from\s+[^\n]+\s+import[^\n]*\bSkillRuntime\b|import\s+[^\n]*\bSkillRuntime\b|class\s+SkillRuntime\b)/m],
  ["SkillToolProjectionRuntime", /^\s*(?:from\s+[^\n]+\s+import[^\n]*\bSkillToolProjectionRuntime\b|import\s+[^\n]*\bSkillToolProjectionRuntime\b|class\s+SkillToolProjectionRuntime\b)/m],
  ["SkillUpdateRuntime", /^\s*(?:from\s+[^\n]+\s+import[^\n]*\bSkillUpdateRuntime\b|import\s+[^\n]*\bSkillUpdateRuntime\b|class\s+SkillUpdateRuntime\b)/m],
  ["SkillUpdateControlRuntime", /^\s*(?:from\s+[^\n]+\s+import[^\n]*\bSkillUpdateControlRuntime\b|import\s+[^\n]*\bSkillUpdateControlRuntime\b|class\s+SkillUpdateControlRuntime\b)/m],
  ["PluginCapabilityIntegrationRuntime", /^\s*(?:from\s+[^\n]+\s+import[^\n]*\bPluginCapabilityIntegrationRuntime\b|import\s+[^\n]*\bPluginCapabilityIntegrationRuntime\b|class\s+PluginCapabilityIntegrationRuntime\b)/m],
  ["PermissionEvaluator", /^\s*(?:from\s+[^\n]+\s+import[^\n]*\bPermissionEvaluator\b|import\s+[^\n]*\bPermissionEvaluator\b|class\s+PermissionEvaluator\b)/m],
  ["zyra_runtime.permission.runtime", /(?:from|import)\s+zyra_runtime\.permission\.runtime\b/],
  ["zyra_integrations.mcp.runtime", /(?:from|import)\s+zyra_integrations\.mcp\.runtime\b/],
  ["zyra_skills.runtime", /(?:from|import)\s+zyra_skills\.runtime\b/],
];
const pythonFallbackFindings: string[] = [];
for (const path of allCandidatePaths.filter((path) => path.endsWith(".py")
  && (path.startsWith("apps/") || path.startsWith("packages/"))
  && !/(?:source_audit|vendor_manifest|ledger|inventory)\.py$/.test(path))) {
  const text = candidateText(path);
  for (const [label, pattern] of deletedImportPatterns) {
    if (pattern.test(text)) pythonFallbackFindings.push(`${path}:${label}`);
  }
}
if (pythonFallbackFindings.length) fail(`deleted Python logical owner references remain: ${pythonFallbackFindings.slice(0, 10).join(", ")}`);

const defaultPath = String((profile.default_entry as Json).path);
const defaultText = candidateText(defaultPath);
for (const marker of ["E02CapabilityCoordinator", "DEFAULT_CAPABILITY_ENTRYPOINT", "runTaskRuntime", "--e02-api"]) {
  if (!defaultText.includes(marker)) fail(`default entry lacks ${marker}`);
}
const stdioText = candidateText("packages/runtime/claude-runtime/src/stdio.ts");
for (const marker of ["TypeScriptCapabilityRuntime.open", "PermissionedCapabilityHost", "default_capability_entrypoint", "typescript_state_journal_owner"]) {
  if (!stdioText.includes(marker)) fail(`default stdio path lacks ${marker}`);
}

if (mutationRows.length < Number(thresholds.mutation_points_minimum)) {
  fail(`frozen mutation count ${mutationRows.length} < ${String(thresholds.mutation_points_minimum)}`);
}
const mutationIds = new Set<string>();
for (const row of mutationRows) {
  const id = String(row.mutation_id);
  if (mutationIds.has(id)) fail(`duplicate frozen mutation ${id}`);
  mutationIds.add(id);
  if (row.compile_survives !== true || !Array.isArray(row.expected_killer_test_ids) || !(row.expected_killer_test_ids as unknown[]).length) {
    fail(`invalid frozen mutation ${id}`);
  }
  for (const testId of (row.expected_killer_test_ids as unknown[]).map(String)) {
    if (!testIds.has(testId)) fail(`mutation killer test is absent: ${id}:${testId}`);
  }
  const patch = row.frozen_patch as Json;
  if (sha256(JSON.stringify(patch)) !== String(row.frozen_patch_sha256)) fail(`mutation patch digest drifted: ${id}`);
}

let mutationSummary: Json | null = null;
if (!args.skipMutationEvidence) {
  if (!existsSync(mutationEvidencePath)) {
    fail("mutation evidence is absent");
  } else {
    const evidence = readJson(mutationEvidencePath);
    mutationSummary = evidence.summary as Json;
    const results = Array.isArray(evidence.results) ? evidence.results as Json[] : [];
    if (results.length !== mutationRows.length) fail("mutation evidence does not cover the frozen corpus");
    for (const result of results) {
      if (result.killed !== true || result.compile_survived !== true || result.original_sha256 !== result.restored_sha256) {
        fail(`mutation was not killed with compilable/restored source: ${String(result.mutation_id)}`);
      }
      if (result.frozen_patch_sha256 !== result.observed_frozen_patch_sha256) {
        fail(`mutation evidence patch mismatch: ${String(result.mutation_id)}`);
      }
    }
    const family = mutationSummary?.family as Json | undefined;
    for (const name of ["permission", "mcp", "skill"]) {
      const result = family?.[name] as Json | undefined;
      const minimum = name === "skill"
        ? Number(thresholds.other_mutation_kill_ratio_minimum)
        : Number(thresholds.core_mutation_kill_ratio_minimum);
      if (Number(result?.kill_rate ?? 0) < minimum) fail(`mutation family ${name} is below kill threshold`);
    }
  }
}

if (!existsSync(cleanroomEvidencePath)) {
  fail("cleanroom evidence is absent");
} else {
  const cleanroom = readJson(cleanroomEvidencePath);
  const expectedCandidate = args.worktree ? "WORKTREE" : gitText(repoRoot, ["rev-parse", args.candidate]);
  if (cleanroom.ok !== true || (!args.worktree && String(cleanroom.candidate) !== expectedCandidate)) {
    fail("cleanroom evidence does not bind the exact successful candidate");
  }
  const commands = Array.isArray(cleanroom.commands) ? cleanroom.commands as Json[] : [];
  const liveProbe = commands.find((command) => Array.isArray(command.command)
    && (command.command as unknown[]).map(String).includes("scripts/remediation/probe_m1_r01_e02.ts")
    && (command.command as unknown[]).map(String).includes("all"));
  if (Number(liveProbe?.exit_code ?? -1) !== 0) {
    fail("cleanroom did not execute the built-only E02 live probe suite");
  }
  const built = commands.find((command) => Array.isArray(command.command)
    && (command.command as unknown[]).map(String).includes("runtime:built:health"));
  const builtOutput = String(built?.output_tail ?? "");
  const builtProjections = builtOutput.split(/\r?\n/).flatMap((line) => {
    try {
      const value = JSON.parse(line.trim()) as Json;
      return value.e02CapabilityRuntime ? [value] : [];
    } catch {
      return [];
    }
  });
  if (Number(built?.exit_code ?? -1) !== 0 || builtProjections.length < 2) {
    fail("cleanroom built-entry evidence did not return Bun and Node E02 readiness projections");
  }
  for (const projection of builtProjections) {
    const capability = projection.e02CapabilityRuntime as Json;
    if (
      projection.defaultCapabilityEntrypoint !== "E02CapabilityCoordinator.execute"
      || projection.stateJournalOwner !== "E02CapabilityCoordinator"
      || capability.schema !== "zyra.e02-built-readiness/v1"
      || capability.implementationReady !== true
      || capability.reviewStatus !== "implementation_complete_review_pending"
      || capability.independentReviewPassed !== false
      || String(capability.implementationCandidate) !== expectedCandidate
      || capability.sourceImportProbeAccepted !== false
      || capability.liveBuiltProbeRequired !== true
      || capability.pythonDecisionFallback !== false
    ) {
      fail("cleanroom built-entry E02 readiness projection is incomplete or bound to another candidate");
    }
  }
}

let resumeRestoreEvidence = false;
for (const name of requiredProbeEvidence) {
  const path = join(repoRoot, "docs/reviews/evidence/M1-R01-v3/execution-02", name);
  if (!existsSync(path)) {
    fail(`required E02 probe evidence is absent: ${name}`);
    continue;
  }
  const probe = readJson(path);
  if (probe.ok !== true || probe.execution_id !== "E02") fail(`required E02 probe failed: ${name}`);
  const expectedProbeCandidate = args.worktree ? String(profile.candidate_head_at_g0 ?? "") : gitText(repoRoot, ["rev-parse", args.candidate]);
  if (String(probe.implementation_candidate ?? "") !== expectedProbeCandidate) {
    fail(`required E02 probe is bound to another implementation candidate: ${name}`);
  }
  if (
    String(probe.reviewer_nonce ?? "").length < 24
    || probe.source_imports_used !== false
    || probe.built_default_entry_executed !== true
    || probe.built_entry !== "dist/code-worker-node/main.js --e02-api"
    || !/^[0-9a-f]{64}$/.test(String(probe.built_entry_sha256 ?? ""))
  ) {
    fail(`required E02 probe lacks reviewer nonce or built-only runtime provenance: ${name}`);
  }
  if (name === "runtime-origin-result.json") {
    if (
      Number(probe.process_id) <= 0
      || !String(probe.default_entry ?? "").startsWith("node dist/code-worker-node/main.js --e02-api")
      || probe.canonical_owner !== "typescript-e02-control"
      || probe.python_decision_fallback !== false
    ) {
      fail("runtime-origin probe did not execute the built default TypeScript owner");
    }
  }
  if (name === "write-path-result.json") {
    if (probe.persisted_state_exists !== true || Number(probe.state_generation) < 1 || !String(probe.state_store).startsWith("E02ApiStateStore")) {
      fail("write-path probe did not persist the built E02 API state owner");
    }
  }
  if (name === "same-session-resume-result.json") {
    if (Number(probe.process_restart_count) < 2 || Number(probe.repeated_effect_count) !== 0) {
      fail("same-session resume probe lacks two process restarts or repeated-effect fencing");
    }
    const processIds = Array.isArray(probe.process_ids) ? probe.process_ids as unknown[] : [];
    if (new Set(processIds.map(String)).size < 3) fail("same-session resume probe reused an owner process");
    const restored = Array.isArray(probe.restored_before_bootstrap) ? probe.restored_before_bootstrap : [];
    const epochs = Array.isArray(probe.runtime_epochs) ? probe.runtime_epochs : [];
    const replayed = Array.isArray(probe.stable_replayed) ? probe.stable_replayed : [];
    resumeRestoreEvidence = JSON.stringify(restored) === JSON.stringify([false, true, true]);
    if (
      !resumeRestoreEvidence
      || JSON.stringify(epochs) !== JSON.stringify([1, 2, 3])
      || JSON.stringify(replayed) !== JSON.stringify([false, true, true])
      || !String(probe.stable_execution_id ?? "")
    ) {
      fail("same-session resume probe lacks exact built restore/replay evidence");
    }
  }
  if (name === "lost-ack-result.json") {
    const processIds = Array.isArray(probe.process_ids) ? probe.process_ids as unknown[] : [];
    if (
      new Set(processIds.map(String)).size < 2
      || Number(probe.external_effect_count) !== 1
      || Number(probe.repeated_effect_count) !== 0
      || probe.restored_before_bootstrap !== true
      || probe.restored_execution_phase !== "recovery_required"
      || probe.restored_transition_phase !== "effect_started"
      || probe.recovery_kind !== "indeterminate_restart_effect"
      || probe.reexecute_without_receipt !== false
      || probe.retry_before_reconcile_error !== "e02_execution_recovery_required"
      || probe.final_execution_phase !== "committed"
      || probe.replayed_after_reconciliation !== true
    ) {
      fail("lost-ACK probe lacks real external single-effect fencing and reconciliation");
    }
  }
  if (name === "disable-result.json" && (
    probe.default_owner_failed !== true
    || probe.python_fallback_observed !== false
    || probe.ready_frame_observed !== false
    || probe.error_code !== "e02_typescript_runtime_disabled"
    || Number(probe.process_id) <= 0
  )) {
    fail("disable probe did not fail closed without Python fallback");
  }
}

checks.baseline = {
  verified_head: baseline,
  candidate: args.worktree ? "WORKTREE" : gitText(repoRoot, ["rev-parse", args.candidate]),
  dirty_paths: dirty.map((line) => line.slice(3).replaceAll("\\", "/")),
  frozen_manifest_hashes: "verified",
};
checks.source_custody = {
  accepted_rows: sourceRows.length,
  physical_selected_lines: sourceLineOwners.size,
  credited_executable_lines: sourceCredit,
  claude_primary_rows: claudeRows.length,
  claude_primary_files: new Set(claudeRows.map((row) => String(row.source_path))).size,
  claude_primary_physical_lines: [...sourceLineOwners].filter(([key]) => key.startsWith("claude-code-best:")).length,
  claude_primary_credited_lines: claudeCredit,
  target_rows: targetRows.length,
  unique_target_symbols: uniqueTargetSymbols,
  maximum_mappings_per_symbol: maxMappings,
};
checks.python_cutover = {
  delete_paths: deletedPaths.size,
  delete_executable_lines: dispositionThresholds.delete,
  retain_executable_lines: dispositionThresholds.retain,
  blocked_executable_lines: dispositionThresholds.blocked,
  remaining_adapter_lines: adapterLines,
  adapter_breakdown: adapterBreakdown,
  adapter_ratio: Number(adapterRatio.toFixed(6)),
  fallback_findings: pythonFallbackFindings,
};
checks.effective_lines = {
  final_non_test_typescript: finalProduction,
  gross_changed_executable_typescript: grossChangedProduction,
  production_clone_deduction: productionDuplicates.deduction,
  production_clone_clusters: productionDuplicates.pairs,
  effective_changed_typescript: effectiveChangedProduction,
  explicit_behavior_cases: testUnits.length,
  unique_behavior_titles: uniqueTestTitles.size,
  explicit_failure_cases: failureCases,
  gross_explicit_behavior_test_lines: grossBehaviorTests,
  test_clone_deduction: testDuplicates.deduction,
  test_clone_clusters: testDuplicates.pairs,
  effective_behavior_test_lines: effectiveBehaviorTests,
  excluded_generated_matrix_files: testPaths.filter((path) => /matrix\.test\.ts$/.test(path)),
};
checks.runtime_custody = {
  default_entry: defaultPath,
  default_symbol: String((profile.default_entry as Json).e02_symbol),
  state_journal_owner: "E02CapabilityCoordinator",
  restore_before_bootstrap: resumeRestoreEvidence,
  forbidden_dependency_findings: dependencyFindings,
};
checks.mutations = {
  declared: mutationRows.length,
  evidence: mutationSummary,
};

const report = {
  schema_version: "3.0",
  verification_contract_version: String(profile.verification_contract_version),
  execution_id: "E02",
  verifier: "scripts/remediation/verify_m1_r01_e02.ts",
  generated_at_utc: new Date().toISOString(),
  ok: failures.length === 0,
  checks,
  failures,
};
const rendered = `${JSON.stringify(report, null, 2)}\n`;
if (args.output) {
  const output = resolve(repoRoot, args.output);
  mkdirSync(dirname(output), { recursive: true });
  writeFileSync(output, rendered, "utf8");
}
process.stdout.write(rendered);
if (failures.length) process.exitCode = 1;
