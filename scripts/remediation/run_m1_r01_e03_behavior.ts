#!/usr/bin/env bun

import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

type FileResult = {
  path: string;
  exit_code: number;
  passed: number;
  failed: number;
  output_tail: string;
};

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const testRoot = join(
  repoRoot,
  "packages/runtime/claude-runtime/test/e03",
);
const files = readdirSync(testRoot)
  .filter((name) => name.endsWith(".test.ts"))
  .sort()
  .map((name) => join(testRoot, name));

const results: FileResult[] = [];
for (const path of files) {
  const run = spawnSync(process.execPath, ["test", path], {
    cwd: repoRoot,
    encoding: "utf8",
    env: { ...process.env, NO_COLOR: "1" },
    timeout: 120_000,
  });
  const output = `${run.stdout ?? ""}\n${run.stderr ?? ""}`;
  const passed = Number(output.match(/\b(\d+) pass\b/)?.[1] ?? 0);
  const failed = Number(output.match(/\b(\d+) fail\b/)?.[1] ?? 0);
  results.push({
    path: relative(repoRoot, path).replaceAll("\\", "/"),
    exit_code: run.status ?? 255,
    passed,
    failed,
    output_tail: output.slice(-2_000),
  });
  if ((run.status ?? 255) !== 0) break;
}

const passed = results.reduce((sum, result) => sum + result.passed, 0);
const failed = results.reduce((sum, result) => sum + result.failed, 0);
const report = {
  schema_version: "3.0",
  execution_id: "E03",
  discovered_files: files.length,
  executed_files: results.length,
  passed,
  failed,
  ok:
    files.length === 8 &&
    results.length === files.length &&
    passed >= 340 &&
    failed === 0 &&
    results.every((result) => result.exit_code === 0),
  results,
};
process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
if (!report.ok) process.exitCode = 1;
