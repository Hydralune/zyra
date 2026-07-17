#!/usr/bin/env bun

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import * as ts from "typescript";

type Json = Record<string, any>;
type Finding = { gate: string; detail: string };

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const workspaceRoot = resolve(repoRoot, "..");
const manifestRoot = join(workspaceRoot, "docs/remediations/M1-R01-claude-source-custody/manifests");
const sourceRepos: Record<string, string> = {
  "claude-code-best": join(workspaceRoot, "claude-code-best"),
  opencode: join(workspaceRoot, "opencode"),
  OpenClaw: join(workspaceRoot, "OpenClaw"),
};
const profilePath = join(manifestRoot, "execution-03-gate-profile.json");
const receiptPath = join(manifestRoot, "execution-03-baseline-receipt.json");
const sourcePath = join(manifestRoot, "execution-03-source-manifest.jsonl");
const targetPath = join(manifestRoot, "execution-03-target-custody-map.jsonl");
const pythonPath = join(manifestRoot, "execution-03-python-owner-baseline.jsonl");
const mutationPath = join(manifestRoot, "execution-03-mutation-manifest.jsonl");

function sha256(value: Uint8Array | string): string {
  return createHash("sha256").update(value).digest("hex");
}

function git(cwd: string, args: readonly string[]): string {
  return execFileSync("git", [...args], { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trimEnd();
}

function gitBytes(cwd: string, args: readonly string[]): Buffer {
  return execFileSync("git", [...args], { cwd, encoding: "buffer", stdio: ["ignore", "pipe", "pipe"] }) as Buffer;
}

function json(path: string): Json {
  return JSON.parse(readFileSync(path, "utf8")) as Json;
}

function jsonl(path: string): Json[] {
  return readFileSync(path, "utf8").split(/\r?\n/).filter(Boolean).map((line, index) => {
    try { return JSON.parse(line) as Json; } catch (error) { throw new Error(`${path}:${index + 1}: ${String(error)}`); }
  });
}

function tsExecutable(line: string): boolean {
  const value = line.trim();
  return Boolean(value) && !value.startsWith("//") && !value.startsWith("/*") && !value.startsWith("*") && value !== "*/";
}

function pyExecutable(line: string): boolean {
  const value = line.trim();
  return Boolean(value) && !value.startsWith("#") && !value.startsWith("\"\"\"") && !value.startsWith("'''");
}

function sourceTextAt(repo: string, commit: string, path: string): { raw: Buffer; lines: string[] } {
  const raw = gitBytes(sourceRepos[repo]!, ["show", `${commit}:${path}`]);
  return { raw, lines: raw.toString("utf8").replaceAll("\r", "").split("\n") };
}

function candidateText(candidate: string, path: string): string {
  return gitBytes(repoRoot, ["show", `${candidate}:${path}`]).toString("utf8");
}

function walkSymbols(path: string, text: string): Set<string> {
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.ESNext, true, path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const symbols = new Set<string>();
  const visit = (node: ts.Node, owners: string[]): void => {
    let nextOwners = owners;
    if (ts.isClassDeclaration(node) && node.name) {
      symbols.add(node.name.text);
      nextOwners = [...owners, node.name.text];
    } else if ((ts.isFunctionDeclaration(node) || ts.isMethodDeclaration(node) || ts.isGetAccessorDeclaration(node) || ts.isSetAccessorDeclaration(node)) && node.name) {
      const name = node.name.getText(source);
      symbols.add(name);
      if (owners.length) symbols.add(`${owners.at(-1)}.${name}`);
    } else if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) {
      symbols.add(node.name.text);
    }
    ts.forEachChild(node, (child) => visit(child, nextOwners));
  };
  visit(source, []);
  return symbols;
}

function listFilesAt(candidate: string, roots: readonly string[]): string[] {
  const tracked = git(repoRoot, ["ls-tree", "-r", "--name-only", candidate]).split(/\r?\n/).filter(Boolean);
  return tracked.filter((path) => roots.some((root) => path === root || path.startsWith(`${root}/`)));
}

function changedFiles(baseline: string, candidate: string): string[] {
  return git(repoRoot, ["diff", "--name-only", baseline, candidate, "--"]).split(/\r?\n/).filter(Boolean);
}

function executableSloc(text: string): number {
  return text.replaceAll("\r", "").split("\n").filter(tsExecutable).length;
}

function normalizedTokens(text: string): string[] {
  return text.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/.*$/gm, " ").replace(/[A-Z]/g, (value) => ` ${value.toLowerCase()}`)
    .split(/[^a-z0-9_]+/).filter((token) => token.length > 2 && !["const", "return", "string", "number", "boolean", "undefined", "readonly", "export", "import", "from", "this", "async", "await"].includes(token));
}

function similarity(left: string, right: string): number {
  const a = new Set(normalizedTokens(left));
  const b = new Set(normalizedTokens(right));
  if (!a.size || !b.size) return 0;
  let intersection = 0;
  for (const token of a) if (b.has(token)) intersection += 1;
  return intersection / Math.max(a.size, b.size);
}

function assert(condition: unknown, findings: Finding[], gate: string, detail: string): void {
  if (!condition) findings.push({ gate, detail });
}

function verifyFrozenInputs(profile: Json, receipt: Json, findings: Finding[]): void {
  const files = [sourcePath, pythonPath, targetPath, mutationPath, profilePath];
  for (const path of files) {
    const name = relative(manifestRoot, path).replaceAll("\\", "/");
    assert(existsSync(path), findings, "g0", `${name} is missing`);
    if (existsSync(path)) assert(receipt.manifest_sha256?.[name] === sha256(readFileSync(path)), findings, "g0", `${name} hash differs from baseline receipt`);
  }
  assert(profile.schema_version === "3.0" && profile.execution_id === "E03", findings, "g0", "gate profile identity is invalid");
  try {
    git(repoRoot, ["merge-base", "--is-ancestor", profile.implementation_diff_baseline, receipt.g0_candidate_head]);
  } catch {
    findings.push({ gate: "g0", detail: "implementation baseline is not an ancestor of the G0 tooling head" });
  }
  assert(receipt.clean_worktree === true, findings, "g0", `G0 receipt captured dirty paths: ${JSON.stringify(receipt.dirty_paths ?? [])}`);
  assert(profile.required_toolchain?.bun === "1.2.15", findings, "toolchain", "Bun is not frozen at 1.2.15");
  for (const [path, expected] of Object.entries(profile.checker_sources ?? {})) {
    const absolute = join(repoRoot, path);
    assert(existsSync(absolute) && sha256(readFileSync(absolute)) === expected, findings, "g0", `${path} checker hash differs from frozen profile`);
  }
}

function verifySources(sourceRows: Json[], targetRows: Json[], profile: Json, candidate: string, findings: Finding[], finalized = true): Json {
  const accepted = sourceRows.filter((row) => row.accepted === true);
  assert(accepted.length === profile.thresholds.accepted_source_ranges, findings, "source", `expected ${profile.thresholds.accepted_source_ranges} accepted ranges, got ${accepted.length}`);
  const ids = new Set<string>();
  const intervals = new Map<string, Array<[number, number]>>();
  let sloc = 0;
  for (const row of accepted) {
    assert(row.schema_version === "3.0" && row.execution_id === "E03" && row.record_type === "source_range", findings, "source", `invalid source row ${row.mapping_id}`);
    assert(!ids.has(row.mapping_id), findings, "source", `duplicate mapping id ${row.mapping_id}`);
    ids.add(row.mapping_id);
    const snapshot = profile.source_snapshots[row.source_repo];
    assert(snapshot === row.source_snapshot, findings, "source", `${row.mapping_id} snapshot differs from profile`);
    if (!sourceRepos[row.source_repo]) { findings.push({ gate: "source", detail: `${row.mapping_id} has unknown repo ${row.source_repo}` }); continue; }
    const { raw, lines } = sourceTextAt(row.source_repo, row.source_snapshot, row.source_path);
    assert(sha256(raw) === row.source_sha256, findings, "source", `${row.mapping_id} source blob hash mismatch`);
    assert(row.start_line >= 1 && row.end_line >= row.start_line && row.end_line <= lines.length, findings, "source", `${row.mapping_id} has invalid line range`);
    const key = `${row.source_repo}:${row.source_path}`;
    const prior = intervals.get(key) ?? [];
    assert(!prior.some(([start, end]) => row.start_line <= end && row.end_line >= start), findings, "source", `${row.mapping_id} overlaps another accepted range`);
    prior.push([row.start_line, row.end_line]); intervals.set(key, prior);
    sloc += lines.slice(row.start_line - 1, row.end_line).filter(tsExecutable).length;
  }
  assert(sloc === profile.thresholds.accepted_source_executable_sloc, findings, "source", `expected ${profile.thresholds.accepted_source_executable_sloc} source SLOC, got ${sloc}`);
  assert(targetRows.length === accepted.length, findings, "mapping", `target rows ${targetRows.length} do not match source rows ${accepted.length}`);
  const targetUse = new Map<string, number>();
  const targetFiles = new Map<string, string>();
  for (const row of targetRows) {
    assert(ids.has(row.mapping_id), findings, "mapping", `${row.mapping_id} has no accepted source row`);
    const key = `${row.target_path}::${row.target_symbol}`;
    targetUse.set(key, (targetUse.get(key) ?? 0) + 1);
    if (!finalized) {
      assert(row.target_sha256 === null, findings, "mapping", `${row.mapping_id} G0 target hash must be null`);
      continue;
    }
    try {
      const text = targetFiles.get(row.target_path) ?? candidateText(candidate, row.target_path);
      targetFiles.set(row.target_path, text);
      assert(row.target_sha256 === sha256(Buffer.from(text, "utf8")), findings, "mapping", `${row.mapping_id} target hash is not finalized at candidate`);
      assert(walkSymbols(row.target_path, text).has(row.target_symbol), findings, "mapping", `${row.mapping_id} target symbol ${row.target_symbol} is absent`);
    } catch (error) { findings.push({ gate: "mapping", detail: `${row.mapping_id} target load failed: ${String(error)}` }); }
  }
  assert(targetUse.size >= profile.thresholds.source_to_target_unique_symbols_minimum, findings, "mapping", `only ${targetUse.size} unique target symbols`);
  for (const [symbol, count] of targetUse) assert(count <= profile.thresholds.source_to_target_max_mappings_per_symbol, findings, "mapping", `${symbol} has ${count} mappings`);
  return { accepted_ranges: accepted.length, accepted_source_sloc: sloc, unique_target_symbols: targetUse.size };
}

function verifyPython(rows: Json[], baseline: string, candidate: string, profile: Json, findings: Finding[], finalized = true): Json {
  let frozenDelete = 0;
  let deletedFiles = 0;
  for (const row of rows) {
    assert(row.schema_version === "3.0" && row.execution_id === "E03" && row.record_type === "python_owner", findings, "python", `invalid Python row ${row.owner_id}`);
    const raw = gitBytes(repoRoot, ["show", `${baseline}:${row.python_path}`]);
    assert(sha256(raw) === row.python_sha256, findings, "python", `${row.owner_id} baseline hash mismatch`);
    if (row.disposition === "delete") {
      const lines = raw.toString("utf8").replaceAll("\r", "").split("\n");
      frozenDelete += lines.slice(row.start_line - 1, row.end_line).filter(pyExecutable).length;
      if (finalized) {
        const present = git(repoRoot, ["ls-tree", "-r", "--name-only", candidate, "--", row.python_path]).trim();
        assert(!present, findings, "python", `${row.python_path} remains in candidate`);
        if (!present) deletedFiles += 1;
      }
    }
  }
  assert(frozenDelete === profile.thresholds.python_delete_executable_sloc, findings, "python", `frozen Python deletion is ${frozenDelete}, expected ${profile.thresholds.python_delete_executable_sloc}`);
  const retainedLogical = [
    "AgentTool", "SubagentRuntime", "LogicalFanoutRuntime", "CodeWorkerSubagentExecutionPort", "ParentScopeBuilder",
    "select_agent", "fork_context", "advance_task_phase", "dispatch_fanout", "decide_resume",
  ];
  let retainedLogicalHits = 0;
  for (const row of finalized ? rows.filter((item) => item.disposition === "retain") : []) {
    try {
      const text = candidateText(candidate, row.python_path);
      retainedLogicalHits += retainedLogical.filter((token) => text.includes(token)).length;
    } catch { findings.push({ gate: "python", detail: `retained Python port ${row.python_path} is missing` }); }
  }
  assert(retainedLogicalHits === 0, findings, "python", `${retainedLogicalHits} forbidden logical-owner symbols remain in retained Python ports`);
  return { frozen_delete_sloc: frozenDelete, deleted_files: deletedFiles, retained_logical_hits: retainedLogicalHits };
}

function verifyLines(profile: Json, baseline: string, candidate: string, findings: Finding[]): Json {
  const roots = profile.production_roots as string[];
  const excluded = profile.excluded_production_prefixes as string[];
  const finalFiles = listFilesAt(candidate, roots).filter((path) => [".ts", ".tsx"].includes(extname(path)) && !path.includes("/test/") && !excluded.some((prefix) => path.startsWith(prefix)));
  const changed = new Set(changedFiles(baseline, candidate));
  const changedFilesInScope = finalFiles.filter((path) => changed.has(path));
  const finalTexts = new Map(finalFiles.map((path) => [path, candidateText(candidate, path)]));
  const finalSloc = [...finalTexts.values()].reduce((sum, text) => sum + executableSloc(text), 0);
  const changedSloc = changedFilesInScope.reduce((sum, path) => sum + executableSloc(finalTexts.get(path)!), 0);
  assert(finalSloc >= profile.thresholds.final_non_test_typescript_sloc, findings, "lines", `final TypeScript SLOC ${finalSloc} < ${profile.thresholds.final_non_test_typescript_sloc}`);
  assert(changedSloc >= profile.thresholds.effective_changed_typescript_sloc, findings, "lines", `changed TypeScript SLOC ${changedSloc} < ${profile.thresholds.effective_changed_typescript_sloc}`);
  const duplicatePairs: string[] = [];
  for (let left = 0; left < changedFilesInScope.length; left += 1) {
    for (let right = left + 1; right < changedFilesInScope.length; right += 1) {
      const a = changedFilesInScope[left]!; const b = changedFilesInScope[right]!;
      if (executableSloc(finalTexts.get(a)!) < 80 || executableSloc(finalTexts.get(b)!) < 80) continue;
      if (similarity(finalTexts.get(a)!, finalTexts.get(b)!) >= 0.8) duplicatePairs.push(`${a} <> ${b}`);
    }
  }
  assert(duplicatePairs.length === 0, findings, "lines", `near-clone production files: ${duplicatePairs.slice(0, 5).join(", ")}`);
  const forbidden = profile.forbidden_runtime_dependencies as string[];
  for (const [path, text] of finalTexts) for (const token of forbidden) assert(!text.includes(token), findings, "dependency", `${path} contains forbidden runtime dependency ${token}`);
  return { final_sloc: finalSloc, changed_sloc: changedSloc, files: finalFiles.length, changed_files: changedFilesInScope.length, duplicate_pairs: duplicatePairs };
}

function verifyTests(profile: Json, candidate: string, findings: Finding[]): Json {
  const files = listFilesAt(candidate, profile.behavior_test_roots as string[]).filter((path) => [".ts", ".tsx"].includes(extname(path)));
  let sloc = 0; let cases = 0; let failures = 0; const texts: string[] = [];
  for (const path of files) {
    const text = candidateText(candidate, path); texts.push(text); sloc += executableSloc(text);
    const names = [...text.matchAll(/\b(?:test|it)\s*\(\s*["'`]([^"'`]+)["'`]/g)].map((match) => match[1]!);
    cases += names.length;
    failures += names.filter((name) => /fail|reject|crash|stale|lost|cancel|kill|timeout|conflict|tamper|duplicate|invalid|denied|unavailable|corrupt|late|dirty|escape/i.test(name)).length;
  }
  assert(sloc >= profile.thresholds.effective_behavior_test_sloc, findings, "tests", `behavior test SLOC ${sloc} < ${profile.thresholds.effective_behavior_test_sloc}`);
  assert(cases >= profile.thresholds.behavior_cases_minimum, findings, "tests", `behavior cases ${cases} < ${profile.thresholds.behavior_cases_minimum}`);
  assert(failures >= profile.thresholds.failure_cases_minimum, findings, "tests", `failure/crash cases ${failures} < ${profile.thresholds.failure_cases_minimum}`);
  const duplicateTests: string[] = [];
  for (let left = 0; left < texts.length; left += 1) for (let right = left + 1; right < texts.length; right += 1) if (similarity(texts[left]!, texts[right]!) >= 0.8) duplicateTests.push(`${files[left]} <> ${files[right]}`);
  assert(duplicateTests.length === 0, findings, "tests", `near-clone test files: ${duplicateTests.slice(0, 5).join(", ")}`);
  return { effective_sloc: sloc, behavior_cases: cases, failure_cases: failures, files: files.length, duplicate_pairs: duplicateTests };
}

function verifyMutations(rows: Json[], profile: Json, findings: Finding[]): Json {
  assert(rows.length >= profile.thresholds.mutation_points_minimum, findings, "mutations", `mutation points ${rows.length} < ${profile.thresholds.mutation_points_minimum}`);
  const ids = new Set<string>();
  for (const row of rows) {
    assert(!ids.has(row.mutation_id), findings, "mutations", `duplicate mutation ${row.mutation_id}`); ids.add(row.mutation_id);
    assert(row.compile_survives === true && row.expected_killer_test_ids?.length, findings, "mutations", `${row.mutation_id} lacks killer binding`);
    assert(row.frozen_patch_sha256 === sha256(stable(row.frozen_patch)), findings, "mutations", `${row.mutation_id} patch hash mismatch`);
  }
  return { points: rows.length };
}

function verifyCumulative(profile: Json, candidate: string, e03Tests: number, e03Delete: number, findings: Finding[]): Json {
  const executionIds = ["01", "02", "03"];
  const targetPaths = new Set<string>();
  let sourceSloc = 0;
  let pythonDeleteSloc = 0;
  for (const id of executionIds) {
    const sources = jsonl(join(manifestRoot, `execution-${id}-source-manifest.jsonl`)).filter((row) => row.accepted === true);
    for (const row of sources) {
      const repo = sourceRepos[row.source_repo];
      if (!repo) continue;
      const raw = gitBytes(repo, ["show", `${row.source_snapshot}:${row.source_path}`]).toString("utf8").replaceAll("\r", "").split("\n");
      sourceSloc += raw.slice(row.start_line - 1, row.end_line).filter(tsExecutable).length;
    }
    for (const row of jsonl(join(manifestRoot, `execution-${id}-target-custody-map.jsonl`))) targetPaths.add(row.target_path);
    for (const row of jsonl(join(manifestRoot, `execution-${id}-python-owner-baseline.jsonl`)).filter((item) => item.disposition === "delete")) {
      const baseline = row.verified_zyra_head;
      const raw = gitBytes(repoRoot, ["show", `${baseline}:${row.python_path}`]).toString("utf8").replaceAll("\r", "").split("\n");
      pythonDeleteSloc += raw.slice(row.start_line - 1, row.end_line).filter(pyExecutable).length;
    }
  }
  let finalSloc = 0;
  for (const path of targetPaths) {
    try { finalSloc += executableSloc(candidateText(candidate, path)); } catch { /* deleted Python-only or superseded target */ }
  }
  const predecessorTests = [
    "packages/runtime/claude-runtime/test/e01", "packages/runtime/claude-runtime/test/e02", "packages/integrations/claude-mcp/test/e02",
  ];
  let testSloc = e03Tests;
  for (const path of listFilesAt(candidate, predecessorTests).filter((file) => [".ts", ".tsx"].includes(extname(file)))) testSloc += executableSloc(candidateText(candidate, path));
  assert(finalSloc >= profile.thresholds.cumulative_final_typescript_sloc, findings, "cumulative", `cumulative target-file union ${finalSloc} < ${profile.thresholds.cumulative_final_typescript_sloc}`);
  assert(testSloc >= profile.thresholds.cumulative_test_sloc, findings, "cumulative", `cumulative test SLOC ${testSloc} < ${profile.thresholds.cumulative_test_sloc}`);
  assert(pythonDeleteSloc >= profile.thresholds.cumulative_python_delete_executable_sloc, findings, "cumulative", `cumulative Python delete ${pythonDeleteSloc} < ${profile.thresholds.cumulative_python_delete_executable_sloc}`);
  assert(sourceSloc >= 35_962, findings, "cumulative", `cumulative accepted source SLOC ${sourceSloc} < 35962`);
  return { source_sloc: sourceSloc, final_target_file_union_sloc: finalSloc, test_sloc: testSloc, python_delete_sloc: pythonDeleteSloc, e03_delete_sloc: e03Delete };
}

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") { const row = value as Json; return `{${Object.keys(row).sort().map((key) => `${JSON.stringify(key)}:${stable(row[key])}`).join(",")}}`; }
  return JSON.stringify(value);
}

function main(): void {
  const argument = process.argv.indexOf("--candidate");
  const candidate = argument >= 0 ? process.argv[argument + 1] : git(repoRoot, ["rev-parse", "HEAD"]);
  if (!candidate) throw new Error("--candidate requires a commit");
  const profile = json(profilePath); const receipt = json(receiptPath);
  const sourceRows = jsonl(sourcePath); const targetRows = jsonl(targetPath); const pythonRows = jsonl(pythonPath); const mutations = jsonl(mutationPath);
  const findings: Finding[] = [];
  git(repoRoot, ["cat-file", "-e", `${candidate}^{commit}`]);
  git(repoRoot, ["merge-base", "--is-ancestor", profile.implementation_diff_baseline, candidate]);
  verifyFrozenInputs(profile, receipt, findings);
  if (process.argv.includes("--g0-only")) {
    const report = {
      schema_version: "3.0", execution_id: "E03", candidate, mode: "g0-only",
      source: verifySources(sourceRows, targetRows, profile, candidate, findings, false),
      python: verifyPython(pythonRows, profile.implementation_diff_baseline, candidate, profile, findings, false),
      mutations: verifyMutations(mutations, profile, findings), findings,
    };
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    if (findings.length) process.exitCode = 1;
    return;
  }
  const sourceReport = verifySources(sourceRows, targetRows, profile, candidate, findings);
  const pythonReport = verifyPython(pythonRows, profile.implementation_diff_baseline, candidate, profile, findings);
  const lineReport = verifyLines(profile, profile.implementation_diff_baseline, candidate, findings);
  const testReport = verifyTests(profile, candidate, findings);
  const mutationReport = verifyMutations(mutations, profile, findings);
  const report = {
    schema_version: "3.0", execution_id: "E03", candidate, baseline: profile.implementation_diff_baseline,
    source: sourceReport, python: pythonReport, lines: lineReport, tests: testReport, mutations: mutationReport,
    cumulative: verifyCumulative(profile, candidate, testReport.effective_sloc, pythonReport.frozen_delete_sloc, findings),
    findings,
  };
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  if (findings.length) process.exitCode = 1;
}

main();
