#!/usr/bin/env bun

import { spawn } from "node:child_process";
import { mkdirSync, rmSync } from "node:fs";
import { createInterface } from "node:readline";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

type Json = Record<string, any>;

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const builtEntry = join(repoRoot, "dist/code-worker-node/main.js");
const stateRoot = join(repoRoot, ".tmp", "m1-r01-e03-probe-state");

class ControlClient {
  private readonly process;
  private readonly lines;
  private readonly pending: Array<{ resolve: (value: Json) => void; reject: (error: Error) => void }> = [];

  constructor(disabled = false) {
    this.process = spawn(process.execPath, [builtEntry, "--e03-control"], {
      cwd: repoRoot,
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, ZYRA_E03_STATE_ROOT: stateRoot, ZYRA_E03_DISABLED: disabled ? "1" : "0" },
    });
    this.lines = createInterface({ input: this.process.stdout! });
    this.lines.on("line", (line) => {
      const waiter = this.pending.shift();
      if (!waiter) return;
      try { waiter.resolve(JSON.parse(line) as Json); } catch (error) { waiter.reject(error as Error); }
    });
    this.process.on("exit", (code) => {
      while (this.pending.length) this.pending.shift()!.reject(new Error(`control process exited ${code}`));
    });
  }

  request(command: Json): Promise<Json> {
    return new Promise((resolveRequest, rejectRequest) => {
      this.pending.push({ resolve: resolveRequest, reject: rejectRequest });
      this.process.stdin!.write(`${JSON.stringify(command)}\n`);
    });
  }

  async close(): Promise<void> {
    this.process.stdin!.end();
    await new Promise<void>((resolveClose) => this.process.once("exit", () => resolveClose()));
  }
}

function envelope(id: string, command: string, body: Json = {}): Json {
  return {
    schema_version: "3.0", request_id: id, idempotency_key: id, run_id: "probe-run",
    session_id: "probe-session", parent_task_id: "probe-parent", expected_revision: body.expected_revision ?? 0,
    command, body: { ...body, expected_revision: undefined },
  };
}

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

async function runtimeOrigin(): Promise<Json> {
  const client = new ControlClient();
  try {
    const result = await client.request(envelope("origin-create", "agent.create", { task_id: "origin-task", prompt: "inspect runtime origin", agent: "general" }));
    assert(result.ok === true, `create failed: ${JSON.stringify(result)}`);
    assert(result.runtime_origin === "typescript.E03AgentControlCoordinator", "runtime origin is not TypeScript E03 coordinator");
    assert(result.python_logical_owner === false, "Python logical owner was reported");
    return { probe: "runtime-origin", passed: true, result };
  } finally { await client.close(); }
}

async function writePath(): Promise<Json> {
  const client = new ControlClient();
  try {
    const created = await client.request(envelope("write-create", "agent.create", { task_id: "write-task", prompt: "prepare write path", agent: "general" }));
    const steered = await client.request(envelope("write-steer", "agent.steer", { task_id: "write-task", message: "continue", expected_revision: created.revision }));
    assert(steered.ok && steered.phase === "ack", `steer did not ACK: ${JSON.stringify(steered)}`);
    assert(steered.revision > created.revision, "control ACK did not advance backend revision");
    assert(steered.state?.messages?.some((item: Json) => item.body === "continue"), "control ACK did not mutate backend session state");
    assert(steered.commit_protocol?.join("/") === "prepare/effect/receipt/commit/ack", "commit phases are missing");
    return { probe: "write-path", passed: true, created, steered };
  } finally { await client.close(); }
}

async function resume(): Promise<Json> {
  const first = new ControlClient();
  const created = await first.request(envelope("resume-create", "agent.create", { task_id: "resume-task", prompt: "persist me", agent: "general" }));
  await first.close();
  const second = new ControlClient();
  try {
    const restored = await second.request(envelope("resume-status", "agent.status", { task_id: "resume-task", expected_revision: created.revision }));
    assert(restored.ok && restored.restored === true, `restart did not restore task: ${JSON.stringify(restored)}`);
    assert(restored.state.task_id === "resume-task" && restored.revision === created.revision, "restored identity/revision mismatch");
    return { probe: "resume", passed: true, created, restored };
  } finally { await second.close(); }
}

async function lostAck(): Promise<Json> {
  const first = new ControlClient();
  const request = envelope("lost-ack-create", "agent.create", { task_id: "lost-ack-task", prompt: "dedupe me", agent: "general" });
  const committed = await first.request({ ...request, simulate_lost_ack: true });
  assert(committed.ok === false && committed.error === "simulated_lost_ack", "lost ACK simulation did not commit before disconnect");
  await first.close();
  const second = new ControlClient();
  try {
    const replay = await second.request(request);
    assert(replay.ok && replay.replayed === true, `lost ACK replay did not recover: ${JSON.stringify(replay)}`);
    assert(replay.dispatch_count === 1, "lost ACK replay dispatched twice");
    const stale = await second.request(envelope("lost-ack-stale", "agent.cancel", { task_id: "lost-ack-task", expected_revision: 0 }));
    assert(stale.ok === false && stale.error === "stale_revision", "stale writer was not rejected");
    return { probe: "lost-ack", passed: true, committed, replay, stale };
  } finally { await second.close(); }
}

async function disable(): Promise<Json> {
  const client = new ControlClient(true);
  try {
    const result = await client.request(envelope("disable-create", "agent.create", { task_id: "disable-task", prompt: "must fail", agent: "general" }));
    assert(result.ok === false && result.error === "typescript_agent_control_disabled", `disabled E03 did not fail closed: ${JSON.stringify(result)}`);
    assert(result.python_fallback_attempted === false, "disabled E03 attempted Python fallback");
    return { probe: "disable", passed: true, result };
  } finally { await client.close(); }
}

async function main(): Promise<void> {
  const mode = process.argv[2];
  if (!mode) throw new Error("usage: bun scripts/remediation/probe_m1_r01_e03.ts <runtime-origin|write-path|resume|lost-ack|disable|all>");
  if (stateRoot === repoRoot || !stateRoot.startsWith(join(repoRoot, ".tmp"))) throw new Error(`unsafe probe state root ${stateRoot}`);
  rmSync(stateRoot, { recursive: true, force: true }); mkdirSync(stateRoot, { recursive: true });
  const selected = mode === "all" ? [runtimeOrigin, writePath, resume, lostAck, disable] : [{ "runtime-origin": runtimeOrigin, "write-path": writePath, resume, "lost-ack": lostAck, disable }[mode]];
  if (selected.some((probe) => !probe)) throw new Error(`unknown probe ${mode}`);
  const results: Json[] = [];
  for (const probe of selected) results.push(await probe!());
  process.stdout.write(`${JSON.stringify({ schema_version: "3.0", execution_id: "E03", mode, results }, null, 2)}\n`);
}

await main();
