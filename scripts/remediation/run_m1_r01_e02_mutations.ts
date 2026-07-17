#!/usr/bin/env bun

import { createHash } from "node:crypto";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import ts from "typescript";

interface FrozenPatch {
  injected_statement: string;
  kind: "typescript-ast-method-entry-throw";
  mutation_id: string;
  target_path: string;
  target_symbol: string;
}

interface ManifestRow {
  schema_version: "3.0";
  execution_id: "E02";
  mutation_id: string;
  mutation_operator: "disconnect-target";
  target_path: string;
  target_symbol: string;
  semantic_risk: string;
  compile_survives: true;
  expected_killer_test_ids: string[];
  frozen_patch: FrozenPatch;
  frozen_patch_sha256: string;
}

interface CommandResult {
  command: string[];
  exitCode: number;
  durationMs: number;
  stdout: string;
  stderr: string;
}

interface MutationMaterialization {
  source: string;
  insertionOffset: number;
  insertedText: string;
  declarationKind: string;
}

const zyra = resolve(import.meta.dir, "..", "..");
const workspace = resolve(zyra, "..");
const manifestPath = join(
  workspace,
  "docs",
  "remediations",
  "M1-R01-claude-source-custody",
  "manifests",
  "execution-02-mutation-manifest.jsonl",
);
const evidencePath = join(
  zyra,
  "docs",
  "reviews",
  "evidence",
  "M1-R01-v3",
  "execution-02",
  "mutation-results.json",
);
const buildOutput = join(zyra, ".tmp", "m1-r01-e02-mutant-build.js");
const mutationContract = "./packages/runtime/claude-runtime/test/e02/mutation-contract.test.ts";

function hash(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

async function run(command: string[], cwd = zyra): Promise<CommandResult> {
  const started = performance.now();
  const child = Bun.spawn({
    cmd: command,
    cwd,
    env: { ...process.env, NO_COLOR: "1", FORCE_COLOR: "0" },
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
  });
  const stdoutPromise = new Response(child.stdout).text();
  const stderrPromise = new Response(child.stderr).text();
  const exitCode = await child.exited;
  return {
    command,
    exitCode,
    durationMs: Math.round(performance.now() - started),
    stdout: await stdoutPromise,
    stderr: await stderrPromise,
  };
}

function excerpt(value: string, limit = 4_000): string {
  const normalized = value.replace(/\x1b\[[0-9;]*m/g, "").trim();
  return normalized.length <= limit ? normalized : normalized.slice(-limit);
}

function materializeTargetDisconnect(
  targetPath: string,
  targetSymbol: string,
  mutationId: string,
  sourceText: string,
): MutationMaterialization {
  const source = ts.createSourceFile(targetPath, sourceText, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS);
  const parts = targetSymbol.split(".");
  if (parts.length !== 2) throw new Error(`E02 mutation target must be Class.method: ${targetSymbol}`);
  const [owner, leaf] = parts;
  let selected: ts.MethodDeclaration | null = null;
  const visit = (node: ts.Node): void => {
    if (selected) return;
    if (ts.isClassDeclaration(node) && node.name?.text === owner) {
      for (const member of node.members) {
        if (ts.isMethodDeclaration(member) && member.body && member.name.getText(source) === leaf) {
          selected = member;
          return;
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  if (!selected) throw new Error(`target declaration body not found: ${targetPath}::${targetSymbol}`);
  const declaration = selected as ts.MethodDeclaration;
  const body = declaration.body!;
  const insertionOffset = body.getStart(source) + 1;
  const newline = sourceText.includes("\r\n") ? "\r\n" : "\n";
  const injected = `throw new Error(${JSON.stringify(`target_disconnect:${mutationId}`)});`;
  const insertedText = `${newline}    ${injected}`;
  return {
    source: sourceText.slice(0, insertionOffset) + insertedText + sourceText.slice(insertionOffset),
    insertionOffset,
    insertedText,
    declarationKind: ts.SyntaxKind[declaration.kind],
  };
}

function validateFrozenPatch(row: ManifestRow): void {
  const expected: FrozenPatch = {
    injected_statement: `throw new Error("target_disconnect:${row.mutation_id}")`,
    kind: "typescript-ast-method-entry-throw",
    mutation_id: row.mutation_id,
    target_path: row.target_path,
    target_symbol: row.target_symbol,
  };
  if (JSON.stringify(row.frozen_patch) !== JSON.stringify(expected)) {
    throw new Error(`frozen patch descriptor mismatch for ${row.mutation_id}`);
  }
  const actual = hash(JSON.stringify(row.frozen_patch));
  if (actual !== row.frozen_patch_sha256) {
    throw new Error(`frozen patch digest mismatch for ${row.mutation_id}: expected ${row.frozen_patch_sha256}, observed ${actual}`);
  }
}

function killerStatuses(output: string, expected: string[]): Record<string, "failed" | "passed" | "mentioned" | "missing"> {
  const normalized = output.replace(/\x1b\[[0-9;]*m/g, "");
  const lines = normalized.split(/\r?\n/);
  return Object.fromEntries(expected.map((id) => {
    const matching = lines.filter((line) => line.includes(id));
    const failed = matching.some((line) => /(?:^|\s)(?:\(fail\)|fail(?:ed)?|✗|✘|×)(?:\s|$)/i.test(line));
    const passed = matching.some((line) => /(?:^|\s)(?:\(pass\)|pass(?:ed)?|✓|✔)(?:\s|$)/i.test(line));
    return [id, failed ? "failed" : passed ? "passed" : matching.length ? "mentioned" : "missing"];
  })) as Record<string, "failed" | "passed" | "mentioned" | "missing">;
}

async function main(): Promise<void> {
  const rows = (await readFile(manifestPath, "utf8"))
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => JSON.parse(line) as ManifestRow);
  if (rows.length !== 70) throw new Error(`expected exactly 70 target-specific E02 mutations, received ${rows.length}`);
  const ids = new Set<string>();
  for (const row of rows) {
    if (row.execution_id !== "E02" || row.mutation_operator !== "disconnect-target" || row.compile_survives !== true) {
      throw new Error(`unsupported mutation record ${row.mutation_id}`);
    }
    if (ids.has(row.mutation_id)) throw new Error(`duplicate mutation ${row.mutation_id}`);
    ids.add(row.mutation_id);
    validateFrozenPatch(row);
  }

  await mkdir(dirname(evidencePath), { recursive: true });
  await mkdir(dirname(buildOutput), { recursive: true });
  const baseline = await run([process.execPath, "test", mutationContract]);
  if (baseline.exitCode !== 0) {
    throw new Error(`baseline E02 mutation contract failed\n${excerpt(baseline.stdout + "\n" + baseline.stderr)}`);
  }

  const results: Array<Record<string, unknown>> = [];
  for (const row of rows) {
    const target = join(zyra, row.target_path);
    const originalBytes = await readFile(target);
    const original = originalBytes.toString("utf8");
    const originalSha256 = hash(originalBytes);
    let materialized: MutationMaterialization | null = null;
    let compile: CommandResult | null = null;
    let contract: CommandResult | null = null;
    let runnerError: string | null = null;
    try {
      materialized = materializeTargetDisconnect(row.target_path, row.target_symbol, row.mutation_id, original);
      if (materialized.source === original) throw new Error("mutation produced no source change");
      await writeFile(target, materialized.source, "utf8");
      compile = await run([process.execPath, "build", row.target_path, "--target", "bun", "--outfile", buildOutput]);
      if (compile.exitCode === 0) contract = await run([process.execPath, "test", mutationContract]);
    } catch (error) {
      runnerError = error instanceof Error ? error.message : String(error);
    } finally {
      await writeFile(target, originalBytes);
      await rm(buildOutput, { force: true });
    }

    const restoredBytes = await readFile(target);
    const restoredSha256 = hash(restoredBytes);
    if (restoredSha256 !== originalSha256) throw new Error(`source restoration failed for ${row.mutation_id}`);
    const output = `${contract?.stdout ?? ""}\n${contract?.stderr ?? ""}`;
    const statuses = killerStatuses(output, row.expected_killer_test_ids);
    const expectedKillerFailed = Object.values(statuses).every((status) => status === "failed");
    const compileSurvived = compile?.exitCode === 0;
    const killed = runnerError === null
      && compileSurvived
      && contract !== null
      && contract.exitCode !== 0
      && expectedKillerFailed;
    results.push({
      mutation_id: row.mutation_id,
      semantic_risk: row.semantic_risk,
      target_path: row.target_path,
      target_symbol: row.target_symbol,
      mutation_operator: row.mutation_operator,
      expected_killer_test_ids: row.expected_killer_test_ids,
      frozen_patch_sha256: row.frozen_patch_sha256,
      observed_frozen_patch_sha256: hash(JSON.stringify(row.frozen_patch)),
      original_sha256: originalSha256,
      mutant_sha256: materialized ? hash(materialized.source) : null,
      restored_sha256: restoredSha256,
      insertion_offset: materialized?.insertionOffset ?? null,
      inserted_text_sha256: materialized ? hash(materialized.insertedText) : null,
      declaration_kind: materialized?.declarationKind ?? null,
      compile_survives_required: true,
      compile_survived: compileSurvived,
      compile_command: compile?.command ?? null,
      compile_exit_code: compile?.exitCode ?? null,
      compile_duration_ms: compile?.durationMs ?? null,
      test_command: contract?.command ?? null,
      test_exit_code: contract?.exitCode ?? null,
      test_duration_ms: contract?.durationMs ?? null,
      expected_killer_statuses: statuses,
      expected_killer_failed: expectedKillerFailed,
      output_sha256: hash(output),
      failure_excerpt: excerpt(output),
      runner_error: runnerError,
      killed,
    });
    process.stdout.write(`${row.mutation_id}: ${killed ? "KILLED" : "SURVIVED_OR_INVALID"}\n`);
  }

  const familySummary = Object.fromEntries(["permission", "mcp", "skill"].map((family) => {
    const familyResults = results.filter((result) => String(result.mutation_id).includes(`-${family}-`));
    const killed = familyResults.filter((result) => result.killed === true).length;
    return [family, { declared: familyResults.length, killed, kill_rate: familyResults.length ? killed / familyResults.length : 0 }];
  }));
  const killed = results.filter((result) => result.killed === true).length;
  const invalid = results.filter((result) => result.compile_survived !== true || result.runner_error !== null).length;
  const output = {
    schema_version: "3.0",
    execution_id: "E02",
    generated_at_utc: new Date().toISOString(),
    runner: "scripts/remediation/run_m1_r01_e02_mutations.ts",
    baseline: {
      command: baseline.command,
      exit_code: baseline.exitCode,
      duration_ms: baseline.durationMs,
      output_sha256: hash(`${baseline.stdout}\n${baseline.stderr}`),
    },
    summary: {
      declared: rows.length,
      applied: results.filter((result) => result.mutant_sha256 !== null && result.mutant_sha256 !== result.original_sha256).length,
      compile_survived: results.filter((result) => result.compile_survived === true).length,
      killed,
      survived: rows.length - killed - invalid,
      invalid,
      kill_rate: killed / rows.length,
      frozen_patch_matches: results.filter((result) => result.frozen_patch_sha256 === result.observed_frozen_patch_sha256).length,
      restored_sha_match: results.every((result) => result.original_sha256 === result.restored_sha256),
      family: familySummary,
    },
    results,
  };
  await writeFile(evidencePath, `${JSON.stringify(output, null, 2)}\n`, "utf8");
  if (killed !== rows.length || invalid !== 0) {
    process.exitCode = 1;
    process.stderr.write(`E02 mutation gate failed: killed=${killed}/${rows.length}, invalid=${invalid}\n`);
  }
}

if (import.meta.main) await main();
