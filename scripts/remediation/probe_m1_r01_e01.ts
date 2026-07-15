#!/usr/bin/env bun

import { createHash } from "node:crypto";
import { lstat, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { dirname, join, relative, resolve } from "node:path";
import { E01RuntimeCoordinator } from "../../packages/runtime/claude-runtime/src/e01/coordinator.ts";
import { command, identity, Journal } from "../../packages/runtime/claude-runtime/src/e01/kernel.ts";

type Obj = Record<string, unknown>;
type CommandResult = { command: string[]; exitCode: number; durationMs: number; stdout: string; stderr: string };

const zyra = resolve(import.meta.dir, "..", "..");
const evidenceRoot = join(zyra, "docs", "reviews", "evidence", "M1-R01-v3", "execution-01");

function hash(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function invariant(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

async function run(command: string[], cwd = zyra, env: Record<string, string | undefined> = process.env): Promise<CommandResult> {
  const started = performance.now();
  const child = Bun.spawn({
    cmd: command,
    cwd,
    env: { ...env, NO_COLOR: "1", FORCE_COLOR: "0" },
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
  });
  const stdoutPromise = new Response(child.stdout).text();
  const stderrPromise = new Response(child.stderr).text();
  const exitCode = await child.exited;
  return { command, exitCode, durationMs: Math.round(performance.now() - started), stdout: await stdoutPromise, stderr: await stderrPromise };
}

function receipt(commandResult: CommandResult): Obj {
  return {
    command: commandResult.command,
    exit_code: commandResult.exitCode,
    duration_ms: commandResult.durationMs,
    stdout_sha256: hash(commandResult.stdout),
    stderr_sha256: hash(commandResult.stderr),
    stdout_excerpt: commandResult.stdout.trim().slice(-2_000),
    stderr_excerpt: commandResult.stderr.trim().slice(-2_000),
  };
}

async function emit(name: string, payload: Obj): Promise<void> {
  const output = {
    schema_version: "3.0",
    execution_id: "E01",
    probe_id: `e01.probe.${name}`,
    generated_at_utc: new Date().toISOString(),
    ok: true,
    ...payload,
  };
  await mkdir(evidenceRoot, { recursive: true });
  await writeFile(join(evidenceRoot, `${name}-result.json`), JSON.stringify(output, null, 2) + "\n", "utf8");
  process.stdout.write(JSON.stringify(output) + "\n");
}

async function runtimeOrigin(): Promise<void> {
  const result = await run([process.execPath, "apps/code-worker/src/main.ts", "--e01-inventory"]);
  invariant(result.exitCode === 0, "default code-worker E01 entry failed");
  const line = result.stdout.trim().split(/\r?\n/).at(-1) ?? "";
  const payload = JSON.parse(line) as Obj;
  const snapshot = payload.snapshot as Obj;
  const journal = snapshot.journal as Obj;
  invariant(payload.ok === true, "inventory did not report success");
  invariant(journal.owner === "typescript", "default entry journal owner is not TypeScript");
  invariant(snapshot.version === "zyra.e01-runtime/v5", "default entry did not use E01 v5 runtime");
  await emit("runtime-origin", {
    entry_path: "apps/code-worker/src/main.ts",
    entry_command: result.command,
    runtime_version: snapshot.version,
    canonical_owner: journal.owner,
    journal_revision: journal.revision,
    command_receipt: receipt(result),
  });
}

async function writePath(): Promise<void> {
  const runtime = new E01RuntimeCoordinator("probe-write-run", "probe-write-session", "probe-write-task", "probe-write-worker");
  await runtime.bootstrap();
  const beforeRevision = runtime.journal.revision;
  const beforeDigest = hash(JSON.stringify(runtime.journal.state));
  const transition = runtime.recordRuntimeEvent("write_path_probe", { canonical_owner: "typescript", mutation: "state" });
  const afterRevision = runtime.journal.revision;
  const afterDigest = hash(JSON.stringify(runtime.journal.state));
  const committed = runtime.journal.committed();
  invariant(transition.owner === "typescript", "write path transition owner is not TypeScript");
  invariant(afterRevision > beforeRevision, "write path did not advance canonical revision");
  invariant(afterDigest !== beforeDigest, "write path did not mutate canonical state");
  invariant(committed.some((item) => item.identity.transitionId === transition.identity.transitionId), "write path transition was not committed");
  await emit("write-path", {
    default_callsite: "ClaudeRuntimeCore.run -> E01RuntimeCoordinator.recordRuntimeEvent -> Journal.commit",
    transition_id: transition.identity.transitionId,
    revision_before: beforeRevision,
    revision_after: afterRevision,
    state_digest_before: beforeDigest,
    state_digest_after: afterDigest,
    committed_count: committed.length,
    canonical_owner: transition.owner,
  });
}

async function resume(): Promise<void> {
  const first = new E01RuntimeCoordinator("probe-resume-run-a", "probe-resume-session", "probe-resume-task", "probe-resume-worker");
  await first.bootstrap();
  first.recordRuntimeEvent("before_snapshot", { value: 1 });
  const snapshot = first.snapshot();
  const oldIds = new Set(snapshot.journal.committed.map((item) => item.identity.transitionId));
  const revisionBefore = snapshot.journal.revision;
  const second = new E01RuntimeCoordinator("probe-resume-run-b", "probe-resume-session", "probe-resume-task", "probe-resume-worker");
  second.restore(snapshot);
  invariant(second.journal.revision === revisionBefore, "restore changed revision before resume bootstrap");
  await second.bootstrap();
  second.recordRuntimeEvent("after_resume", { value: 2 });
  const resumed = second.snapshot();
  const newIds = resumed.journal.committed.map((item) => item.identity.transitionId).filter((id) => !oldIds.has(id));
  const replayedIds = newIds.filter((id) => oldIds.has(id));
  invariant(newIds.length >= 2, "resume did not append new bootstrap and state transitions");
  invariant(new Set(newIds).size === newIds.length, "resume emitted duplicate transition IDs");
  invariant(replayedIds.length === 0, "resume replayed a pre-restart transition ID");
  invariant(resumed.journal.revision > revisionBefore, "resume revision is not monotonic");
  invariant(resumed.journal.restartEpoch === snapshot.journal.restartEpoch + 1, "resume restart epoch did not advance exactly once");
  await emit("same-session-resume", {
    session_id: resumed.sessionId,
    run_before: snapshot.runId,
    run_after: resumed.runId,
    revision_before: revisionBefore,
    revision_after: resumed.journal.revision,
    restart_epoch_before: snapshot.journal.restartEpoch,
    restart_epoch_after: resumed.journal.restartEpoch,
    prior_transition_count: oldIds.size,
    new_transition_count: newIds.length,
    replayed_transition_ids: replayedIds,
    new_transition_ids: newIds,
  });
}

async function lostAck(): Promise<void> {
  const journal = new Journal("probe-ack-run", "probe-ack-session");
  const transitionIdentity = identity(journal);
  const value = command(journal, "probe", "lost_ack", { value: 1 }, { identity: transitionIdentity, writeSet: ["probe.value"] });
  journal.prepare(value);
  const committed = journal.commit(value, { probe: { value: 1 } }, [{ kind: "probe.updated" }]);
  const revisionAtCommit = journal.revision;
  const committedCount = journal.committed().length;
  const duplicate = journal.prepare(value);
  invariant(duplicate.duplicate, "lost ACK retry did not resolve as duplicate");
  invariant(journal.revision === revisionAtCommit, "lost ACK retry repeated canonical mutation");
  invariant(journal.committed().length === committedCount, "lost ACK retry appended another commit");
  journal.ack(committed.identity.transitionId);
  journal.ack(committed.identity.transitionId);
  invariant(journal.revision === revisionAtCommit, "ACK changed canonical revision");
  invariant(journal.committed().length === committedCount, "ACK appended another transition");
  const firstProjection = journal.project(committed.outboxIds[0], "2026-07-15T00:00:00.000Z");
  const repeatedProjection = journal.project(committed.outboxIds[0], "2026-07-15T00:01:00.000Z");
  invariant(firstProjection.deliveredAt === repeatedProjection.deliveredAt, "outbox replay repeated the external projection");
  await emit("lost-ack", {
    transition_id: committed.identity.transitionId,
    duplicate_retry: duplicate.duplicate,
    revision_at_commit: revisionAtCommit,
    revision_after_retries: journal.revision,
    committed_count_before_ack: committedCount,
    committed_count_after_ack: journal.committed().length,
    outbox_id: committed.outboxIds[0],
    first_delivered_at: firstProjection.deliveredAt,
    replay_delivered_at: repeatedProjection.deliveredAt,
    repeated_effect_count: 0,
  });
}

async function disable(): Promise<void> {
  const result = await run(
    [process.execPath, "apps/code-worker/src/main.ts", "--e01-inventory"],
    zyra,
    { ...process.env, ZYRA_DISABLE_E01_TYPESCRIPT_RUNTIME: "1" },
  );
  const combined = result.stdout + "\n" + result.stderr;
  invariant(result.exitCode !== 0, "default entry still succeeded with TypeScript E01 owner disabled");
  invariant(combined.includes("e01_typescript_runtime_disabled"), "disable failure did not originate from the E01 owner gate");
  await emit("disable", {
    disabled_component: "E01RuntimeCoordinator.bootstrap",
    environment_gate: "ZYRA_DISABLE_E01_TYPESCRIPT_RUNTIME=1",
    default_entry_failed: true,
    expected_error: "e01_typescript_runtime_disabled",
    command_receipt: receipt(result),
  });
}

async function walk(root: string): Promise<string[]> {
  const output: string[] = [];
  for (const entry of await readdir(root, { withFileTypes: true })) {
    if (["node_modules", ".git", ".tmp", "tmp", "dist"].includes(entry.name)) continue;
    const path = join(root, entry.name);
    if (entry.isDirectory()) output.push(...await walk(path));
    else output.push(path);
  }
  return output;
}

async function dependencies(): Promise<void> {
  const roots = [
    join(zyra, "apps", "code-worker"),
    join(zyra, "packages", "runtime", "claude-runtime"),
    join(zyra, "packages", "runtime", "runtime-event-spine"),
  ];
  const files = (await Promise.all(roots.map(walk))).flat();
  const forbidden = [
    /\.\.\/(?:claude-code-best|opencode|OpenHands|browser-use)(?:\/|["'])/i,
    /(?:^|["'\/\\])(?:vendor|vendor-runtimes|source-pool|runtime-sources)(?:[\/\\]|["'])/i,
    /(?:file|link):\.\.[/\\]/i,
  ];
  const violations: Obj[] = [];
  const symlinks: string[] = [];
  for (const path of files) {
    const stat = await lstat(path);
    if (stat.isSymbolicLink()) symlinks.push(relative(zyra, path).replaceAll("\\", "/"));
    if (!/\.(?:ts|tsx|js|mjs|cjs|json)$/.test(path)) continue;
    const text = await readFile(path, "utf8");
    for (const pattern of forbidden) {
      if (pattern.test(text)) violations.push({ path: relative(zyra, path).replaceAll("\\", "/"), pattern: pattern.source });
    }
  }
  const packageFiles = [join(zyra, "package.json"), join(zyra, "bun.lock")];
  for (const path of packageFiles) {
    const text = await readFile(path, "utf8");
    if (/(?:file|link):\.\.[/\\]/i.test(text)) violations.push({ path: relative(zyra, path), pattern: "relative-package-link" });
  }
  invariant(violations.length === 0, `runtime dependency path violations: ${JSON.stringify(violations)}`);
  invariant(symlinks.length === 0, `runtime package contains symlinks: ${symlinks.join(",")}`);
  await emit("dependencies", {
    scanned_file_count: files.length,
    runtime_roots: roots.map((path) => relative(zyra, path).replaceAll("\\", "/")),
    forbidden_runtime_path_violations: violations,
    runtime_symlinks: symlinks,
    relative_package_links: [],
    clean_dependency_path: true,
  });
}

async function runToolchain(cwd: string): Promise<{ ok: boolean; receipts: Obj[] }> {
  const isolatedTemp = join(cwd, ".tmp", "e01-toolchain");
  const isolatedCache = join(cwd, ".tmp", "bun-install-cache");
  await mkdir(isolatedTemp, { recursive: true });
  await mkdir(isolatedCache, { recursive: true });
  const isolatedEnvironment = {
    ...process.env,
    TMP: isolatedTemp,
    TEMP: isolatedTemp,
    TMPDIR: isolatedTemp,
    BUN_INSTALL_CACHE_DIR: isolatedCache,
  };
  const commands = [
    [process.execPath, "--version"],
    [process.execPath, "install", "--frozen-lockfile"],
    [process.execPath, "run", "typecheck"],
    [process.execPath, "run", "build"],
    [process.execPath, "run", "runtime:e01:test"],
    [process.execPath, "run", "runtime:built:health"],
  ];
  const receipts: Obj[] = [];
  for (const commandLine of commands) {
    const result = await run(commandLine, cwd, isolatedEnvironment);
    receipts.push(receipt(result));
    if (result.exitCode !== 0) return { ok: false, receipts };
  }
  return { ok: true, receipts };
}

async function toolchain(): Promise<void> {
  invariant(Bun.version === "1.2.15", `unexpected Bun version ${Bun.version}`);
  const result = await runToolchain(zyra);
  invariant(result.ok, "frozen E01 toolchain command failed");
  await emit("toolchain", { bun_version: Bun.version, typescript_version: "5.8.3", commands: result.receipts });
}

async function cleanroom(): Promise<void> {
  const headResult = await run(["git", "rev-parse", "HEAD"]);
  invariant(headResult.exitCode === 0, "cannot resolve cleanroom target commit");
  const head = headResult.stdout.trim();
  const cleanroomRoot = join(zyra, "tmp", `e01-cleanroom-${head.slice(0, 12)}`);
  const archivePath = `${cleanroomRoot}.tar`;
  invariant(cleanroomRoot.startsWith(join(zyra, "tmp", "e01-cleanroom-")), "unsafe cleanroom path");
  await rm(cleanroomRoot, { recursive: true, force: true });
  await rm(archivePath, { force: true });
  await mkdir(dirname(cleanroomRoot), { recursive: true });
  const archive = await run(["git", "archive", "--format=tar", "-o", archivePath, head]);
  invariant(archive.exitCode === 0, "git archive failed for cleanroom");
  await mkdir(cleanroomRoot, { recursive: true });
  const extract = await run(["tar", "-xf", archivePath, "-C", cleanroomRoot]);
  invariant(extract.exitCode === 0, "cleanroom archive extraction failed");
  let toolchainResult: { ok: boolean; receipts: Obj[] };
  try {
    toolchainResult = await runToolchain(cleanroomRoot);
  } finally {
    await rm(archivePath, { force: true });
    await rm(cleanroomRoot, { recursive: true, force: true });
  }
  invariant(toolchainResult.ok, "cleanroom toolchain failed");
  await emit("cleanroom", {
    target_commit: head,
    source: "git archive",
    cache_state: "fresh extracted tree without node_modules, dist, .tmp, or git metadata",
    commands: toolchainResult.receipts,
    cleaned_after_run: true,
  });
}

const mode = process.argv[2] ?? "";
if (mode === "runtime-origin") await runtimeOrigin();
else if (mode === "write-path") await writePath();
else if (mode === "resume") await resume();
else if (mode === "lost-ack") await lostAck();
else if (mode === "disable") await disable();
else if (mode === "dependencies") await dependencies();
else if (mode === "toolchain") await toolchain();
else if (mode === "cleanroom") await cleanroom();
else throw new Error(`unsupported E01 probe mode: ${mode || "<missing>"}`);
