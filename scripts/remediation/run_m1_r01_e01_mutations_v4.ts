import { execFileSync, spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

interface MutationSpec {
  id: string;
  search: string;
  replacement: string;
  killer: string;
}

interface MutationResult {
  mutation_id: string;
  compile_survives: boolean;
  killed: boolean;
  killer_test_id: string;
  test_exit_code: number | null;
  status: "KILLED" | "SURVIVED" | "COMPILE_FAILED";
}

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const legacyRunner = resolve(here, "run_m1_r01_e01_mutations.ts");
const targetPath = resolve(
  repoRoot,
  "packages/runtime/claude-runtime/src/loop/tool-observation-budget-runtime.ts",
);
const testPath = "packages/runtime/claude-runtime/test/e01/tool-observation-budget.behavior.test.ts";
const onlyNew = process.argv.includes("--only-new");
const outputIndex = process.argv.indexOf("--output");
const outputPath = outputIndex >= 0 ? resolve(repoRoot, process.argv[outputIndex + 1]) : undefined;

const specs: MutationSpec[] = [
  {
    id: "e01-mut-045-observation-budget-enforcement",
    search: "const limit = Math.max(1, this.policy.maxRoundChars - errorReserve);",
    replacement: "const limit = Number.MAX_SAFE_INTEGER;",
    killer: "e01.mutation.observation-budget-enforces-cross-result-limit",
  },
  {
    id: "e01-mut-046-observation-budget-snapshot-checksum",
    search: "if (expectedChecksum !== snapshot.checksum) {",
    replacement: "if (false) {",
    killer: "e01.mutation.observation-budget-restore-rejects-tampering",
  },
];

let legacyOutput: unknown = null;
if (!onlyNew) {
  const output = execFileSync(process.execPath, [legacyRunner], {
    cwd: repoRoot,
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
  });
  const trimmed = output.trim();
  try {
    legacyOutput = JSON.parse(trimmed);
  } catch {
    legacyOutput = { raw_output: trimmed };
  }
}

const original = readFileSync(targetPath, "utf8");
const results: MutationResult[] = [];
for (const spec of specs) {
  if (!original.includes(spec.search)) throw new Error(`mutation anchor is absent: ${spec.id}`);
  const mutated = original.replace(spec.search, spec.replacement);
  writeFileSync(targetPath, mutated, "utf8");
  try {
    const compile = spawnSync(process.execPath, ["run", "typecheck"], {
      cwd: repoRoot,
      encoding: "utf8",
      timeout: 120_000,
    });
    if (compile.status !== 0) {
      results.push({
        mutation_id: spec.id,
        compile_survives: false,
        killed: false,
        killer_test_id: spec.killer,
        test_exit_code: null,
        status: "COMPILE_FAILED",
      });
      continue;
    }
    const test = spawnSync(
      process.execPath,
      ["test", testPath, "--test-name-pattern", spec.killer],
      { cwd: repoRoot, encoding: "utf8", timeout: 120_000 },
    );
    results.push({
      mutation_id: spec.id,
      compile_survives: true,
      killed: test.status !== 0,
      killer_test_id: spec.killer,
      test_exit_code: test.status,
      status: test.status !== 0 ? "KILLED" : "SURVIVED",
    });
  } finally {
    writeFileSync(targetPath, original, "utf8");
  }
}

const report = {
  schema_version: "4.0",
  execution_id: "E01",
  legacy_mutations: legacyOutput,
  strict_mutations: results,
  summary: {
    strict_total: results.length,
    strict_killed: results.filter((result) => result.killed && result.compile_survives).length,
    strict_survived: results.filter((result) => result.status === "SURVIVED").length,
    strict_compile_failed: results.filter((result) => result.status === "COMPILE_FAILED").length,
  },
};
const rendered = `${JSON.stringify(report, null, 2)}\n`;
if (outputPath) writeFileSync(outputPath, rendered, "utf8");
process.stdout.write(rendered);
if (results.some((result) => !result.killed || !result.compile_survives)) process.exitCode = 1;
