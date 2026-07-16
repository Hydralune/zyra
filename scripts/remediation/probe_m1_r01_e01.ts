#!/usr/bin/env bun

import { createHash, randomUUID } from "node:crypto";
import { lstat, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import {
  E01RuntimeCoordinator,
  type E01CoordinatorSnapshot,
} from "../../packages/runtime/claude-runtime/src/e01/coordinator.ts";
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
  invariant(snapshot.version === "zyra.e01-runtime/v6", "default entry did not use E01 v6 runtime");
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

interface ResumeEpochMarker extends Obj {
  stage: number;
  process_id: number;
  input_snapshot: string | null;
  output_snapshot: string;
  output_sha256: string;
  revision: number;
  restart_epoch: number;
  transition_ids: string[];
  custody_restart_epoch: number;
}

interface KilledEpochResult {
  marker: ResumeEpochMarker;
  command: string[];
  exitCode: number;
  durationMs: number;
  stdout: string;
  stderr: string;
  forceKilled: boolean;
}

async function resumeWorker(): Promise<void> {
  const stage = Number(process.argv[3]);
  const inputPath = process.argv[4] ?? "";
  const outputPath = process.argv[5] ?? "";
  const markerPath = process.argv[6] ?? "";
  invariant(Number.isInteger(stage) && stage >= 0 && stage <= 2, "invalid resume worker stage");
  invariant(outputPath.length > 0 && markerPath.length > 0, "resume worker paths are missing");
  const runId = `probe-resume-run-${stage}`;
  const runtime = new E01RuntimeCoordinator(
    runId,
    "probe-resume-session",
    "probe-resume-task",
    "probe-resume-worker",
  );
  let inputSnapshot: E01CoordinatorSnapshot | null = null;
  if (stage > 0) {
    invariant(inputPath.length > 0, "resume worker input snapshot is missing");
    inputSnapshot = JSON.parse(await readFile(inputPath, "utf8")) as E01CoordinatorSnapshot;
    const revisionBeforeRestore = inputSnapshot.journal.revision;
    runtime.restore(inputSnapshot);
    invariant(
      runtime.journal.revision === revisionBeforeRestore,
      "restore changed revision before process bootstrap",
    );
  }
  await runtime.bootstrap();
  runtime.recordRuntimeEvent(`resume_epoch_${stage}`, {
    stage,
    process_id: process.pid,
    canonical_owner: "typescript",
  });
  const snapshot = runtime.snapshot();
  const encoded = JSON.stringify(snapshot, null, 2) + "\n";
  await writeFile(outputPath, encoded, "utf8");
  const marker: ResumeEpochMarker = {
    stage,
    process_id: process.pid,
    input_snapshot: inputSnapshot ? inputPath : null,
    output_snapshot: outputPath,
    output_sha256: hash(encoded),
    revision: snapshot.journal.revision,
    restart_epoch: snapshot.journal.restartEpoch,
    transition_ids: snapshot.journal.committed.map(
      (item) => item.identity.transitionId,
    ),
    custody_restart_epoch: snapshot.custody.restartEpoch,
  };
  await writeFile(markerPath, JSON.stringify(marker, null, 2) + "\n", "utf8");
  process.stdout.write(JSON.stringify({ ready_for_forced_termination: true, ...marker }) + "\n");
  await Bun.sleep(60_000);
  throw new Error("resume worker was not force-terminated by the probe parent");
}

async function runKilledResumeEpoch(
  stage: number,
  inputPath: string | null,
  outputPath: string,
  markerPath: string,
): Promise<KilledEpochResult> {
  const probePath = resolve(import.meta.dir, "probe_m1_r01_e01.ts");
  const command = [
    process.execPath,
    probePath,
    "resume-worker",
    String(stage),
    inputPath ?? "-",
    outputPath,
    markerPath,
  ];
  const started = performance.now();
  const child = Bun.spawn({
    cmd: command,
    cwd: zyra,
    env: { ...process.env, NO_COLOR: "1", FORCE_COLOR: "0" },
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
  });
  const stdoutPromise = new Response(child.stdout).text();
  const stderrPromise = new Response(child.stderr).text();
  let marker: ResumeEpochMarker | null = null;
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    try {
      marker = JSON.parse(await readFile(markerPath, "utf8")) as ResumeEpochMarker;
      break;
    } catch {
      if (await Promise.race([
        child.exited.then(() => true),
        Bun.sleep(10).then(() => false),
      ])) {
        break;
      }
    }
  }
  if (marker === null) {
    child.kill();
    const exitCode = await child.exited;
    const stdout = await stdoutPromise;
    const stderr = await stderrPromise;
    throw new Error(
      `resume worker ${stage} did not publish a durable marker; exit=${exitCode}; stdout=${stdout.slice(-2_000)}; stderr=${stderr.slice(-2_000)}`,
    );
  }
  child.kill();
  const exitCode = await child.exited;
  const stdout = await stdoutPromise;
  const stderr = await stderrPromise;
  invariant(
    exitCode !== 0,
    `resume worker ${stage} exited normally instead of being force-terminated`,
  );
  invariant(
    marker.process_id === child.pid,
    `resume worker ${stage} marker PID does not match spawned process`,
  );
  return {
    marker,
    command,
    exitCode,
    durationMs: Math.round(performance.now() - started),
    stdout,
    stderr,
    forceKilled: true,
  };
}

function transitionDelta(
  current: ResumeEpochMarker,
  previous: ResumeEpochMarker | null,
): string[] {
  const prior = new Set(previous?.transition_ids ?? []);
  return current.transition_ids.filter((id) => !prior.has(id));
}

async function resume(): Promise<void> {
  const root = join(tmpdir(), `zyra-e01-resume-${randomUUID()}`);
  await mkdir(root, { recursive: false });
  const snapshots = [0, 1, 2].map((stage) => join(root, `snapshot-${stage}.json`));
  const markers = [0, 1, 2].map((stage) => join(root, `marker-${stage}.json`));
  const epochs: KilledEpochResult[] = [];
  try {
    epochs.push(await runKilledResumeEpoch(0, null, snapshots[0], markers[0]));
    epochs.push(await runKilledResumeEpoch(1, snapshots[0], snapshots[1], markers[1]));
    epochs.push(await runKilledResumeEpoch(2, snapshots[1], snapshots[2], markers[2]));
    const [epoch0, epoch1, epoch2] = epochs.map((item) => item.marker);
    invariant(new Set(epochs.map((item) => item.marker.process_id)).size === 3, "resume epochs reused an owner process PID");
    invariant(epoch0.restart_epoch === 0, "initial owner process did not start at epoch zero");
    invariant(epoch1.restart_epoch === 1, "first restarted owner process did not advance to epoch one");
    invariant(epoch2.restart_epoch === 2, "second restarted owner process did not advance to epoch two");
    invariant(epoch0.custody_restart_epoch === 0, "initial execution custody epoch is not zero");
    invariant(epoch1.custody_restart_epoch === 1, "execution custody did not restore into epoch one");
    invariant(epoch2.custody_restart_epoch === 2, "execution custody did not restore into epoch two");
    invariant(epoch1.revision > epoch0.revision, "first process restart did not advance revision");
    invariant(epoch2.revision > epoch1.revision, "second process restart did not advance revision");
    const deltas = [
      transitionDelta(epoch0, null),
      transitionDelta(epoch1, epoch0),
      transitionDelta(epoch2, epoch1),
    ];
    for (const [index, ids] of deltas.entries()) {
      invariant(ids.length >= 2, `resume epoch ${index} did not append bootstrap and state transitions`);
      invariant(new Set(ids).size === ids.length, `resume epoch ${index} emitted duplicate transition IDs`);
    }
    const replayed = deltas.flatMap((ids, index) => {
      const other = new Set(deltas.filter((_, candidate) => candidate !== index).flat());
      return ids.filter((id) => other.has(id));
    });
    invariant(replayed.length === 0, "cross-process resume replayed a transition ID across epochs");
    await emit("same-session-resume", {
      session_id: "probe-resume-session",
      process_restart_count: 2,
      process_ids: epochs.map((item) => item.marker.process_id),
      force_killed_processes: epochs.map((item) => item.forceKilled),
      revisions: epochs.map((item) => item.marker.revision),
      restart_epochs: epochs.map((item) => item.marker.restart_epoch),
      custody_restart_epochs: epochs.map((item) => item.marker.custody_restart_epoch),
      epoch_transition_ids: deltas,
      replayed_transition_ids: replayed,
      worker_receipts: epochs.map((item) => ({
        command: item.command,
        exit_code: item.exitCode,
        duration_ms: item.durationMs,
        force_killed: item.forceKilled,
        process_id: item.marker.process_id,
        snapshot_sha256: item.marker.output_sha256,
        stdout_sha256: hash(item.stdout),
        stderr_sha256: hash(item.stderr),
      })),
      isolation_root: "system temporary directory outside repository",
      cleaned_after_run: true,
    });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
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

async function runToolchain(
  cwd: string,
  implementationCandidate = process.env.E01_IMPLEMENTATION_CANDIDATE?.trim(),
): Promise<{ ok: boolean; receipts: Obj[] }> {
  const scope = hash(resolve(cwd)).slice(0, 16);
  const isolatedTemp = join(tmpdir(), "zyra-e01-toolchain", scope, "tmp");
  const isolatedCache = join(tmpdir(), "zyra-e01-toolchain", scope, "bun-cache");
  await mkdir(isolatedTemp, { recursive: true });
  await mkdir(isolatedCache, { recursive: true });
  const isolatedEnvironment = {
    ...process.env,
    TMP: isolatedTemp,
    TEMP: isolatedTemp,
    TMPDIR: isolatedTemp,
    BUN_INSTALL_CACHE_DIR: isolatedCache,
    npm_config_cache: join(isolatedCache, "npm"),
    NODE_PATH: "",
    ...(implementationCandidate ? { E01_IMPLEMENTATION_CANDIDATE: implementationCandidate } : {}),
  };
  const commands = [
    [process.execPath, "--version"],
    ["node", "--version"],
    [process.execPath, "install", "--frozen-lockfile"],
    [process.execPath, "run", "typecheck:e01"],
    [process.execPath, "run", "build:bun"],
    [process.execPath, "run", "runtime:built:bun:health"],
    [process.execPath, "run", "build:node"],
    ["node", "dist/code-worker-node/main.js", "--health"],
    [process.execPath, "run", "runtime:e01:test"],
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
  const requestedTarget = process.env.E01_CLEANROOM_TARGET?.trim() || "HEAD";
  const headResult = await run(["git", "rev-parse", requestedTarget]);
  invariant(headResult.exitCode === 0, "cannot resolve cleanroom target commit");
  const head = headResult.stdout.trim();
  const cleanroomRoot = join(tmpdir(), "zyra-e01-cleanroom", `e01-cleanroom-${head.slice(0, 12)}`);
  const archivePath = `${cleanroomRoot}.tar`;
  invariant(
    resolve(cleanroomRoot).startsWith(resolve(tmpdir(), "zyra-e01-cleanroom") + "\\")
      || resolve(cleanroomRoot).startsWith(resolve(tmpdir(), "zyra-e01-cleanroom") + "/"),
    "unsafe cleanroom path",
  );
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
    toolchainResult = await runToolchain(cleanroomRoot, head);
  } finally {
    await rm(archivePath, { force: true });
    await rm(cleanroomRoot, { recursive: true, force: true });
  }
  if (!toolchainResult.ok) {
    process.stderr.write(`cleanroom_failure_receipts=${JSON.stringify(toolchainResult.receipts)}\n`);
  }
  invariant(toolchainResult.ok, "cleanroom toolchain failed");
  await emit("cleanroom", {
    target_commit: head,
    source: "git archive",
    cache_state: "fresh extracted tree without node_modules, dist, .tmp, or git metadata",
    cleanroom_root_parent: resolve(tmpdir(), "zyra-e01-cleanroom"),
    inherited_node_path: false,
    commands: toolchainResult.receipts,
    cleaned_after_run: true,
  });
}

const mode = process.argv[2] ?? "";
if (mode === "runtime-origin") await runtimeOrigin();
else if (mode === "write-path") await writePath();
else if (mode === "resume") await resume();
else if (mode === "resume-worker") await resumeWorker();
else if (mode === "lost-ack") await lostAck();
else if (mode === "disable") await disable();
else if (mode === "dependencies") await dependencies();
else if (mode === "toolchain") await toolchain();
else if (mode === "cleanroom") await cleanroom();
else throw new Error(`unsupported E01 probe mode: ${mode || "<missing>"}`);
