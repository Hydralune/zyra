import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";

import {
  mutationPatchFingerprint,
  mutationSpecForRecord,
  mutationSpecs,
} from "./run_m1_r01_e01_mutations.ts";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const workspaceRoot = resolve(repoRoot, "..");
const sourceRoot = join(workspaceRoot, "claude-code-best");
const manifestRoot = join(
  workspaceRoot,
  "docs/remediations/M1-R01-claude-source-custody/manifests",
);
const sourceManifestPath = join(manifestRoot, "execution-01-source-manifest.jsonl");
const targetManifestPath = join(manifestRoot, "execution-01-target-custody-map.jsonl");
const mutationManifestPath = join(manifestRoot, "execution-01-mutation-manifest.jsonl");
const gateProfilePath = join(manifestRoot, "execution-01-gate-profile.json");
const receiptPath = join(manifestRoot, "execution-01-baseline-receipt.json");
const metadataPath = join(
  repoRoot,
  "docs/reviews/evidence/M1-R01-v3/execution-01/candidate-metadata.json",
);
const mutationEvidencePath = "docs/reviews/evidence/M1-R01-v3/execution-01/mutation-results.json";
const fiveHopEvidencePath = join(
  repoRoot,
  "docs/reviews/evidence/M1-R01-v3/execution-01/source-to-target-five-hop.jsonl",
);

const SOURCE_SNAPSHOT = "c57f5a29e88e9a814bea47abeb9a0a6f725dc102";
const VERIFIED_BASELINE = "c34535a783e88f9481387ced89cba4fbc333dc74";
const IMPLEMENTATION_DIFF_BASELINE = "0cd21bff5e2d160476f2ce3cef766bf53aab1239";
const DEFAULT_ENTRY_ID = "e01.default-code-worker";
const VERIFICATION_CONTRACT_VERSION = "zyra.e01-verification/v6";
const AUTHORIZED_POST_CUTOFF_SYMBOLS = new Set([
  "getDefaultMaxRetries",
  "getMaxRetries",
  "getRetryAfterMs",
  "getRateLimitResetDelayMs",
  "categorizeRetryableAPIError",
  "getErrorMessageIfRefusal",
]);

type JsonRecord = Record<string, unknown>;

interface Arguments {
  candidate?: string;
  output?: string;
  requireMetadata: boolean;
}

interface ChangedFile {
  path: string;
  changedLines: Set<number>;
  text: string;
  lines: string[];
}

interface TokenUnit {
  id: string;
  path: string;
  startLine: number;
  endLine: number;
  changedExecutableLines: number;
  tokens: string[];
  fingerprints: Set<string>;
}

interface DuplicatePair {
  retainedUnit: string;
  deductedUnit: string;
  similarity: number;
  lengthRatio: number;
  deductedLines: number;
}

const args = (() => {
  const value: Arguments = { requireMetadata: false };
  for (let index = 2; index < process.argv.length; index += 1) {
    const argument = process.argv[index];
    if (argument === "--candidate") value.candidate = process.argv[++index];
    else if (argument === "--output") value.output = process.argv[++index];
    else if (argument === "--require-metadata") value.requireMetadata = true;
    else throw new Error(`unknown argument ${argument}`);
  }
  return value;
})();

const gitText = (cwd: string, command: readonly string[], trim = true): string => {
  const output = execFileSync("git", [...command], {
    cwd,
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
  });
  return trim ? output.trim() : output;
};

const gitBytes = (cwd: string, command: readonly string[]): Buffer =>
  execFileSync("git", [...command], {
    cwd,
    encoding: "buffer",
    maxBuffer: 256 * 1024 * 1024,
  });

const sha256 = (value: Uint8Array | string): string =>
  createHash("sha256").update(value).digest("hex");

const readJson = (path: string): JsonRecord => JSON.parse(readFileSync(path, "utf8")) as JsonRecord;

const readJsonLines = (path: string): JsonRecord[] =>
  readFileSync(path, "utf8")
    .split(/\r?\n/)
    .filter((line) => line.trim().length > 0)
    .map((line) => JSON.parse(line) as JsonRecord);

const committedTextCache = new Map<string, string>();
const textAt = (commit: string, path: string): string => {
  const key = `${commit}:${path}`;
  const cached = committedTextCache.get(key);
  if (cached !== undefined) return cached;
  const text = gitBytes(repoRoot, ["show", key]).toString("utf8");
  committedTextCache.set(key, text);
  return text;
};

const sourceTextAt = (path: string): string =>
  gitBytes(sourceRoot, ["show", `${SOURCE_SNAPSHOT}:${path}`]).toString("utf8");

const symbolLeaf = (value: unknown): string => {
  const text = String(value ?? "");
  return text.slice(Math.max(text.lastIndexOf("."), text.lastIndexOf("::")) + 1);
};

interface DeclarationAnalysis {
  text: string;
  calledSymbols: Set<string>;
}

const declarationAnalysisCache = new Map<string, DeclarationAnalysis | null>();
const namedTestBodyCache = new Map<string, string | null>();

const parsedSource = (path: string, text: string): ts.SourceFile =>
  ts.createSourceFile(
    path,
    text,
    ts.ScriptTarget.Latest,
    true,
    path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );

const declaredName = (node: ts.Node, source: ts.SourceFile): string => {
  if ("name" in node && node.name && ts.isIdentifier(node.name as ts.Node)) {
    return (node.name as ts.Identifier).text;
  }
  if ("name" in node && node.name) return (node.name as ts.Node).getText(source).replace(/["']/g, "");
  return "";
};

const declarationAnalysis = (
  path: string,
  text: string,
  qualifiedSymbol: unknown,
): DeclarationAnalysis | null => {
  const cacheKey = `${path}::${String(qualifiedSymbol ?? "")}`;
  if (declarationAnalysisCache.has(cacheKey)) return declarationAnalysisCache.get(cacheKey) ?? null;
  const source = parsedSource(path, text);
  const symbol = String(qualifiedSymbol ?? "");
  const segments = symbol.split(".");
  const leaf = symbolLeaf(symbol);
  const owner = segments.length > 1 ? segments.at(-2) ?? "" : "";
  let exact: ts.Node | null = null;
  let fallback: ts.Node | null = null;
  const visit = (node: ts.Node): void => {
    const callable = ts.isFunctionDeclaration(node)
      || ts.isMethodDeclaration(node)
      || ts.isGetAccessorDeclaration(node)
      || ts.isSetAccessorDeclaration(node)
      || ts.isConstructorDeclaration(node);
    if (callable && declaredName(node, source) === leaf) {
      fallback ??= node;
      const parent = node.parent;
      if (!owner || (ts.isClassDeclaration(parent) && parent.name?.text === owner)) exact ??= node;
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  const declaration = exact ?? fallback;
  if (!declaration) {
    declarationAnalysisCache.set(cacheKey, null);
    return null;
  }
  const calledSymbols = new Set<string>();
  const collect = (node: ts.Node): void => {
    if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
      const expression = node.expression;
      if (ts.isIdentifier(expression)) calledSymbols.add(expression.text);
      else if (ts.isPropertyAccessExpression(expression)) calledSymbols.add(expression.name.text);
      else if (ts.isElementAccessExpression(expression) && ts.isStringLiteralLike(expression.argumentExpression)) {
        calledSymbols.add(expression.argumentExpression.text);
      }
    }
    ts.forEachChild(node, collect);
  };
  collect(declaration);
  const analysis = { text: declaration.getText(source), calledSymbols };
  declarationAnalysisCache.set(cacheKey, analysis);
  return analysis;
};

const namedTestBody = (path: string, text: string, testName: string): string | null => {
  const cacheKey = `${path}::${testName}`;
  if (namedTestBodyCache.has(cacheKey)) return namedTestBodyCache.get(cacheKey) ?? null;
  const source = parsedSource(path, text);
  let body: string | null = null;
  const visit = (node: ts.Node): void => {
    if (body || !ts.isCallExpression(node)) {
      if (!body) ts.forEachChild(node, visit);
      return;
    }
    const expression = node.expression;
    const callName = ts.isIdentifier(expression)
      ? expression.text
      : ts.isPropertyAccessExpression(expression)
        ? expression.name.text
        : "";
    const title = node.arguments[0];
    const callback = node.arguments[1];
    if (
      (callName === "test" || callName === "it")
      && title
      && ts.isStringLiteralLike(title)
      && title.text === testName
      && callback
      && (ts.isArrowFunction(callback) || ts.isFunctionExpression(callback))
    ) {
      body = callback.body.getText(source);
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  namedTestBodyCache.set(cacheKey, body);
  return body;
};

const executableLine = (line: string): boolean => {
  const value = line.trim();
  return Boolean(value) && !value.startsWith("//") && !value.startsWith("/*") && !value.startsWith("*");
};

const productionTypeScript = (path: string): boolean =>
  /\.tsx?$/.test(path) &&
  !/(^|\/)(test|tests|fixtures|generated|vendor|source-pool)(\/|$)/.test(path) &&
  !/\.test\.tsx?$/.test(path) &&
  !path.startsWith("scripts/") &&
  (path.startsWith("apps/code-worker/") || path.startsWith("packages/"));

const testTypeScript = (path: string): boolean =>
  /\.test\.tsx?$/.test(path) || /(^|\/)(test|tests)\//.test(path);

const changedLineNumbers = (baseline: string, candidate: string, path: string): Set<number> => {
  const diff = gitText(repoRoot, ["diff", "--unified=0", baseline, candidate, "--", path], false);
  const lines = new Set<number>();
  for (const raw of diff.split(/\r?\n/)) {
    const match = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/.exec(raw);
    if (!match) continue;
    const start = Number(match[1]);
    const length = match[2] === undefined ? 1 : Number(match[2]);
    for (let offset = 0; offset < length; offset += 1) lines.add(start + offset);
  }
  return lines;
};

const normalizeTokens = (text: string): string[] => {
  const scanner = ts.createScanner(ts.ScriptTarget.Latest, true, ts.LanguageVariant.Standard, text);
  const tokens: string[] = [];
  while (true) {
    const kind = scanner.scan();
    if (kind === ts.SyntaxKind.EndOfFileToken) break;
    const raw = scanner.getTokenText();
    if (
      kind === ts.SyntaxKind.StringLiteral ||
      kind === ts.SyntaxKind.NoSubstitutionTemplateLiteral ||
      kind === ts.SyntaxKind.NumericLiteral ||
      kind === ts.SyntaxKind.BigIntLiteral
    ) {
      tokens.push(ts.tokenToString(kind) ?? "literal");
    } else {
      tokens.push(raw);
    }
  }
  return tokens;
};

const shingleHash = (tokens: readonly string[]): string =>
  createHash("sha1").update(tokens.join("\u001f")).digest("hex").slice(0, 16);

const winnowFingerprints = (tokens: readonly string[]): Set<string> => {
  if (tokens.length < 5) return new Set();
  const shingles: string[] = [];
  for (let index = 0; index <= tokens.length - 5; index += 1) {
    shingles.push(shingleHash(tokens.slice(index, index + 5)));
  }
  if (shingles.length <= 8) return new Set(shingles);
  const fingerprints = new Set<string>();
  for (let start = 0; start <= shingles.length - 8; start += 1) {
    const window = shingles.slice(start, start + 8);
    let selected = window[0];
    for (const value of window) if (value <= selected) selected = value;
    fingerprints.add(selected);
  }
  return fingerprints;
};

const changedExecutableCount = (
  file: ChangedFile,
  startLine: number,
  endLine: number,
): number => {
  let count = 0;
  for (let line = startLine; line <= endLine; line += 1) {
    if (file.changedLines.has(line) && executableLine(file.lines[line - 1] ?? "")) count += 1;
  }
  return count;
};

const tokenUnits = (file: ChangedFile): TokenUnit[] => {
  const source = ts.createSourceFile(
    file.path,
    file.text,
    ts.ScriptTarget.Latest,
    true,
    file.path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const units: TokenUnit[] = [];
  const covered = new Set<number>();
  const add = (node: ts.Node, label: string): void => {
    const start = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
    const end = source.getLineAndCharacterOfPosition(node.end).line + 1;
    const changed = changedExecutableCount(file, start, end);
    if (changed === 0) return;
    for (let line = start; line <= end; line += 1) covered.add(line);
    const tokens = normalizeTokens(node.getText(source));
    units.push({
      id: `${file.path}:${start}:${label}`,
      path: file.path,
      startLine: start,
      endLine: end,
      changedExecutableLines: changed,
      tokens,
      fingerprints: winnowFingerprints(tokens),
    });
  };
  const visit = (node: ts.Node): void => {
    if (
      ts.isFunctionDeclaration(node) ||
      ts.isMethodDeclaration(node) ||
      ts.isConstructorDeclaration(node) ||
      ts.isGetAccessorDeclaration(node) ||
      ts.isSetAccessorDeclaration(node)
    ) {
      const name = "name" in node && node.name ? node.name.getText(source) : ts.SyntaxKind[node.kind];
      add(node, name);
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  let remainderStart: number | null = null;
  const flush = (end: number): void => {
    if (remainderStart === null) return;
    const start = remainderStart;
    const text = file.lines.slice(start - 1, end).join("\n");
    const changed = changedExecutableCount(file, start, end);
    const tokens = normalizeTokens(text);
    if (changed > 0 && tokens.length > 0) {
      units.push({
        id: `${file.path}:${start}:remainder`,
        path: file.path,
        startLine: start,
        endLine: end,
        changedExecutableLines: changed,
        tokens,
        fingerprints: winnowFingerprints(tokens),
      });
    }
    remainderStart = null;
  };
  for (let line = 1; line <= file.lines.length; line += 1) {
    if (covered.has(line) || !file.changedLines.has(line) || !executableLine(file.lines[line - 1] ?? "")) {
      flush(line - 1);
    } else if (remainderStart === null) {
      remainderStart = line;
    }
  }
  flush(file.lines.length);
  return units;
};

const jaccard = (left: ReadonlySet<string>, right: ReadonlySet<string>): number => {
  if (left.size === 0 || right.size === 0) return 0;
  let intersection = 0;
  for (const value of left) if (right.has(value)) intersection += 1;
  return intersection / (left.size + right.size - intersection);
};

const duplicateUnits = (units: readonly TokenUnit[]): { deduction: number; pairs: DuplicatePair[] } => {
  const eligible = units
    .filter((unit) => unit.tokens.length >= 20 && unit.changedExecutableLines >= 3)
    .sort((left, right) => left.id.localeCompare(right.id));
  const deducted = new Set<string>();
  const pairs: DuplicatePair[] = [];
  for (let rightIndex = 0; rightIndex < eligible.length; rightIndex += 1) {
    const right = eligible[rightIndex];
    if (deducted.has(right.id)) continue;
    let best: { unit: TokenUnit; similarity: number; ratio: number } | undefined;
    for (let leftIndex = 0; leftIndex < rightIndex; leftIndex += 1) {
      const left = eligible[leftIndex];
      if (left.path === right.path || deducted.has(left.id)) continue;
      const ratio = Math.min(left.tokens.length, right.tokens.length) / Math.max(left.tokens.length, right.tokens.length);
      if (ratio < 0.8) continue;
      const similarity = jaccard(left.fingerprints, right.fingerprints);
      if (similarity < 0.8) continue;
      if (!best || similarity > best.similarity) best = { unit: left, similarity, ratio };
    }
    if (best) {
      deducted.add(right.id);
      pairs.push({
        retainedUnit: best.unit.id,
        deductedUnit: right.id,
        similarity: Number(best.similarity.toFixed(4)),
        lengthRatio: Number(best.ratio.toFixed(4)),
        deductedLines: right.changedExecutableLines,
      });
    }
  }
  return { deduction: pairs.reduce((total, pair) => total + pair.deductedLines, 0), pairs };
};

const candidate = args.candidate ?? String(readJson(metadataPath).implementation_candidate ?? gitText(repoRoot, ["rev-parse", "HEAD"]));
const failures: string[] = [];
const checks: JsonRecord = {};
const profile = readJson(gateProfilePath);
const receipt = readJson(receiptPath);
const metadata = readJson(metadataPath);
const sourceRecords = readJsonLines(sourceManifestPath);
const targetRecords = readJsonLines(targetManifestPath);
const mutationRecords = readJsonLines(mutationManifestPath);

const fail = (message: string): void => {
  failures.push(message);
};

try {
  if (sha256(readFileSync(fiveHopEvidencePath)) !== sha256(readFileSync(targetManifestPath))) {
    fail("committed five-hop evidence bytes differ from the authoritative target custody map");
  }
} catch (error) {
  fail(`five-hop evidence is unavailable: ${error instanceof Error ? error.message : String(error)}`);
}

const dirtyPaths = Array.isArray(receipt.dirty_paths_at_capture)
  ? receipt.dirty_paths_at_capture.map(String)
  : [];
if (receipt.clean_worktree !== true || dirtyPaths.length !== 0) {
  fail(`baseline receipt is not clean: ${dirtyPaths.join(",")}`);
}
if (String(receipt.current_control_plane_head) !== candidate) {
  fail("baseline receipt capture head does not match candidate");
}
if (String(receipt.verified_zyra_head) !== VERIFIED_BASELINE) {
  fail("baseline receipt verified head drifted");
}
const sourceValidator = ((profile.commands as JsonRecord | undefined)?.source_validator as unknown[] | undefined)?.map(String) ?? [];
const effectiveValidator = ((profile.commands as JsonRecord | undefined)?.effective_loc_clone as unknown[] | undefined)?.map(String) ?? [];
if (!sourceValidator.includes("scripts/remediation/verify_m1_r01_e01_v4.ts")) {
  fail("gate profile source validator is not V4");
}
if (!effectiveValidator.includes("scripts/remediation/verify_m1_r01_e01_v4.ts")) {
  fail("gate profile effective-line validator is not V4");
}
if (!((receipt.schema_validator_command as unknown[] | undefined)?.map(String) ?? []).includes("scripts/remediation/verify_m1_r01_e01_v4.ts")) {
  fail("receipt schema validator is not V4");
}

const mutationRecordById = new Map(
  mutationRecords.map((record) => [String(record.mutation_id), record]),
);
for (const target of targetRecords) {
  const behaviorNames = new Set(
    (Array.isArray(target.behavior_tests) ? target.behavior_tests as JsonRecord[] : [])
      .map((item) => String(item.name))
      .filter(Boolean),
  );
  const exact = (Array.isArray(target.mutation_ids) ? target.mutation_ids.map(String) : [])
    .map((id) => mutationRecordById.get(id))
    .filter((record): record is JsonRecord => Boolean(record))
    .some((record) => {
      const killers = Array.isArray(record.expected_killer_test_ids)
        ? record.expected_killer_test_ids.map(String)
        : [];
      return String(record.target_path) === String(target.target_path)
        && String(record.target_symbol) === String(target.target_symbol)
        && killers.some((name) => behaviorNames.has(name));
    });
  if (!exact) fail(`mapping lacks exact target/test mutation: ${String(target.mapping_id)}`);
}

const mutationIdsFromManifest = new Set(mutationRecords.map((record) => String(record.mutation_id)));
for (const record of mutationRecords) {
  const id = String(record.mutation_id);
  const spec = mutationSpecForRecord(record as Parameters<typeof mutationSpecForRecord>[0]);
  if (!spec) {
    fail(`mutation manifest has no executable spec: ${id}`);
    continue;
  }
  const canonicalFingerprint = mutationPatchFingerprint(spec, "\n");
  if (String(record.frozen_patch_sha256) !== canonicalFingerprint) {
    fail(`mutation frozen patch fingerprint drifted: ${id}`);
  }
}
for (const id of Object.keys(mutationSpecs)) {
  if (!mutationIdsFromManifest.has(id)) fail(`executable mutation spec is not frozen in manifest: ${id}`);
}

if (String(profile.verified_zyra_head) !== VERIFIED_BASELINE) fail("verified baseline changed");
if (String(profile.implementation_diff_baseline) !== IMPLEMENTATION_DIFF_BASELINE) {
  fail("implementation diff baseline is not explicit");
}
const baselineParent = gitText(repoRoot, ["rev-parse", `${IMPLEMENTATION_DIFF_BASELINE}^`]);
if (baselineParent !== VERIFIED_BASELINE) fail("pre-E01 checkpoint is not directly based on verified head");
if ((profile.allowed_control_plane_commits as unknown[] | undefined)?.map(String).includes(IMPLEMENTATION_DIFF_BASELINE)) {
  fail("pre-E01 production checkpoint is incorrectly allowlisted as control-plane");
}
if (gitText(repoRoot, ["merge-base", "--is-ancestor", IMPLEMENTATION_DIFF_BASELINE, candidate]) !== "") {
  fail("candidate ancestry check returned unexpected output");
}

const sourceIds = sourceRecords.map((record) => String(record.mapping_id));
if (sourceIds.join("\n") !== [...sourceIds].sort().join("\n")) fail("source manifest is not sorted by mapping_id");
if (new Set(sourceIds).size !== sourceIds.length) fail("source manifest repeats mapping_id");
const sourceById = new Map(sourceRecords.map((record) => [String(record.mapping_id), record]));
const sourceBlobCache = new Map<string, { bytes: Buffer; lines: string[] }>();
const acceptedLineKeys = new Set<string>();
for (const record of sourceRecords) {
  const path = String(record.source_path);
  let blob = sourceBlobCache.get(path);
  if (!blob) {
    const bytes = gitBytes(sourceRoot, ["show", `${SOURCE_SNAPSHOT}:${path}`]);
    blob = { bytes, lines: bytes.toString("utf8").split("\n") };
    sourceBlobCache.set(path, blob);
  }
  if (String(record.source_snapshot) !== SOURCE_SNAPSHOT) fail(`source snapshot drift: ${path}`);
  if (String(record.source_sha256) !== sha256(blob.bytes)) fail(`source blob hash mismatch: ${path}`);
  const start = Number(record.start_line);
  const end = Number(record.end_line);
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start || end > blob.lines.length) {
    fail(`invalid frozen source range: ${String(record.mapping_id)}`);
    continue;
  }
  if (record.accepted === true) {
    const sourceRole = String(record.source_role);
    if (sourceRole !== "primary" && sourceRole !== "supplementary") {
      fail(`accepted source has forbidden source_role: ${String(record.mapping_id)} ${sourceRole}`);
    }
    if (sourceRole === "supplementary" && !String(record.supplementary_gap ?? "").trim()) {
      fail(`supplementary source lacks a primary-gap explanation: ${String(record.mapping_id)}`);
    }
    const symbolName = String(record.source_symbol).slice(String(record.source_symbol).lastIndexOf("::") + 2);
    if (
      String(record.mapping_id).startsWith("e01-rej-")
      && !String(record.continuation_of_source_symbol ?? "").trim()
      && !AUTHORIZED_POST_CUTOFF_SYMBOLS.has(symbolName)
    ) {
      fail(`outside-curated source symbol was accepted without authorization: ${symbolName}`);
    }
    for (let line = start; line <= end; line += 1) {
      if (executableLine(blob.lines[line - 1] ?? "")) acceptedLineKeys.add(`${path}:${line}`);
    }
    const symbol = String(record.source_symbol);
    if (/getPersistenceThreshold|currentFlushPromise|formatImageRef|restoreSessionStateFromLog|createBudgetTracker|snipProjection|snipModule|cachedMCModule|buildToolNameMap/.test(symbol)) {
      fail(`known unmigrated source behavior was accepted: ${symbol}`);
    }
  } else if (!String(record.exclusion_reason ?? "").trim()) {
    fail(`rejected source lacks reason: ${String(record.mapping_id)}`);
  }
}

const targetIds = targetRecords.map((record) => String(record.mapping_id));
if (targetIds.join("\n") !== [...targetIds].sort().join("\n")) fail("target manifest is not sorted by mapping_id");
if (new Set(targetIds).size !== targetIds.length) fail("target manifest repeats mapping_id");
const acceptedIds = sourceRecords.filter((record) => record.accepted === true).map((record) => String(record.mapping_id));
if (acceptedIds.join("\n") !== targetIds.join("\n")) fail("accepted source-to-target closure is not one-to-one");

const defaults = Array.isArray(profile.default_entries) ? (profile.default_entries as JsonRecord[]) : [];
const defaultIds = new Set(defaults.map((entry) => String(entry.default_entry_id)));
if (!defaultIds.has(DEFAULT_ENTRY_ID) || defaultIds.size !== defaults.length) fail("default entry registry is missing or duplicated");
for (const entry of defaults) {
  const path = String(entry.path);
  try {
    textAt(candidate, path);
  } catch {
    fail(`default entry does not exist in candidate: ${path}`);
  }
}

const mutationIds = new Set(mutationRecords.map((record) => String(record.mutation_id)));
const contractIds = new Set<string>();
const targetGroups = new Map<string, JsonRecord[]>();
for (const target of targetRecords) {
  const key = `${String(target.target_path)}::${String(target.target_symbol)}`;
  targetGroups.set(key, [...(targetGroups.get(key) ?? []), target]);
}
let fiveHopCount = 0;
for (const target of targetRecords) {
  const failuresBeforeTarget = failures.length;
  const id = String(target.mapping_id);
  const source = sourceById.get(id);
  if (!source || source.accepted !== true) {
    fail(`target ${id} has no accepted source`);
    continue;
  }
  if (!defaultIds.has(String(target.default_entry_id))) fail(`target ${id} references undefined default entry`);
  if (String(target.source_symbol) !== String(source.source_symbol)) fail(`target ${id} does not bind its source symbol`);
  if (!String(target.source_behavior_claim ?? "").trim()) fail(`target ${id} lacks source behavior claim`);
  if (!String(target.target_behavior_claim ?? "").trim()) fail(`target ${id} lacks target behavior claim`);
  if (!String(target.semantic_equivalence ?? "").trim()) fail(`target ${id} lacks semantic equivalence claim`);
  const targetPath = String(target.target_path);
  let targetText = "";
  try {
    const bytes = gitBytes(repoRoot, ["show", `${candidate}:${targetPath}`]);
    targetText = bytes.toString("utf8");
    if (String(target.target_sha256) !== sha256(bytes)) fail(`target blob hash mismatch: ${id}`);
  } catch {
    fail(`target path is absent at candidate: ${targetPath}`);
  }
  const targetLeaf = symbolLeaf(target.target_symbol);
  if (targetText && !new RegExp(`\\b${targetLeaf.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`).test(targetText)) {
    fail(`target symbol is absent: ${id} ${String(target.target_symbol)}`);
  }
  const callsitePath = String(target.default_callsite_path);
  try {
    const callsiteText = textAt(candidate, callsitePath);
    const callsiteLeaf = symbolLeaf(target.default_callsite_symbol);
    if (!callsiteText.includes(callsiteLeaf)) fail(`default callsite symbol is absent: ${id}`);
  } catch {
    fail(`default callsite path is absent: ${id}`);
  }
  const edges = Array.isArray(target.default_entry_edges)
    ? target.default_entry_edges as JsonRecord[]
    : [];
  if (edges.length === 0) fail(`target ${id} lacks an executable default-entry edge`);
  for (const edge of edges) {
    const callerPath = String(edge.caller_path);
    const callerSymbol = String(edge.caller_symbol);
    const calleePath = String(edge.callee_path);
    const calleeSymbol = String(edge.callee_symbol);
    try {
      const caller = declarationAnalysis(callerPath, textAt(candidate, callerPath), callerSymbol);
      const callee = declarationAnalysis(calleePath, textAt(candidate, calleePath), calleeSymbol);
      if (!caller) fail(`edge caller declaration is absent: ${id} ${callerSymbol}`);
      if (!callee) fail(`edge callee declaration is absent: ${id} ${calleeSymbol}`);
      if (caller && !caller.calledSymbols.has(symbolLeaf(calleeSymbol))) {
        fail(`edge caller does not invoke callee: ${id} ${callerSymbol} -> ${calleeSymbol}`);
      }
    } catch {
      fail(`edge path is absent: ${id} ${callerPath} -> ${calleePath}`);
    }
  }
  const tests = Array.isArray(target.behavior_tests) ? (target.behavior_tests as JsonRecord[]) : [];
  if (tests.length === 0) fail(`target ${id} lacks behavior tests`);
  const testBodies = new Map<string, string>();
  for (const test of tests) {
    try {
      const testPath = String(test.path);
      const testName = String(test.name);
      const testText = textAt(candidate, testPath);
      const body = namedTestBody(testPath, testText, testName);
      if (!body) {
        fail(`named behavior test callback is absent: ${id} ${testName}`);
        continue;
      }
      testBodies.set(testName, body);
      if (!body.includes("expect")) fail(`behavior test has no state assertion: ${id} ${testName}`);
      const anchor = String(test.anchor ?? "").trim();
      if (!anchor || !body.includes(anchor)) fail(`behavior invocation anchor is absent from named test: ${id} ${anchor}`);
      for (const token of (test.assertion_tokens as unknown[] | undefined) ?? []) {
        if (!body.includes(String(token))) fail(`behavior assertion token is absent from named test: ${id} ${String(token)}`);
      }
    } catch {
      fail(`behavior test path is absent: ${id}`);
    }
  }
  for (const mutationId of (target.mutation_ids as unknown[] | undefined) ?? []) {
    if (!mutationIds.has(String(mutationId))) fail(`target ${id} references unknown mutation ${String(mutationId)}`);
  }
  const exactMutationId = String(target.exact_mutation_id ?? "");
  const exactMutation = mutationRecordById.get(exactMutationId);
  if (
    !exactMutation
    || String(exactMutation.target_path) !== String(target.target_path)
    || String(exactMutation.target_symbol) !== String(target.target_symbol)
  ) {
    fail(`target ${id} does not bind its exact disconnect mutation`);
  } else {
    const killers = Array.isArray(exactMutation.expected_killer_test_ids)
      ? exactMutation.expected_killer_test_ids.map(String)
      : [];
    if (!killers.some((name) => testBodies.has(name))) {
      fail(`target ${id} exact mutation is not killed by its named behavior test`);
    }
  }
  const contractId = String(target.behavior_contract_id ?? "");
  if (contractId !== `e01.contract.${id}` || contractIds.has(contractId)) {
    fail(`target ${id} has an invalid or duplicate behavior contract id`);
  }
  contractIds.add(contractId);
  const equivalence = String(target.semantic_equivalence ?? "");
  for (const token of [id, symbolLeaf(source.source_symbol), targetLeaf, exactMutationId]) {
    if (!token || !equivalence.includes(token)) fail(`target ${id} semantic equivalence omits ${token || "required token"}`);
  }
  const stateAssertionTokens = Array.isArray(target.state_assertion_tokens)
    ? target.state_assertion_tokens.map(String)
    : [];
  if (stateAssertionTokens.length === 0) fail(`target ${id} lacks state assertion tokens`);
  for (const token of stateAssertionTokens) {
    if (![...testBodies.values()].some((body) => body.includes(token))) {
      fail(`target ${id} state assertion is absent from its named tests: ${token}`);
    }
  }
  const targetKey = `${String(target.target_path)}::${String(target.target_symbol)}`;
  const group = targetGroups.get(targetKey) ?? [];
  const consolidation = target.consolidation as JsonRecord | undefined;
  const consolidatedSources = Array.isArray(consolidation?.source_symbols)
    ? consolidation.source_symbols.map(String).sort()
    : [];
  const expectedSources = group.map((item) => String(item.source_symbol)).sort();
  if (
    Number(consolidation?.source_count) !== group.length
    || consolidatedSources.join("\n") !== expectedSources.join("\n")
    || !String(consolidation?.rationale ?? "").trim()
  ) {
    fail(`target ${id} lacks an exact consolidation disclosure`);
  }
  if (
    String(target.state_store ?? "").trim() &&
    String(target.state_effect_kind ?? "").trim() &&
    String(target.state_effect_assertion ?? "").trim()
  ) {
    if (failures.length === failuresBeforeTarget) fiveHopCount += 1;
  } else {
    fail(`target ${id} lacks state-effect hop`);
  }
}

const candidatePaths = gitText(repoRoot, ["ls-tree", "-r", "--name-only", candidate])
  .split(/\r?\n/)
  .filter(Boolean);
const changedPaths = gitText(repoRoot, ["diff", "--name-only", IMPLEMENTATION_DIFF_BASELINE, candidate])
  .split(/\r?\n/)
  .filter(productionTypeScript);
const changedFiles: ChangedFile[] = changedPaths.map((path) => {
  const text = textAt(candidate, path);
  return {
    path,
    changedLines: changedLineNumbers(IMPLEMENTATION_DIFF_BASELINE, candidate, path),
    text,
    lines: text.split(/\r?\n/),
  };
});
let grossChangedExecutable = 0;
for (const file of changedFiles) {
  for (const line of file.changedLines) {
    if (executableLine(file.lines[line - 1] ?? "")) grossChangedExecutable += 1;
  }
}
const units = changedFiles.flatMap(tokenUnits);
const duplicates = duplicateUnits(units);
const effectiveChangedTypeScript = grossChangedExecutable - duplicates.deduction;

const countBlobLines = (paths: readonly string[], predicate: (path: string) => boolean): number => {
  let count = 0;
  for (const path of paths.filter(predicate)) {
    const text = textAt(candidate, path);
    count += text.split(/\r?\n/).filter(executableLine).length;
  }
  return count;
};
const finalProductionTypeScript = countBlobLines(candidatePaths, productionTypeScript);
const finalTestTypeScript = countBlobLines(candidatePaths, (path) => /\.tsx?$/.test(path) && testTypeScript(path));
let pythonDeletedLines = 0;
for (const line of gitText(repoRoot, ["diff", "--numstat", IMPLEMENTATION_DIFF_BASELINE, candidate]).split(/\r?\n/)) {
  const [added, deleted, path] = line.split("\t");
  if (path?.endsWith(".py") && /^\d+$/.test(deleted ?? "")) pythonDeletedLines += Number(deleted);
  void added;
}

const thresholds = {
  acceptedSourceExecutable: 10_587,
  effectiveChangedTypeScript: 25_416,
  finalProductionTypeScript: 34_000,
  finalTestTypeScript: 8_000,
  pythonDeletedLines: 2_850,
  mutations: 44,
};
if (acceptedLineKeys.size < thresholds.acceptedSourceExecutable) {
  fail(`accepted frozen source executable lines ${acceptedLineKeys.size} < ${thresholds.acceptedSourceExecutable}`);
}
if (effectiveChangedTypeScript < thresholds.effectiveChangedTypeScript) {
  fail(`token-winnowed changed TypeScript ${effectiveChangedTypeScript} < ${thresholds.effectiveChangedTypeScript}`);
}
if (finalProductionTypeScript < thresholds.finalProductionTypeScript) {
  fail(`final production TypeScript ${finalProductionTypeScript} < ${thresholds.finalProductionTypeScript}`);
}
if (finalTestTypeScript < thresholds.finalTestTypeScript) {
  fail(`final test TypeScript ${finalTestTypeScript} < ${thresholds.finalTestTypeScript}`);
}
if (pythonDeletedLines < thresholds.pythonDeletedLines) {
  fail(`Python deletion floor ${pythonDeletedLines} < ${thresholds.pythonDeletedLines}`);
}
if (mutationRecords.length < thresholds.mutations) fail(`mutation count ${mutationRecords.length} < ${thresholds.mutations}`);

if (String(metadata.verified_baseline) !== VERIFIED_BASELINE) fail("candidate metadata verified baseline drifted");
if (String(metadata.verification_contract_version) !== VERIFICATION_CONTRACT_VERSION) {
  fail("candidate metadata verification contract drifted");
}
if (String(metadata.implementation_diff_baseline) !== IMPLEMENTATION_DIFF_BASELINE) {
  fail("candidate metadata lacks diff rebaseline");
}
if (String(metadata.implementation_candidate) !== candidate) fail("candidate metadata does not identify validation target");
if (args.requireMetadata) {
  if (!String(metadata.candidate_evidence_commit ?? "").match(/^[0-9a-f]{40}$/)) fail("final metadata lacks evidence commit");
  if (!String(metadata.independent_review_target ?? "").match(/^[0-9a-f]{40}$/)) fail("final metadata lacks review target");
  if (String(metadata.independent_review_verdict) !== "PASS") fail("final metadata does not record independent PASS");
  if (metadata.verified_complete !== true) fail("final metadata is not marked verified complete");
  try {
    const mutationEvidence = JSON.parse(
      textAt(String(metadata.candidate_evidence_commit), mutationEvidencePath),
    ) as JsonRecord;
    const results = Array.isArray(mutationEvidence.results)
      ? (mutationEvidence.results as JsonRecord[])
      : [];
    if (results.length !== mutationRecords.length) fail("final mutation evidence does not cover the frozen corpus");
    for (const result of results) {
      const id = String(result.mutation_id);
      if (result.killed !== true || result.compile_survived !== true) {
        fail(`final mutation evidence did not kill a compilable mutant: ${id}`);
      }
      if (String(result.actual_patch_sha256) !== String(result.frozen_patch_sha256)) {
        fail(`final mutation evidence patch identity mismatch: ${id}`);
      }
      if (String(result.original_sha256) !== String(result.restored_sha256)) {
        fail(`final mutation evidence source restoration mismatch: ${id}`);
      }
    }
  } catch (error) {
    fail(`final mutation evidence is unavailable: ${error instanceof Error ? error.message : String(error)}`);
  }
}

checks.source_manifest = {
  total: sourceRecords.length,
  accepted: acceptedIds.length,
  rejected: sourceRecords.length - acceptedIds.length,
  accepted_executable_lines: acceptedLineKeys.size,
  hash_semantics: "immutable-git-blob-bytes",
};
checks.source_to_target = {
  target_mappings: targetRecords.length,
  five_hop_mappings: fiveHopCount,
  default_entries: defaults.length,
};
checks.scope = {
  verified_baseline: VERIFIED_BASELINE,
  implementation_diff_baseline: IMPLEMENTATION_DIFF_BASELINE,
  preexisting_baseline_e01_credit: 0,
  candidate,
};
checks.effective_lines = {
  gross_changed_executable_typescript: grossChangedExecutable,
  token_winnowing: {
    shingle_tokens: 5,
    window_shingles: 8,
    minimum_length_ratio: 0.8,
    minimum_jaccard: 0.8,
    compared_units: units.length,
    duplicate_pairs: duplicates.pairs,
    deducted_lines: duplicates.deduction,
  },
  effective_changed_typescript: effectiveChangedTypeScript,
  final_production_typescript: finalProductionTypeScript,
  final_test_typescript: finalTestTypeScript,
  python_deleted_lines: pythonDeletedLines,
};
checks.mutations = {
  declared: mutationRecords.length,
  canonical_frozen_patch_fingerprints: mutationRecords.filter((record) => {
    const spec = mutationSpecForRecord(record as Parameters<typeof mutationSpecForRecord>[0]);
    return spec && String(record.frozen_patch_sha256) === mutationPatchFingerprint(spec, "\n");
  }).length,
};

const report = {
  schema_version: "4.0",
  execution_id: "E01",
  verifier: "verify_m1_r01_e01_v4",
  candidate,
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
if (failures.length > 0) process.exitCode = 1;
