#!/usr/bin/env bun

import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import type { JsonObject, RuntimeRunInput } from "../../packages/runtime/claude-runtime/src/contracts.ts";
import {
  E02CapabilityCoordinator,
  type E02CapabilityCoordinatorSnapshot,
} from "../../packages/runtime/claude-runtime/src/e02/coordinator.ts";
import {
  McpRequestJournal,
  type McpRequestIdentity,
} from "../../packages/integrations/claude-mcp/src/index.ts";

type Obj = Record<string, unknown>;

interface EpochMarker extends Obj {
  stage: number;
  process_id: number;
  runtime_epoch: number;
  restored_before_bootstrap: boolean;
  journal_sequence: number;
  event_sequence: number;
  transition_ids: string[];
  execution_ids: string[];
  stable_execution_id: string;
  stable_replayed: boolean;
  snapshot_path: string;
  snapshot_sha256: string;
  snapshot_hash: string;
}

interface KilledEpoch {
  marker: EpochMarker;
  exitCode: number;
  forceKilled: boolean;
  stdout: string;
  stderr: string;
}

const repoRoot = resolve(import.meta.dir, "..", "..");
const evidenceRoot = join(repoRoot, "docs", "reviews", "evidence", "M1-R01-v3", "execution-02");
const probePath = resolve(import.meta.dir, "probe_m1_r01_e02.ts");

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function invariant(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

async function emit(name: string, payload: Obj): Promise<void> {
  const output = {
    ...payload,
    schema_version: "3.0",
    execution_id: "E02",
    probe_id: `e02.probe.${name}`,
    generated_at_utc: new Date().toISOString(),
    ok: true,
  };
  await mkdir(evidenceRoot, { recursive: true });
  await writeFile(join(evidenceRoot, `${name}-result.json`), `${JSON.stringify(output, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify(output)}\n`);
}

function runtimeInput(
  workspaceRoot: string,
  snapshot: E02CapabilityCoordinatorSnapshot | null,
): RuntimeRunInput {
  return {
    runId: "e02-probe-run",
    taskId: "e02-probe-task",
    nodeId: "e02-probe-node",
    workerRequestId: "e02-probe-worker",
    sessionId: "e02-probe-session",
    messages: [],
    turns: [],
    tools: [],
    config: {
      permissionPolicy: { mode: "default", default_effect: "allow" },
      runtimeConstraints: {
        workspaceRoot,
        watchSkills: false,
        watchPlugins: false,
      },
    },
    restoredState: snapshot as unknown as JsonObject | null,
    metadata: {
      session_revision: 11,
      probe_nonce: process.env.E02_REVIEWER_NONCE ?? "implementation-probe",
    },
  };
}

async function resumeWorker(): Promise<void> {
  const stage = Number(process.argv[3]);
  const workspaceRoot = process.argv[4] ?? "";
  const inputPath = process.argv[5] ?? "";
  const outputPath = process.argv[6] ?? "";
  const markerPath = process.argv[7] ?? "";
  invariant(Number.isInteger(stage) && stage >= 0 && stage <= 2, "invalid E02 resume stage");
  invariant(workspaceRoot && outputPath && markerPath, "E02 resume worker paths are incomplete");
  const prior = stage === 0
    ? null
    : JSON.parse(await readFile(inputPath, "utf8")) as E02CapabilityCoordinatorSnapshot;
  const coordinator = await E02CapabilityCoordinator.open(runtimeInput(workspaceRoot, prior));
  const stable = await coordinator.execute("e02_health", {
    idempotency_key: "e02-probe-stable-effect",
  }, {
    toolCallId: "e02-probe-stable-call",
    sessionRevision: 11,
    namespace: "control",
    operation: "read",
  });
  await coordinator.execute("e02_health", {
    idempotency_key: `e02-probe-epoch-${stage}`,
  }, {
    toolCallId: `e02-probe-epoch-call-${stage}`,
    sessionRevision: 11,
    namespace: "control",
    operation: "read",
  });
  const snapshot = coordinator.snapshot();
  const encoded = `${JSON.stringify(snapshot, null, 2)}\n`;
  await writeFile(outputPath, encoded, "utf8");
  const marker: EpochMarker = {
    stage,
    process_id: process.pid,
    runtime_epoch: snapshot.runtime.epoch,
    restored_before_bootstrap: snapshot.restoredBeforeBootstrap,
    journal_sequence: snapshot.journal.sequence,
    event_sequence: snapshot.events.sequence,
    transition_ids: snapshot.journal.committed.map((item) => item.transitionId).sort(),
    execution_ids: snapshot.executionLedger.executions.map((item) => item.executionId).sort(),
    stable_execution_id: stable.executionId,
    stable_replayed: stable.replayed,
    snapshot_path: outputPath,
    snapshot_sha256: sha256(encoded),
    snapshot_hash: snapshot.snapshotHash,
  };
  await writeFile(markerPath, `${JSON.stringify(marker, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify({ ready_for_forced_termination: true, ...marker })}\n`);
  await Bun.sleep(60_000);
  throw new Error("E02 resume worker was not force-terminated");
}

async function killedEpoch(
  stage: number,
  workspaceRoot: string,
  inputPath: string | null,
  outputPath: string,
  markerPath: string,
): Promise<KilledEpoch> {
  const child = Bun.spawn({
    cmd: [
      process.execPath,
      probePath,
      "resume-worker",
      String(stage),
      workspaceRoot,
      inputPath ?? "-",
      outputPath,
      markerPath,
    ],
    cwd: repoRoot,
    env: { ...process.env, NO_COLOR: "1", FORCE_COLOR: "0" },
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
  });
  const stdoutPromise = new Response(child.stdout).text();
  const stderrPromise = new Response(child.stderr).text();
  let marker: EpochMarker | null = null;
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    try {
      marker = JSON.parse(await readFile(markerPath, "utf8")) as EpochMarker;
      break;
    } catch {
      const exited = await Promise.race([
        child.exited.then(() => true),
        Bun.sleep(10).then(() => false),
      ]);
      if (exited) break;
    }
  }
  if (!marker) {
    child.kill();
    const exitCode = await child.exited;
    throw new Error(`E02 resume worker ${stage} did not persist a marker; exit=${exitCode}; stderr=${(await stderrPromise).slice(-2_000)}`);
  }
  child.kill();
  const exitCode = await child.exited;
  const stdout = await stdoutPromise;
  const stderr = await stderrPromise;
  invariant(exitCode !== 0, `E02 resume worker ${stage} exited normally instead of being killed`);
  invariant(marker.process_id === child.pid, `E02 resume worker ${stage} reported the wrong PID`);
  return { marker, exitCode, forceKilled: true, stdout, stderr };
}

function added(current: string[], prior: string[]): string[] {
  const existing = new Set(prior);
  return current.filter((value) => !existing.has(value));
}

async function resume(): Promise<void> {
  const root = join(tmpdir(), `zyra-e02-resume-${randomUUID()}`);
  const workspaceRoot = join(root, "workspace");
  await mkdir(workspaceRoot, { recursive: true });
  const snapshots = [0, 1, 2].map((stage) => join(root, `snapshot-${stage}.json`));
  const markers = [0, 1, 2].map((stage) => join(root, `marker-${stage}.json`));
  const epochs: KilledEpoch[] = [];
  try {
    epochs.push(await killedEpoch(0, workspaceRoot, null, snapshots[0]!, markers[0]!));
    epochs.push(await killedEpoch(1, workspaceRoot, snapshots[0]!, snapshots[1]!, markers[1]!));
    epochs.push(await killedEpoch(2, workspaceRoot, snapshots[1]!, snapshots[2]!, markers[2]!));
    const values = epochs.map((item) => item.marker);
    invariant(new Set(values.map((item) => item.process_id)).size === 3, "E02 resume reused an owner PID");
    invariant(JSON.stringify(values.map((item) => item.runtime_epoch)) === JSON.stringify([1, 2, 3]), "E02 runtime epochs did not advance 1/2/3");
    invariant(JSON.stringify(values.map((item) => item.restored_before_bootstrap)) === JSON.stringify([false, true, true]), "E02 restore did not precede bootstrap");
    invariant(JSON.stringify(values.map((item) => item.stable_replayed)) === JSON.stringify([false, true, true]), "E02 committed execution was not replayed exactly");
    invariant(new Set(values.map((item) => item.stable_execution_id)).size === 1, "E02 lost-ACK retry created another execution");
    for (let index = 1; index < values.length; index += 1) {
      invariant(values[index]!.journal_sequence > values[index - 1]!.journal_sequence, "E02 journal sequence did not advance");
      invariant(values[index]!.event_sequence > values[index - 1]!.event_sequence, "E02 event sequence did not advance");
      const transitionDelta = added(values[index]!.transition_ids, values[index - 1]!.transition_ids);
      const executionDelta = added(values[index]!.execution_ids, values[index - 1]!.execution_ids);
      invariant(transitionDelta.length === 1, `E02 epoch ${index} added ${transitionDelta.length} transitions instead of one`);
      invariant(executionDelta.length === 1, `E02 epoch ${index} added ${executionDelta.length} executions instead of one`);
    }
    await emit("same-session-resume", {
      session_id: "e02-probe-session",
      session_revision: 11,
      process_restart_count: 2,
      process_ids: values.map((item) => item.process_id),
      force_killed_processes: epochs.map((item) => item.forceKilled),
      runtime_epochs: values.map((item) => item.runtime_epoch),
      journal_sequences: values.map((item) => item.journal_sequence),
      event_sequences: values.map((item) => item.event_sequence),
      stable_execution_id: values[0]!.stable_execution_id,
      stable_replayed: values.map((item) => item.stable_replayed),
      repeated_effect_count: 0,
      worker_receipts: epochs.map((item) => ({
        exit_code: item.exitCode,
        process_id: item.marker.process_id,
        snapshot_sha256: item.marker.snapshot_sha256,
        snapshot_hash: item.marker.snapshot_hash,
        stdout_sha256: sha256(item.stdout),
        stderr_sha256: sha256(item.stderr),
      })),
      isolation_root: "system temporary directory outside repository",
      cleaned_after_run: true,
    });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

function journalInput(nonce: string) {
  const identity: McpRequestIdentity = {
    runId: `run-${nonce}`,
    taskId: `task-${nonce}`,
    sessionId: `session-${nonce}`,
    sessionRevision: 13,
    workerRequestId: `worker-${nonce}`,
    toolCallId: `call-${nonce}`,
    serverId: `server-${nonce}`,
    connectionId: `connection-${nonce}`,
    connectionEpoch: 4,
    requestId: `request-${nonce}`,
    method: "tools/call",
  };
  return {
    identity,
    message: {
      jsonrpc: "2.0" as const,
      id: identity.requestId,
      method: identity.method,
      params: { name: "mutate_state", arguments: { nonce } },
    },
    arguments: { nonce },
    idempotent: true,
    idempotencyKey: `idempotency-${nonce}`,
    metadata: { execution_id: "E02", reviewer_nonce: nonce },
  };
}

async function lostAck(): Promise<void> {
  const nonce = (process.env.E02_REVIEWER_NONCE ?? randomUUID()).slice(0, 24);
  const input = journalInput(nonce);
  const journal = new McpRequestJournal();
  const prepared = journal.prepare(input);
  journal.markSent(prepared.journalId, prepared.transitionId, 4);
  const effect = journal.recordEffect({
    journalId: prepared.journalId,
    transitionId: prepared.transitionId,
    effectKind: "remote_mutation",
    effectKey: `mutation:${nonce}`,
    payload: { revision: 1, nonce },
    reversible: false,
    metadata: { reviewer_nonce: nonce },
  });
  journal.commit({
    journalId: prepared.journalId,
    transitionId: prepared.transitionId,
    status: "committed",
    response: { jsonrpc: "2.0", id: input.identity.requestId, result: { revision: 1, nonce } },
    failure: null,
    metadata: { effect_receipt_id: effect.receiptId },
  });
  const restored = new McpRequestJournal({ snapshot: journal.snapshot() });
  const replay = restored.prepare(input);
  invariant(replay.status === "committed", "lost-ACK retry did not return the committed response");
  invariant(replay.journalId === prepared.journalId, "lost-ACK retry created another journal record");
  invariant(restored.effectsFor(prepared.journalId).length === 1, "lost-ACK retry duplicated the remote effect");
  await emit("lost-ack", {
    reviewer_nonce: nonce,
    journal_id: prepared.journalId,
    transition_id: prepared.transitionId,
    effect_receipt_id: effect.receiptId,
    replay_status: replay.status,
    effect_count_after_restore: restored.effectsFor(prepared.journalId).length,
    repeated_effect_count: 0,
  });
}

async function runtimeOrigin(): Promise<void> {
  const workspaceRoot = join(tmpdir(), `zyra-e02-origin-${randomUUID()}`);
  await mkdir(workspaceRoot, { recursive: false });
  try {
    const coordinator = await E02CapabilityCoordinator.open(runtimeInput(workspaceRoot, null));
    const receipt = await coordinator.execute("e02_health", {}, {
      toolCallId: "e02-origin-call",
      sessionRevision: 11,
      operation: "read",
    });
    const snapshot = coordinator.snapshot();
    invariant(receipt.owner === "typescript-e02-control", "E02 default write path owner is not TypeScript");
    invariant(snapshot.journal.committed.some((item) => item.transitionId === receipt.transitionId), "E02 transition was not committed");
    await coordinator.close();
    await emit("runtime-origin", {
      default_entry: "CodeWorkerApplication.runTaskRuntime -> PermissionedCapabilityHost.executeBatch -> E02CapabilityCoordinator.execute",
      canonical_owner: receipt.owner,
      transition_id: receipt.transitionId,
      capability_execution_id: receipt.executionId,
      runtime_epoch: snapshot.runtime.epoch,
      journal_sequence: snapshot.journal.sequence,
      python_decision_fallback: false,
    });
    await emit("write-path", {
      state_store: "E02CapabilityCoordinator.journal + CapabilityExecutionLedger",
      transition_id: receipt.transitionId,
      capability_execution_id: receipt.executionId,
      commit_revision_after: receipt.commit.revisionAfter,
      snapshot_hash: snapshot.snapshotHash,
      canonical_owner: receipt.owner,
    });
  } finally {
    await rm(workspaceRoot, { recursive: true, force: true });
  }
}

async function disableWorker(): Promise<void> {
  const input = runtimeInput(process.cwd(), null);
  await E02CapabilityCoordinator.open(input);
}

async function disable(): Promise<void> {
  const child = Bun.spawn({
    cmd: [process.execPath, probePath, "disable-worker"],
    cwd: repoRoot,
    env: { ...process.env, ZYRA_DISABLE_E02_TYPESCRIPT_RUNTIME: "1" },
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
  });
  const stdoutPromise = new Response(child.stdout).text();
  const stderrPromise = new Response(child.stderr).text();
  const exitCode = await child.exited;
  const stdout = await stdoutPromise;
  const stderr = await stderrPromise;
  invariant(exitCode !== 0, "E02 disabled owner unexpectedly opened");
  invariant(`${stdout}\n${stderr}`.includes("e02_typescript_runtime_disabled"), "E02 disable failure did not originate from the canonical owner gate");
  await emit("disable", {
    disabled_component: "E02CapabilityCoordinator.open",
    environment_gate: "ZYRA_DISABLE_E02_TYPESCRIPT_RUNTIME=1",
    default_owner_failed: true,
    python_fallback_observed: false,
    exit_code: exitCode,
    stdout_sha256: sha256(stdout),
    stderr_sha256: sha256(stderr),
  });
}

const mode = process.argv[2] ?? "";
if (mode === "runtime-origin" || mode === "write-path") await runtimeOrigin();
else if (mode === "resume") await resume();
else if (mode === "resume-worker") await resumeWorker();
else if (mode === "lost-ack") await lostAck();
else if (mode === "disable") await disable();
else if (mode === "disable-worker") await disableWorker();
else if (mode === "all") {
  await runtimeOrigin();
  await resume();
  await lostAck();
  await disable();
} else {
  throw new Error(`unsupported E02 probe mode: ${mode || "<missing>"}`);
}
