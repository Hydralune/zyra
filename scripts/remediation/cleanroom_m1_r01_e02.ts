#!/usr/bin/env bun

import { createHash } from "node:crypto";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join, relative, resolve } from "node:path";

interface CommandEvidence {
  command: string[];
  started_at_utc: string;
  finished_at_utc: string;
  duration_ms: number;
  exit_code: number;
  stdout_sha256: string;
  stderr_sha256: string;
  output_tail: string;
}

const repoRoot = resolve(import.meta.dir, "..", "..");
const temporaryRoot = resolve(repoRoot, ".tmp", "m1-r01-e02-cleanroom");
const archivePath = resolve(repoRoot, ".tmp", "m1-r01-e02-cleanroom.tar");
const evidencePath = resolve(
  repoRoot,
  "docs",
  "reviews",
  "evidence",
  "M1-R01-v3",
  "execution-02",
  "cleanroom-result.json",
);
const npx = process.platform === "win32" ? "npx.cmd" : "npx";

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function assertSafeTemporaryPath(path: string): void {
  const relativePath = relative(resolve(repoRoot, ".tmp"), path);
  if (!relativePath || relativePath.startsWith("..") || resolve(path) === resolve(repoRoot, ".tmp")) {
    throw new Error(`unsafe E02 cleanroom path: ${path}`);
  }
}

async function run(command: string[], cwd: string): Promise<CommandEvidence> {
  const started = new Date();
  const clock = performance.now();
  const child = Bun.spawn({
    cmd: command,
    cwd,
    env: {
      ...process.env,
      NO_COLOR: "1",
      FORCE_COLOR: "0",
      npm_config_cache: resolve(repoRoot, ".tmp", "npm-cache"),
    },
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
  });
  const stdoutPromise = new Response(child.stdout).text();
  const stderrPromise = new Response(child.stderr).text();
  const exitCode = await child.exited;
  const stdout = await stdoutPromise;
  const stderr = await stderrPromise;
  const output = `${stdout}\n${stderr}`.replace(/\x1b\[[0-9;]*m/g, "").trim();
  return {
    command,
    started_at_utc: started.toISOString(),
    finished_at_utc: new Date().toISOString(),
    duration_ms: Math.round(performance.now() - clock),
    exit_code: exitCode,
    stdout_sha256: sha256(stdout),
    stderr_sha256: sha256(stderr),
    output_tail: output.length <= 8_000 ? output : output.slice(-8_000),
  };
}

async function requireSuccess(command: string[], cwd: string, evidence: CommandEvidence[]): Promise<void> {
  const result = await run(command, cwd);
  evidence.push(result);
  process.stdout.write(`${command.join(" ")}: exit=${result.exit_code} duration_ms=${result.duration_ms}\n`);
  if (result.exit_code !== 0) throw new Error(`cleanroom command failed: ${command.join(" ")}\n${result.output_tail}`);
}

async function requireBuiltHealth(command: string[], cwd: string, evidence: CommandEvidence[], candidate: string): Promise<void> {
  const result = await run(command, cwd);
  evidence.push(result);
  process.stdout.write(`${command.join(" ")}: exit=${result.exit_code} duration_ms=${result.duration_ms}\n`);
  if (result.exit_code !== 0) {
    throw new Error(`built health command failed: ${command.join(" ")}\n${result.output_tail}`);
  }
  const projections = result.output_tail
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.startsWith("{") && line.endsWith("}"))
    .flatMap((line) => {
      try {
        const value = JSON.parse(line) as Record<string, unknown>;
        return value.productizedRuntime ? [value] : [];
      } catch {
        return [];
      }
    });
  if (projections.length < 2) {
    throw new Error(`built health did not return both Bun and Node projections\n${result.output_tail}`);
  }
  for (const projection of projections) {
    const productized = projection.productizedRuntime as Record<string, unknown>;
    const capability = projection.e02CapabilityRuntime as Record<string, unknown>;
    if (
      projection.ok !== true
      || projection.defaultCapabilityEntrypoint !== "E02CapabilityCoordinator.execute"
      || projection.stateJournalOwner !== "E02CapabilityCoordinator"
      || productized.complete !== true
      || productized.evidenceIntegrity !== true
      || !capability
      || capability.schema !== "zyra.e02-built-readiness/v1"
      || capability.implementationReady !== true
      || capability.reviewStatus !== "implementation_complete_review_pending"
      || capability.independentReviewPassed !== false
      || capability.implementationCandidate !== candidate
      || capability.canonicalEntrypoint !== "E02CapabilityCoordinator.execute"
      || capability.stateJournalOwner !== "E02CapabilityCoordinator"
      || capability.sourceImportProbeAccepted !== false
      || capability.liveBuiltProbeRequired !== true
      || capability.pythonDecisionFallback !== false
    ) {
      throw new Error(`built health projection is incomplete: ${JSON.stringify(projection)}`);
    }
  }
}

async function main(): Promise<void> {
  assertSafeTemporaryPath(temporaryRoot);
  assertSafeTemporaryPath(archivePath);
  const dirtyResult = await run(["git", "status", "--porcelain=v1", "--untracked-files=all"], repoRoot);
  const dirtyPaths = dirtyResult.output_tail.split(/\r?\n/)
    .map((line) => line.trimEnd())
    .filter(Boolean)
    .map((line) => line.slice(3).replaceAll("\\", "/"));
  const nonEvidenceDirty = dirtyPaths.filter((path) => !path.startsWith("docs/reviews/evidence/M1-R01-v3/execution-02/"));
  if (nonEvidenceDirty.length) {
    throw new Error(`E02 cleanroom requires an implementation-clean worktree; observed ${nonEvidenceDirty.join(", ")}`);
  }
  const candidateResult = await run(["git", "rev-parse", "HEAD"], repoRoot);
  if (candidateResult.exit_code !== 0) throw new Error(candidateResult.output_tail);
  const candidate = candidateResult.output_tail.trim().split(/\r?\n/)[0]!;
  process.env.E02_IMPLEMENTATION_CANDIDATE = candidate;
  process.env.E02_REVIEW_STATUS = "implementation_complete_review_pending";
  const evidence: CommandEvidence[] = [];
  let error: string | null = null;
  await mkdir(dirname(archivePath), { recursive: true });
  await rm(temporaryRoot, { recursive: true, force: true });
  await rm(archivePath, { force: true });
  await mkdir(temporaryRoot, { recursive: true });
  try {
    await requireSuccess(["git", "archive", "--format=tar", `--output=${archivePath}`, candidate], repoRoot, evidence);
    await requireSuccess(["tar", "-xf", archivePath, "-C", temporaryRoot], repoRoot, evidence);
    await requireSuccess([npx, "--yes", "bun@1.2.15", "install", "--frozen-lockfile"], temporaryRoot, evidence);
    await requireSuccess([npx, "--yes", "bun@1.2.15", "run", "typecheck:e02"], temporaryRoot, evidence);
    await requireSuccess([npx, "--yes", "bun@1.2.15", "run", "build"], temporaryRoot, evidence);
    await requireBuiltHealth([npx, "--yes", "bun@1.2.15", "run", "runtime:built:health"], temporaryRoot, evidence, candidate);
    await requireSuccess([
      npx,
      "--yes",
      "bun@1.2.15",
      "scripts/remediation/probe_m1_r01_e02.ts",
      "all",
    ], temporaryRoot, evidence);
    await requireSuccess([
      npx,
      "--yes",
      "bun@1.2.15",
      "test",
      "packages/runtime/claude-runtime/test/e02",
      "packages/integrations/claude-mcp/test/e02",
    ], temporaryRoot, evidence);
  } catch (caught) {
    error = caught instanceof Error ? caught.message : String(caught);
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
    await rm(archivePath, { force: true });
  }
  const output = {
    schema_version: "3.0",
    execution_id: "E02",
    generated_at_utc: new Date().toISOString(),
    candidate,
    source: "git archive of exact candidate commit",
    preexisting_evidence_dirty_paths: dirtyPaths,
    implementation_worktree_clean: nonEvidenceDirty.length === 0,
    forbidden_workspace_source_dependencies: [
      "../claude-code-best",
      "../opencode",
      "../OpenClaw",
      "../Hermes-Agent",
    ],
    cleanroom_deleted_after_run: true,
    ok: error === null,
    error,
    commands: evidence,
  };
  await mkdir(dirname(evidencePath), { recursive: true });
  await writeFile(evidencePath, `${JSON.stringify(output, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify({ candidate, ok: error === null, evidence: evidencePath }, null, 2)}\n`);
  if (error) {
    process.stderr.write(`${error}\n`);
    process.exitCode = 1;
  }
  // Ensure the evidence was completely flushed before returning to the caller.
  await readFile(evidencePath);
}

if (import.meta.main) await main();
