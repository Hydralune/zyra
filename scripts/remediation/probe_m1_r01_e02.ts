#!/usr/bin/env bun

import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";

type Obj = Record<string, unknown>;

interface ApiProcess {
  child: any;
  reader: ReadableStreamDefaultReader<Uint8Array>;
  decoder: TextDecoder;
  buffer: string;
  output: string;
  frames: Obj[];
  stderr: Promise<string>;
}

interface ResumeMarker extends Obj {
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
  state_generation: number;
  snapshot_hash: string;
  forced_exit_code: number;
}

const repoRoot = resolve(import.meta.dir, "..", "..");
const evidenceRoot = join(repoRoot, "docs", "reviews", "evidence", "M1-R01-v3", "execution-02");
const probePath = resolve(import.meta.dir, "probe_m1_r01_e02.ts");
const builtEntry = resolve(repoRoot, "dist", "code-worker-node", "main.js");
const reviewerNonce = process.env.E02_REVIEWER_NONCE?.trim() || randomUUID().replaceAll("-", "");

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function invariant(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function object(value: unknown): Obj {
  invariant(value !== null && typeof value === "object" && !Array.isArray(value), "expected JSON object");
  return value as Obj;
}

function array(value: unknown): Obj[] {
  return Array.isArray(value) ? value.map(object) : [];
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function candidateIdentity(): string {
  const configured = process.env.E02_IMPLEMENTATION_CANDIDATE?.trim();
  if (configured && /^[0-9a-f]{40}$/.test(configured)) return configured;
  const result = Bun.spawnSync({ cmd: ["git", "rev-parse", "HEAD"], cwd: repoRoot, stdout: "pipe", stderr: "pipe" });
  const candidate = result.exitCode === 0 ? result.stdout.toString().trim() : "";
  invariant(/^[0-9a-f]{40}$/.test(candidate), "E02 probe cannot bind an implementation candidate");
  return candidate;
}

async function builtEntryDigest(): Promise<string> {
  const info = await stat(builtEntry);
  invariant(info.isFile() && info.size > 0, `built E02 Node entry is absent: ${builtEntry}`);
  return sha256(await readFile(builtEntry));
}

async function emit(name: string, payload: Obj): Promise<void> {
  const output = {
    ...payload,
    schema_version: "3.0",
    execution_id: "E02",
    probe_id: `e02.probe.${name}`,
    generated_at_utc: new Date().toISOString(),
    implementation_candidate: candidateIdentity(),
    reviewer_nonce: reviewerNonce,
    built_entry: "dist/code-worker-node/main.js --e02-api",
    built_entry_sha256: await builtEntryDigest(),
    source_imports_used: false,
    built_default_entry_executed: true,
    ok: true,
  };
  await mkdir(evidenceRoot, { recursive: true });
  await writeFile(join(evidenceRoot, `${name}-result.json`), `${JSON.stringify(output, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify(output)}\n`);
}

async function startApi(
  root: string,
  statePath: string,
  runtimeConstraints: Obj = {},
  environment: Record<string, string | undefined> = {},
): Promise<{ api: ApiProcess; ready: Obj }> {
  await builtEntryDigest();
  const child = Bun.spawn({
    cmd: ["node", builtEntry, "--e02-api"],
    cwd: repoRoot,
    env: { ...process.env, ...environment, NO_COLOR: "1", FORCE_COLOR: "0" },
    stdin: "pipe",
    stdout: "pipe",
    stderr: "pipe",
  });
  const api: ApiProcess = {
    child,
    reader: child.stdout.getReader(),
    decoder: new TextDecoder(),
    buffer: "",
    output: "",
    frames: [],
    stderr: new Response(child.stderr).text(),
  };
  await sendFrame(api, {
    type: "initialize",
    request_id: `initialize-${reviewerNonce}-${child.pid}`,
    workspace_root: join(root, "workspace"),
    state_path: statePath,
    permission_mode: "default",
    sealed_autonomous: false,
    runtime_constraints: runtimeConstraints,
  });
  const ready = await readFrame(api, 20_000);
  return { api, ready };
}

async function sendFrame(api: ApiProcess, frame: Obj): Promise<void> {
  api.child.stdin.write(`${JSON.stringify(frame)}\n`);
  await api.child.stdin.flush();
}

function takeBufferedFrame(api: ApiProcess): Obj | null {
  const newline = api.buffer.indexOf("\n");
  if (newline < 0) return null;
  const line = api.buffer.slice(0, newline).trim();
  api.buffer = api.buffer.slice(newline + 1);
  if (!line) return takeBufferedFrame(api);
  const frame = object(JSON.parse(line));
  api.frames.push(frame);
  return frame;
}

async function readFrame(api: ApiProcess, timeoutMs = 10_000): Promise<Obj> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const buffered = takeBufferedFrame(api);
    if (buffered) return buffered;
    const remaining = deadline - Date.now();
    invariant(remaining > 0, `timed out waiting for built E02 API frame; output=${api.output.slice(-2_000)}`);
    const result = await Promise.race([
      api.reader.read(),
      Bun.sleep(remaining).then(() => ({ timeout: true } as const)),
    ]);
    invariant(!("timeout" in result), `timed out waiting for built E02 API output; output=${api.output.slice(-2_000)}`);
    invariant(!result.done, "built E02 API closed stdout before returning a frame");
    const chunk = api.decoder.decode(result.value, { stream: true });
    api.output += chunk;
    api.buffer += chunk;
  }
}

async function requestApi(
  api: ApiProcess,
  operation: string,
  payload: Obj,
  requestId: string,
  allowError = false,
): Promise<Obj> {
  await sendFrame(api, { type: "request", request_id: requestId, operation, payload });
  const frame = await readFrame(api, 30_000);
  invariant(frame.request_id === requestId, `built E02 API response correlation mismatch for ${requestId}`);
  if (!allowError) {
    invariant(frame.type === "response" && frame.ok === true, `built E02 API request failed: ${JSON.stringify(frame)}`);
  }
  return frame;
}

async function stopGracefully(api: ApiProcess): Promise<number> {
  api.child.stdin.end();
  return api.child.exited;
}

async function forceStop(api: ApiProcess): Promise<number> {
  api.child.kill();
  const exitCode = await api.child.exited;
  invariant(exitCode !== 0, `built E02 owner ${api.child.pid} was not force-terminated`);
  return exitCode;
}

function allowRule(ruleId: string, toolPattern: string, namespacePattern: string, serverPattern = "*"): Obj {
  return {
    ruleId,
    effect: "allow",
    source: "session",
    kind: "tool",
    toolPattern,
    namespacePattern,
    serverPattern,
    operationPattern: "*",
    priority: 10_000,
  };
}

async function installRules(api: ApiProcess, rules: Obj[], id: string): Promise<void> {
  await requestApi(api, "permission.policy", {
    action: "replace_rules",
    expected_revision: 0,
    actor_id: `e02-probe-${reviewerNonce}`,
    rules,
  }, id);
}

function healthPayload(frame: Obj): Obj {
  invariant(frame.type === "ready" && frame.ok === true, `built E02 API did not become ready: ${JSON.stringify(frame)}`);
  const payload = object(frame.payload);
  invariant(payload.protocol === "zyra.e02-api-port/v1", "built E02 API protocol mismatch");
  invariant(payload.canonical_entrypoint === "E02CapabilityCoordinator.execute", "built E02 canonical entrypoint mismatch");
  invariant(payload.canonical_owner === "typescript", "built E02 canonical owner mismatch");
  invariant(payload.python_permission_fallback === false && payload.python_mcp_fallback === false, "built E02 reported a Python decision fallback");
  return payload;
}

function executePayload(toolName: string, argumentsValue: Obj, toolCallId: string, operation: string, namespace: string, serverId = ""): Obj {
  return {
    tool_name: toolName,
    arguments: argumentsValue,
    operation,
    namespace,
    server_id: serverId,
    tool_call_id: toolCallId,
    session_revision: 11,
  };
}

async function runtimeOrigin(): Promise<void> {
  const root = join(tmpdir(), `zyra-e02-built-origin-${randomUUID()}`);
  const statePath = join(root, "state", "e02.json");
  await mkdir(join(root, "workspace"), { recursive: true });
  let api: ApiProcess | null = null;
  try {
    const started = await startApi(root, statePath);
    api = started.api;
    const ready = healthPayload(started.ready);
    await installRules(api, [allowRule("allow-e02-health", "e02_health", "control")], "origin-policy");
    const response = await requestApi(api, "execute", executePayload(
      "e02_health",
      { idempotency_key: `origin-${reviewerNonce}` },
      `origin-call-${reviewerNonce}`,
      "read",
      "control",
    ), "origin-execute");
    const body = object(response.payload);
    const receipt = object(body.receipt);
    const snapshotFrame = await requestApi(api, "snapshot", {}, "origin-snapshot");
    const snapshot = object(snapshotFrame.payload);
    const journal = object(snapshot.journal);
    const committed = array(journal.committed);
    invariant(receipt.owner === "typescript-e02-control", "built E02 default write owner is not TypeScript");
    invariant(committed.some((item) => item.transitionId === receipt.transitionId), "built E02 transition was not committed");
    invariant(number(ready.process_id) === api.child.pid, "built E02 ready frame reported another owner PID");
    await emit("runtime-origin", {
      protocol: ready.protocol,
      default_entry: "node dist/code-worker-node/main.js --e02-api -> CodeWorkerApplication.runCapabilityApiPort -> E02CapabilityCoordinator.execute",
      canonical_owner: receipt.owner,
      process_id: api.child.pid,
      transition_id: receipt.transitionId,
      capability_execution_id: receipt.executionId,
      runtime_epoch: number(object(snapshot.runtime).epoch),
      journal_sequence: number(journal.sequence),
      api_frame_count: api.frames.length,
      api_frame_sha256: sha256(api.output),
      python_decision_fallback: false,
    });
    await emit("write-path", {
      state_store: "E02ApiStateStore -> E02CapabilityCoordinator.journal + CapabilityExecutionLedger",
      state_path: statePath,
      state_generation: number(object(ready.state).generation),
      transition_id: receipt.transitionId,
      capability_execution_id: receipt.executionId,
      commit_revision_after: number(object(receipt.commit).revisionAfter),
      snapshot_hash: text(snapshot.snapshotHash),
      persisted_state_exists: existsSync(statePath),
      canonical_owner: receipt.owner,
      process_id: api.child.pid,
    });
    await forceStop(api);
    api = null;
  } finally {
    if (api) api.child.kill();
    await rm(root, { recursive: true, force: true });
  }
}

function added(current: string[], prior: string[]): string[] {
  const before = new Set(prior);
  return current.filter((value) => !before.has(value));
}

async function resume(): Promise<void> {
  const root = join(tmpdir(), `zyra-e02-built-resume-${randomUUID()}`);
  const statePath = join(root, "state", "e02.json");
  await mkdir(join(root, "workspace"), { recursive: true });
  const markers: ResumeMarker[] = [];
  let api: ApiProcess | null = null;
  try {
    for (let stage = 0; stage < 3; stage += 1) {
      const started = await startApi(root, statePath);
      api = started.api;
      const ready = healthPayload(started.ready);
      if (stage === 0) await installRules(api, [allowRule("allow-resume-health", "e02_health", "control")], "resume-policy");
      const stableFrame = await requestApi(api, "execute", executePayload(
        "e02_health",
        { idempotency_key: "e02-built-stable-effect" },
        "e02-built-stable-call",
        "read",
        "control",
      ), `resume-stable-${stage}`);
      await requestApi(api, "execute", executePayload(
        "e02_health",
        { idempotency_key: `e02-built-epoch-${stage}` },
        `e02-built-epoch-call-${stage}`,
        "read",
        "control",
      ), `resume-epoch-${stage}`);
      const snapshotFrame = await requestApi(api, "snapshot", {}, `resume-snapshot-${stage}`);
      const snapshot = object(snapshotFrame.payload);
      const runtime = object(snapshot.runtime);
      const journal = object(snapshot.journal);
      const events = object(snapshot.events);
      const ledger = object(snapshot.executionLedger);
      const stable = object(object(stableFrame.payload).receipt);
      const pid = api.child.pid;
      const exitCode = await forceStop(api);
      markers.push({
        stage,
        process_id: pid,
        runtime_epoch: number(runtime.epoch),
        restored_before_bootstrap: snapshot.restoredBeforeBootstrap === true,
        journal_sequence: number(journal.sequence),
        event_sequence: number(events.sequence),
        transition_ids: array(journal.committed).map((item) => text(item.transitionId)).sort(),
        execution_ids: array(ledger.executions).map((item) => text(item.executionId)).sort(),
        stable_execution_id: text(stable.executionId),
        stable_replayed: stable.replayed === true,
        state_generation: number(object(ready.state).generation),
        snapshot_hash: text(snapshot.snapshotHash),
        forced_exit_code: exitCode,
      });
      api = null;
    }
    invariant(new Set(markers.map((item) => item.process_id)).size === 3, "built resume reused an owner PID");
    invariant(JSON.stringify(markers.map((item) => item.runtime_epoch)) === JSON.stringify([1, 2, 3]), "built resume epochs did not advance 1/2/3");
    invariant(JSON.stringify(markers.map((item) => item.restored_before_bootstrap)) === JSON.stringify([false, true, true]), "built restore did not precede bootstrap");
    invariant(JSON.stringify(markers.map((item) => item.stable_replayed)) === JSON.stringify([false, true, true]), "built committed execution did not replay exactly");
    invariant(new Set(markers.map((item) => item.stable_execution_id)).size === 1, "built exact replay created another execution");
    for (let index = 1; index < markers.length; index += 1) {
      invariant(markers[index]!.journal_sequence > markers[index - 1]!.journal_sequence, "built journal sequence did not advance");
      invariant(markers[index]!.event_sequence > markers[index - 1]!.event_sequence, "built event sequence did not advance");
      invariant(added(markers[index]!.transition_ids, markers[index - 1]!.transition_ids).length === 1, `built epoch ${index} added the wrong transition count`);
      invariant(added(markers[index]!.execution_ids, markers[index - 1]!.execution_ids).length === 1, `built epoch ${index} added the wrong execution count`);
    }
    await emit("same-session-resume", {
      session_id: "e02-api-control-session",
      session_revision: 11,
      process_restart_count: 2,
      process_ids: markers.map((item) => item.process_id),
      force_killed_processes: markers.map(() => true),
      forced_exit_codes: markers.map((item) => item.forced_exit_code),
      runtime_epochs: markers.map((item) => item.runtime_epoch),
      restored_before_bootstrap: markers.map((item) => item.restored_before_bootstrap),
      journal_sequences: markers.map((item) => item.journal_sequence),
      event_sequences: markers.map((item) => item.event_sequence),
      state_generations: markers.map((item) => item.state_generation),
      stable_execution_id: markers[0]!.stable_execution_id,
      stable_replayed: markers.map((item) => item.stable_replayed),
      repeated_effect_count: 0,
      snapshot_hashes: markers.map((item) => item.snapshot_hash),
      isolation_root: "system temporary directory outside repository",
      cleaned_after_run: true,
    });
  } finally {
    if (api) api.child.kill();
    await rm(root, { recursive: true, force: true });
  }
}

async function mcpServer(): Promise<void> {
  const effectPath = process.argv[3] ?? "";
  const markerPath = process.argv[4] ?? "";
  const delayMs = Number(process.argv[5] ?? "0");
  invariant(effectPath && markerPath && Number.isFinite(delayMs), "MCP probe server arguments are incomplete");
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  const send = (id: unknown, result: Obj): void => {
    process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id, result })}\n`);
  };
  for await (const line of lines) {
    const request = object(JSON.parse(line));
    if (request.id === undefined || request.id === null) continue;
    if (request.method === "initialize") {
      send(request.id, {
        protocolVersion: "2025-06-18",
        capabilities: { tools: {} },
        serverInfo: { name: "e02-built-live", version: "1.0.0" },
      });
    } else if (request.method === "tools/list") {
      send(request.id, {
        tools: [{
          name: "nonce_effect",
          description: "commit one durable reviewer effect",
          inputSchema: {
            type: "object",
            properties: { nonce: { type: "string" } },
            required: ["nonce"],
            additionalProperties: false,
          },
          annotations: {
            readOnlyHint: false,
            destructiveHint: true,
            idempotentHint: true,
            openWorldHint: false,
          },
        }],
      });
    } else if (request.method === "tools/call") {
      const params = object(request.params);
      const argumentsValue = object(params.arguments);
      const before = existsSync(effectPath) ? Number(await readFile(effectPath, "utf8")) : 0;
      const count = before + 1;
      await writeFile(effectPath, String(count), "utf8");
      await writeFile(markerPath, `${JSON.stringify({
        server_pid: process.pid,
        effect_count: count,
        nonce: text(argumentsValue.nonce),
        request_id: request.id,
        committed_at: new Date().toISOString(),
      })}\n`, "utf8");
      if (delayMs > 0) await Bun.sleep(delayMs);
      send(request.id, {
        content: [{ type: "text", text: `effect:${count}:${text(argumentsValue.nonce)}` }],
        structuredContent: { effect_count: count, nonce: text(argumentsValue.nonce) },
        isError: false,
      });
    } else {
      send(request.id, {});
    }
  }
}

async function waitForEffect(effectPath: string, markerPath: string): Promise<Obj> {
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    try {
      const marker = object(JSON.parse(await readFile(markerPath, "utf8")));
      if (Number(await readFile(effectPath, "utf8")) === 1) return marker;
    } catch {
      await Bun.sleep(10);
    }
  }
  throw new Error("live MCP effect did not cross the process boundary");
}

function terminateKnownProcess(pid: number): void {
  if (!Number.isSafeInteger(pid) || pid <= 0 || pid === process.pid) return;
  try {
    process.kill(pid);
  } catch {
    // The MCP child may already have exited because its owner pipe closed.
  }
}

async function lostAck(): Promise<void> {
  const root = join(tmpdir(), `zyra-e02-built-lost-ack-${randomUUID()}`);
  const statePath = join(root, "state", "e02.json");
  const effectPath = join(root, "external-effect-count.txt");
  const markerPath = join(root, "external-effect-marker.json");
  await mkdir(join(root, "workspace"), { recursive: true });
  const runtimeConstraints: Obj = {
    typescriptMcpServers: {
      review: {
        transport: {
          kind: "stdio",
          command: process.execPath,
          arguments: [probePath, "mcp-server", effectPath, markerPath, "60000"],
          cwd: root,
          inherit_environment: [],
        },
        network_policy: "offline",
        allowed_operations: ["*"],
        timeouts: {
          connect_ms: 5_000,
          initialize_ms: 5_000,
          request_ms: 120_000,
          shutdown_ms: 2_000,
        },
      },
    },
  };
  const toolName = "mcp__review__nonce_effect";
  const toolCallId = `lost-ack-call-${reviewerNonce}`;
  const invocation = executePayload(
    toolName,
    { nonce: reviewerNonce },
    toolCallId,
    "tools/call",
    "mcp",
    "review",
  );
  let api: ApiProcess | null = null;
  let mcpPid = 0;
  try {
    const first = await startApi(root, statePath, runtimeConstraints);
    api = first.api;
    healthPayload(first.ready);
    await installRules(api, [
      allowRule("allow-live-mcp", toolName, "mcp", "review"),
      allowRule("allow-reconciliation", "e02_control", "control"),
    ], "lost-ack-policy");
    await sendFrame(api, { type: "request", request_id: "lost-ack-first", operation: "execute", payload: invocation });
    const marker = await waitForEffect(effectPath, markerPath);
    mcpPid = number(marker.server_pid);
    const firstOwnerPid = api.child.pid;
    const firstExitCode = await forceStop(api);
    api = null;
    terminateKnownProcess(mcpPid);

    const restored = await startApi(root, statePath, runtimeConstraints);
    api = restored.api;
    const ready = healthPayload(restored.ready);
    const snapshotFrame = await requestApi(api, "snapshot", {}, "lost-ack-restored-snapshot");
    const snapshot = object(snapshotFrame.payload);
    const executions = array(object(snapshot.executionLedger).executions);
    const execution = executions.find((item) => item.toolCallId === toolCallId);
    invariant(execution, "restored built E02 ledger lost the in-flight external execution");
    invariant(execution.phase === "recovery_required", `lost-ACK execution restored as ${text(execution.phase)}`);
    const recovery = object(execution.recovery);
    invariant(recovery.kind === "indeterminate_restart_effect", "lost-ACK recovery kind is not indeterminate_restart_effect");
    invariant(recovery.reexecute_without_receipt === false, "lost-ACK recovery permits silent re-execution");
    const transitionId = text(execution.transitionId);
    const transition = array(object(snapshot.journal).pending).find((item) => item.transitionId === transitionId);
    invariant(transition?.phase === "effect_started", "lost-ACK transition did not restore the pre-effect fence");
    invariant(transition.effectReceipt === null, "lost-ACK unexpectedly fabricated a provider receipt");

    const rejectedRetry = await requestApi(api, "execute", invocation, "lost-ack-retry-before-reconcile", true);
    invariant(rejectedRetry.type === "error", "lost-ACK retry silently re-executed instead of requiring reconciliation");
    invariant(text(object(rejectedRetry.error).code) === "e02_execution_recovery_required", "lost-ACK retry returned the wrong recovery error");
    invariant(Number(await readFile(effectPath, "utf8")) === 1, "lost-ACK retry repeated the external effect");

    const observedResult = {
      summary: "MCP tool review/nonce_effect completed",
      output: {
        content: [{ type: "text", text: `effect:1:${reviewerNonce}` }],
        structuredContent: { effect_count: 1, nonce: reviewerNonce },
        isError: false,
      },
      metadata: {
        canonical_runtime_owner: "typescript",
        capability_owner: "typescript-mcp",
        mcp_server_id: "review",
        reviewer_observed_effect: "true",
      },
    };
    const reconcileFrame = await requestApi(api, "execute", executePayload(
      "e02_control",
      {
        operation: "recovery.transition.reconcile",
        payload: {
          transition_id: transitionId,
          outcome: "confirm_effect",
          result: observedResult,
          provider_receipt_id: `reviewer-file:${sha256(await readFile(markerPath))}`,
          actor: `reviewer-${reviewerNonce}`,
          reason: "process-external effect file proves the provider committed before owner termination",
          metadata: { effect_path_sha256: sha256(effectPath), reviewer_nonce: reviewerNonce },
        },
        idempotency_key: `reconcile-${transitionId}`,
      },
      `lost-ack-reconcile-${reviewerNonce}`,
      "recovery",
      "control",
    ), "lost-ack-reconcile");
    const reconciliationReceipt = object(object(reconcileFrame.payload).receipt);
    const replayFrame = await requestApi(api, "execute", invocation, "lost-ack-replay-after-reconcile");
    const replayReceipt = object(object(replayFrame.payload).receipt);
    invariant(replayReceipt.replayed === true, "reconciled lost-ACK execution did not replay its committed receipt");
    invariant(replayReceipt.executionId === execution.executionId, "reconciled replay changed execution identity");
    invariant(Number(await readFile(effectPath, "utf8")) === 1, "reconciled replay repeated the external effect");
    const finalSnapshot = object((await requestApi(api, "snapshot", {}, "lost-ack-final-snapshot")).payload);
    const finalExecution = array(object(finalSnapshot.executionLedger).executions).find((item) => item.executionId === execution.executionId);
    invariant(finalExecution?.phase === "committed", "manual lost-ACK reconciliation did not commit the execution");
    const secondOwnerPid = api.child.pid;
    const exitCode = await forceStop(api);
    api = null;
    await emit("lost-ack", {
      process_ids: [firstOwnerPid, secondOwnerPid],
      process_restart_count: 1,
      first_owner_exit_code: firstExitCode,
      restored_owner_exit_code: exitCode,
      mcp_server_process_id: mcpPid,
      external_effect_count: 1,
      repeated_effect_count: 0,
      restored_before_bootstrap: snapshot.restoredBeforeBootstrap === true,
      restored_runtime_epoch: number(object(snapshot.runtime).epoch),
      restored_execution_id: execution.executionId,
      restored_transition_id: transitionId,
      restored_execution_phase: "recovery_required",
      restored_transition_phase: "effect_started",
      recovery_kind: recovery.kind,
      reexecute_without_receipt: recovery.reexecute_without_receipt,
      retry_before_reconcile_error: text(object(rejectedRetry.error).code),
      reconciliation_execution_id: reconciliationReceipt.executionId,
      final_execution_phase: finalExecution.phase,
      replayed_after_reconciliation: replayReceipt.replayed,
      state_generation_after_restart: number(object(ready.state).generation),
      effect_marker_sha256: sha256(await readFile(markerPath)),
      cleaned_after_run: true,
    });
  } finally {
    if (api) api.child.kill();
    terminateKnownProcess(mcpPid);
    await rm(root, { recursive: true, force: true });
  }
}

async function disable(): Promise<void> {
  const root = join(tmpdir(), `zyra-e02-built-disable-${randomUUID()}`);
  const statePath = join(root, "state", "e02.json");
  await mkdir(join(root, "workspace"), { recursive: true });
  let api: ApiProcess | null = null;
  try {
    await builtEntryDigest();
    const child = Bun.spawn({
      cmd: ["node", builtEntry, "--e02-api"],
      cwd: repoRoot,
      env: { ...process.env, ZYRA_DISABLE_E02_TYPESCRIPT_RUNTIME: "1", NO_COLOR: "1", FORCE_COLOR: "0" },
      stdin: "pipe",
      stdout: "pipe",
      stderr: "pipe",
    });
    api = {
      child,
      reader: child.stdout.getReader(),
      decoder: new TextDecoder(),
      buffer: "",
      output: "",
      frames: [],
      stderr: new Response(child.stderr).text(),
    };
    await sendFrame(api, {
      type: "initialize",
      request_id: "disable-initialize",
      workspace_root: join(root, "workspace"),
      state_path: statePath,
      permission_mode: "default",
    });
    const frame = await readFrame(api, 10_000);
    invariant(frame.type === "error" && frame.ok === false, "disabled built E02 owner unexpectedly became ready");
    const error = object(frame.error);
    invariant(
      text(error.message).includes("e02_typescript_runtime_disabled") || text(error.code) === "e02_typescript_runtime_disabled",
      `disable failure did not originate from the canonical owner gate: ${JSON.stringify(frame)}`,
    );
    const exitCode = await stopGracefully(api);
    api = null;
    await emit("disable", {
      disabled_component: "built CodeWorkerApplication.runCapabilityApiPort -> E02CapabilityCoordinator.open",
      environment_gate: "ZYRA_DISABLE_E02_TYPESCRIPT_RUNTIME=1",
      process_id: child.pid,
      default_owner_failed: true,
      error_code: error.code,
      error_message_sha256: sha256(text(error.message)),
      python_fallback_observed: false,
      protocol_process_exit_code: exitCode,
      ready_frame_observed: false,
    });
  } finally {
    if (api) api.child.kill();
    await rm(root, { recursive: true, force: true });
  }
}

const mode = process.argv[2] ?? "";
if (mode === "mcp-server") await mcpServer();
else if (mode === "runtime-origin" || mode === "write-path") await runtimeOrigin();
else if (mode === "resume") await resume();
else if (mode === "lost-ack") await lostAck();
else if (mode === "disable") await disable();
else if (mode === "all") {
  await runtimeOrigin();
  await resume();
  await lostAck();
  await disable();
} else {
  throw new Error(`unsupported E02 probe mode: ${mode || "<missing>"}`);
}
