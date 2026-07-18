#!/usr/bin/env bun

import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

type Result = { name: string; command: string[]; exit_code: number; stdout_tail: string; stderr_tail: string };

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const outputPath = join(repoRoot, "docs/reviews/evidence/M1-R01-v3/execution-03/cleanroom-result.json");

function git(cwd: string, args: readonly string[]): string {
  return execFileSync("git", [...args], { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trimEnd();
}

function run(cwd: string, name: string, command: string[]): Result {
  const result = spawnSync(command[0]!, command.slice(1), {
    cwd, encoding: "utf8", env: { ...process.env, NO_COLOR: "1", ZYRA_E03_STATE_ROOT: join(cwd, ".tmp", "e03-clean-state") }, timeout: 600_000,
  });
  return { name, command, exit_code: result.status ?? 255, stdout_tail: (result.stdout ?? "").slice(-8_000), stderr_tail: (result.stderr ?? String(result.error ?? "")).slice(-8_000) };
}

function main(): void {
  const candidate = process.argv.includes("--candidate") ? process.argv[process.argv.indexOf("--candidate") + 1]! : git(repoRoot, ["rev-parse", "HEAD"]);
  const cleanRoot = join(repoRoot, ".tmp", `e03-cleanroom-${candidate.slice(0, 12)}`);
  if (cleanRoot === repoRoot || !cleanRoot.startsWith(join(repoRoot, ".tmp"))) throw new Error(`unsafe cleanroom root ${cleanRoot}`);
  rmSync(cleanRoot, { recursive: true, force: true }); mkdirSync(dirname(cleanRoot), { recursive: true });
  git(repoRoot, ["worktree", "add", "--detach", cleanRoot, candidate]);
  const results: Result[] = [];
  const removed: string[] = [];
  try {
    for (const relativePath of ["vendor", "vendor-runtimes", "source-pool", "runtime-sources", "node_modules", ".cache", "dist", ".tmp"]) {
      const path = join(cleanRoot, relativePath);
      if (existsSync(path)) { rmSync(path, { recursive: true, force: true }); removed.push(relativePath); }
    }
    const packageText = readFileSync(join(cleanRoot, "package.json"), "utf8") + readFileSync(join(cleanRoot, "bun.lock"), "utf8");
    for (const forbidden of ["../claude-code-best", "../opencode", "../OpenClaw", "vendor-runtimes", "source-pool", "runtime-sources"]) {
      if (packageText.includes(forbidden)) throw new Error(`cleanroom dependency contains ${forbidden}`);
    }
    const bun = process.execPath;
    results.push(run(cleanRoot, "install", [bun, "install", "--frozen-lockfile"]));
    if (results.at(-1)!.exit_code === 0) results.push(run(cleanRoot, "typecheck", [bun, "run", "typecheck:e03"]));
    if (results.at(-1)!.exit_code === 0) results.push(run(cleanRoot, "build", [bun, "run", "build"]));
    if (results.at(-1)!.exit_code === 0) results.push(run(cleanRoot, "behavior", [bun, "run", "e03:test"]));
    if (results.at(-1)!.exit_code === 0) results.push(run(cleanRoot, "built-health", [bun, "run", "runtime:built:health"]));
    if (results.at(-1)!.exit_code === 0) results.push(run(cleanRoot, "probes", [bun, "scripts/remediation/probe_m1_r01_e03.ts", "all"]));
    const forbiddenExists = ["vendor", "vendor-runtimes", "source-pool", "runtime-sources"].filter((path) => existsSync(join(cleanRoot, path)));
    const passed = results.length === 6 && results.every((result) => result.exit_code === 0) && forbiddenExists.length === 0;
    const report = { schema_version: "3.0", execution_id: "E03", candidate, cleanroom_root: cleanRoot, removed, forbidden_exists: forbiddenExists, passed, results };
    mkdirSync(dirname(outputPath), { recursive: true }); writeFileSync(outputPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    if (!passed) process.exitCode = 1;
  } finally {
    git(repoRoot, ["worktree", "remove", "--force", cleanRoot]);
  }
}

main();
