#!/usr/bin/env bun

import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import * as ts from "typescript";

type Json = Record<string, any>;

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const workspaceRoot = resolve(repoRoot, "..");
const manifestPath = join(workspaceRoot, "docs/remediations/M1-R01-claude-source-custody/manifests/execution-03-mutation-manifest.jsonl");
const outputPath = join(repoRoot, "docs/reviews/evidence/M1-R01-v3/execution-03/mutation-results.json");
const testRoot = "packages/runtime/claude-runtime/test/e03/mutation-killers.test.ts";

function git(cwd: string, args: readonly string[]): string {
  return execFileSync("git", [...args], { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trimEnd();
}

function mutations(): Json[] {
  return readFileSync(manifestPath, "utf8").split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line) as Json);
}

function findMethod(source: ts.SourceFile, symbol: string): ts.MethodDeclaration | ts.GetAccessorDeclaration | ts.SetAccessorDeclaration {
  const [owner, method] = symbol.split(".");
  let found: ts.MethodDeclaration | ts.GetAccessorDeclaration | ts.SetAccessorDeclaration | undefined;
  for (const statement of source.statements) {
    if (!ts.isClassDeclaration(statement) || statement.name?.text !== owner) continue;
    for (const member of statement.members) {
      if (!(ts.isMethodDeclaration(member) || ts.isGetAccessorDeclaration(member) || ts.isSetAccessorDeclaration(member))) continue;
      if (member.name.getText(source) === method) found = member;
    }
  }
  if (!found?.body) throw new Error(`target method ${symbol} not found in ${source.fileName}`);
  return found;
}

function mutate(path: string, symbol: string, statement: string): { original: string; mutated: string } {
  const original = readFileSync(path, "utf8");
  const source = ts.createSourceFile(path, original, ts.ScriptTarget.ESNext, true, path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const method = findMethod(source, symbol);
  const insertion = method.body!.getStart(source) + 1;
  return { original, mutated: `${original.slice(0, insertion)}\n    ${statement};${original.slice(insertion)}` };
}

function runBun(cwd: string, args: readonly string[]): { status: number; stdout: string; stderr: string } {
  const executable = process.execPath;
  const result = spawnSync(executable, [...args], { cwd, encoding: "utf8", env: { ...process.env, NO_COLOR: "1" }, timeout: 120_000 });
  return { status: result.status ?? 255, stdout: result.stdout ?? "", stderr: result.stderr ?? String(result.error ?? "") };
}

function main(): void {
  const candidate = process.argv.includes("--candidate") ? process.argv[process.argv.indexOf("--candidate") + 1]! : git(repoRoot, ["rev-parse", "HEAD"]);
  const tempRoot = join(repoRoot, ".tmp", `e03-mutation-${candidate.slice(0, 12)}`);
  if (tempRoot === repoRoot || !tempRoot.startsWith(join(repoRoot, ".tmp"))) throw new Error(`unsafe mutation root ${tempRoot}`);
  rmSync(tempRoot, { recursive: true, force: true });
  mkdirSync(dirname(tempRoot), { recursive: true });
  git(repoRoot, ["worktree", "add", "--detach", tempRoot, candidate]);
  const rows = mutations();
  const results: Json[] = [];
  try {
    const baseline = runBun(tempRoot, ["test", testRoot]);
    if (baseline.status !== 0) throw new Error(`mutation baseline failed\n${baseline.stdout}\n${baseline.stderr}`);
    for (const row of rows) {
      const absolute = join(tempRoot, row.target_path);
      const patch = mutate(absolute, row.target_symbol, row.frozen_patch.injected_statement);
      writeFileSync(absolute, patch.mutated, "utf8");
      const killer = row.expected_killer_test_ids[0] as string;
      const run = runBun(tempRoot, ["test", testRoot, "-t", killer]);
      writeFileSync(absolute, patch.original, "utf8");
      results.push({
        mutation_id: row.mutation_id, target_path: row.target_path, target_symbol: row.target_symbol,
        killer_test_id: killer, killed: run.status !== 0, exit_code: run.status,
        stdout_tail: run.stdout.slice(-2_000), stderr_tail: run.stderr.slice(-2_000),
      });
    }
  } finally {
    git(repoRoot, ["worktree", "remove", "--force", tempRoot]);
  }
  const coreRisks = /fallback|late-result|task-commit|control|lost-ack|stale/i;
  const core = results.filter((result) => coreRisks.test(String(rows.find((row) => row.mutation_id === result.mutation_id)?.semantic_risk)));
  const other = results.filter((result) => !core.includes(result));
  const report = {
    schema_version: "3.0", execution_id: "E03", candidate, total: results.length,
    killed: results.filter((result) => result.killed).length,
    core_killed_ratio: core.length ? core.filter((result) => result.killed).length / core.length : 1,
    other_killed_ratio: other.length ? other.filter((result) => result.killed).length / other.length : 1,
    results,
  };
  mkdirSync(dirname(outputPath), { recursive: true });
  writeFileSync(outputPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  if (report.core_killed_ratio !== 1 || report.other_killed_ratio < 0.9) process.exitCode = 1;
}

main();
