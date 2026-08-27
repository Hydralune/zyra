import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "bun:test";

import type { JsonObject, JsonValue } from "../../src/contracts.ts";
import {
  E02_API_PROTOCOL_VERSION,
  E02ApiPortRuntime,
  type E02ApiPortInitialization,
  type E02ApiPortRequest,
} from "../../src/e02/api-port-runtime.ts";

interface TestPort {
  root: string;
  initialization: E02ApiPortInitialization;
  runtime: E02ApiPortRuntime;
}

async function openPort(label: string): Promise<TestPort> {
  const root = await mkdtemp(join(tmpdir(), `zyra-e02-api-${label}-`));
  const workspace = join(root, "workspace");
  await mkdir(workspace, { recursive: true });
  const initialization: E02ApiPortInitialization = {
    type: "initialize",
    request_id: `initialize-${label}`,
    workspace_root: workspace,
    state_path: join(root, "state", "e02.json"),
    artifact_root: join(root, "artifacts"),
    permission_mode: "default",
    sealed_autonomous: false,
    runtime_constraints: {
      projectRoot: resolve("."),
      test_label: label,
    },
  };
  const runtime = await E02ApiPortRuntime.open(initialization);
  return { root, initialization, runtime };
}

async function disposePort(port: TestPort): Promise<void> {
  await port.runtime.close();
  await rm(port.root, { recursive: true, force: true });
}

function request(operation: string, payload: JsonObject = {}, suffix = operation): E02ApiPortRequest {
  return {
    type: "request",
    request_id: `request-${suffix}`,
    operation,
    payload,
  };
}

function object(value: JsonValue): JsonObject {
  assert.ok(value && typeof value === "object" && !Array.isArray(value));
  return value as JsonObject;
}

test("API port health exposes one locked TypeScript owner and no Python fallback", async () => {
  const port = await openPort("health");
  try {
    const health = port.runtime.health();
    assert.equal(health.protocol, E02_API_PROTOCOL_VERSION);
    assert.equal(health.canonical_entrypoint, "E02CapabilityCoordinator.execute");
    assert.equal(health.canonical_owner, "typescript");
    assert.equal(health.python_permission_fallback, false);
    assert.equal(health.python_mcp_fallback, false);
    assert.equal(health.python_skill_fallback, false);
    assert.equal(health.python_plugin_fallback, false);
    assert.equal(health.python_command_fallback, false);
    const state = object(health.state as JsonValue);
    assert.equal(state.lock_held, true);
    assert.equal(state.python_state_writer, false);
    assert.equal(state.atomic_checkpoint_replace, true);
  } finally {
    await disposePort(port);
  }
});

test("API projections are live views of the coordinator registries", async () => {
  const port = await openPort("projections");
  try {
    const tools = object(await port.runtime.dispatch(request("tools")));
    assert.ok(Number(tools.count) >= 30);
    assert.ok(Array.isArray(tools.tools));
    assert.equal(tools.canonical_owner, "typescript");
    assert.ok((tools.tools as JsonValue[]).some((item) => object(item).name === "e02_health"));

    const skills = object(await port.runtime.dispatch(request("skills")));
    assert.ok(Array.isArray(skills.skills));
    assert.equal(skills.canonical_owner, "typescript");
    assert.equal(skills.body_in_projection, false);

    const plugins = object(await port.runtime.dispatch(request("plugins")));
    assert.ok(Array.isArray(plugins.tools));
    assert.equal(plugins.canonical_owner, "typescript");

    const commands = object(await port.runtime.dispatch(request("commands")));
    assert.equal(commands.dispatch_owner, "typescript");
    assert.equal(commands.python_parser_enabled, false);
    assert.ok(commands.commands);
  } finally {
    await disposePort(port);
  }
});

test("MCP HTTP projection remains TypeScript-owned and unknown routes fail closed", async () => {
  const port = await openPort("mcp-get");
  try {
    const health = object(await port.runtime.dispatch(request("mcp.http.get", {
      parts: ["mcp", "health"],
    }, "mcp-health")));
    assert.equal(health.status, 200);
    const healthBody = object(health.body as JsonValue);
    assert.equal(healthBody.state_owner, "McpRuntimeCoordinator");
    assert.equal(healthBody.default_entrypoint, "E02CapabilityCoordinator.execute");

    const missing = object(await port.runtime.dispatch(request("mcp.http.get", {
      parts: ["mcp", "not-a-route"],
    }, "mcp-missing")));
    assert.equal(missing.status, 404);
    assert.equal(object(missing.body as JsonValue).error, "mcp_route_not_found");
  } finally {
    await disposePort(port);
  }
});

test("unregistered MCP mutations return conflict without a Python mutation path", async () => {
  const port = await openPort("mcp-post");
  try {
    const response = object(await port.runtime.dispatch(request("mcp.http.post", {
      parts: ["mcp", "servers", "invented"],
      body: { action: "connect" },
    }, "mcp-post-unknown")));
    assert.equal(response.status, 409);
    const body = object(response.body as JsonValue);
    assert.equal(body.error, "mcp_mutation_requires_typescript_control_operation");
    assert.equal(body.canonical_permission_owner, "typescript");
    assert.equal(body.python_mutation_fallback, false);
  } finally {
    await disposePort(port);
  }
});

test("execute resolves canonical namespace and enters E02CapabilityCoordinator.execute", async () => {
  const port = await openPort("execute");
  try {
    const response = object(await port.runtime.dispatch(request("execute", {
      tool_name: "e02_health",
      arguments: {},
      operation: "read",
      tool_call_id: "api-health-call",
      actor_id: "behavior-test",
      correlation_id: "api-health-correlation",
    }, "execute-health")));
    assert.equal(response.canonical_entrypoint, "E02CapabilityCoordinator.execute");
    assert.ok(response.receipt);
    assert.ok(response.snapshot_hash);
    const receipt = object(response.receipt as JsonValue);
    assert.equal(receipt.owner, "typescript-e02-control");
    assert.ok(receipt.executionId);
    assert.ok(receipt.transitionId);
    const commit = object(receipt.commit as JsonValue);
    assert.equal(commit.operation, "read");
    assert.equal(commit.domain, "command");
  } finally {
    await disposePort(port);
  }
});

test("read-only slash commands are parsed, authorized, and dispatched only by TypeScript", async () => {
  const port = await openPort("command-dispatch");
  try {
    const response = object(await port.runtime.dispatch(request("execute", {
      tool_name: "command",
      arguments: { input: "/mcp tools" },
      command_name: "mcp",
      operation: "read",
      tool_call_id: "api-command-call",
      actor_id: "behavior-test",
      correlation_id: "api-command-correlation",
    }, "execute-command")));
    assert.equal(response.canonical_entrypoint, "E02CapabilityCoordinator.execute");
    const receipt = object(response.receipt as JsonValue);
    const result = object(receipt.result as JsonValue);
    const output = object(result.output as JsonValue);
    const invocation = object(output.invocation as JsonValue);
    assert.equal(invocation.commandName, "mcp");
    assert.equal(invocation.status, "completed");
    assert.equal(object(invocation.permission as JsonValue).effect, "allow");
    assert.equal(receipt.owner, "typescript-command");
  } finally {
    await disposePort(port);
  }
});

test("API approval transport forwards a response and TS issues the exact retry permit", async () => {
  const port = await openPort("permission-forwarder");
  const executionPayload: JsonObject = {
    tool_name: "command",
    arguments: { input: "/e02-reload" },
    operation: "execute",
    tool_call_id: "api-approval-call",
    actor_id: "behavior-test",
    correlation_id: "api-approval-correlation",
  };
  try {
    await assert.rejects(
      () => port.runtime.dispatch(request("execute", executionPayload, "approval-required")),
      /capability requires exact approval/,
    );
    const projection = object(await port.runtime.dispatch(request("permission.get", {
      view: "requests",
    }, "approval-list")));
    assert.equal(projection.canonical_owner, "typescript.PermissionCoordinator");
    assert.equal(projection.python_decision_fallback, false);
    const requests = projection.requests as JsonValue[];
    assert.equal(requests.length, 1);
    const approval = object(requests[0]!);
    assert.equal(approval.status, "delivered");
    assert.equal(approval.tool_call_id, "api-approval-call");
    assert.equal(approval.final_arguments_projected, false);

    const response = object(await port.runtime.dispatch(request("permission.respond", {
      request_id: approval.request_id,
      response_id: "approval-response-exact",
      effect: "allow",
      responder: "behavior-test",
    }, "approval-response")));
    assert.equal(response.accepted, true);
    assert.equal(response.effect, "allow");
    assert.ok(response.permit_id);
    assert.equal(response.canonical_owner, "typescript.PermissionCoordinator");
    assert.equal(response.python_decision_fallback, false);

    const retry = object(await port.runtime.dispatch(request("execute", {
      ...executionPayload,
      permit_id: response.permit_id,
    }, "approval-retry")));
    const receipt = object(retry.receipt as JsonValue);
    assert.equal(receipt.owner, "typescript-command");
    assert.equal(receipt.permitId, response.permit_id);
    const result = object(receipt.result as JsonValue);
    const output = object(result.output as JsonValue);
    const invocation = object(output.invocation as JsonValue);
    assert.equal(invocation.commandName, "e02-reload");
    assert.equal(invocation.status, "completed");
    assert.equal(object(invocation.permission as JsonValue).reasonCode, "outer_e02_permit_consumed");
  } finally {
    await disposePort(port);
  }
});

test("external permission transport binds approval and one-use permit to the physical call", async () => {
  const port = await openPort("external-permission");
  const external: JsonObject = {
    run_id: "external-browser-run",
    task_id: "external-browser-task",
    session_id: "external-browser-session",
    session_revision: 4,
    worker_request_id: "external-browser-worker",
    tool_call_id: "external-browser-call",
    tool_name: "open_url",
    namespace: "browser",
    server_id: "",
    operation: "execute",
    workspace_root: port.initialization.workspace_root,
    arguments: { url: "https://example.test/original" },
    metadata: {
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        openWorldHint: true,
        idempotentHint: false,
      },
    },
    await_approval_delivery: true,
  };
  try {
    const enforcement = object(await port.runtime.dispatch(request(
      "permission.enforce",
      external,
      "external-enforce",
    )));
    assert.equal(enforcement.canonical_owner, "typescript.PermissionCoordinator");
    assert.equal(enforcement.python_decision_fallback, false);
    assert.equal(enforcement.allowed, false);
    assert.equal(enforcement.pending_approval, true);
    const asked = object(enforcement.decision as JsonValue);
    assert.equal(asked.effect, "ask");
    const binding = object(asked.requestBinding as JsonValue);
    assert.equal(binding.run_id, external.run_id);
    assert.equal(binding.task_id, external.task_id);
    assert.equal(binding.session_id, external.session_id);
    assert.equal(binding.worker_request_id, external.worker_request_id);
    assert.equal(binding.tool_call_id, external.tool_call_id);
    const approval = object(enforcement.approval_request as JsonValue);
    const approvalBinding = object(approval.request_binding as JsonValue);
    assert.equal(approvalBinding.run_id, external.run_id);
    assert.equal(approvalBinding.tool_call_id, external.tool_call_id);
    assert.equal(approvalBinding.arguments_digest, binding.arguments_digest);

    const response = object(await port.runtime.dispatch(request("permission.respond", {
      request_id: approval.request_id,
      response_id: "external-browser-approval",
      effect: "allow",
      responder: "behavior-test",
    }, "external-respond")));
    assert.equal(response.accepted, true);
    assert.ok(response.permit_id);

    await port.runtime.close();
    port.runtime = await E02ApiPortRuntime.open({
      ...port.initialization,
      request_id: "initialize-external-permission-restored",
    });
    assert.ok(Number(object(port.runtime.health().state as JsonValue).generation) >= 2);

    const mismatch = object(await port.runtime.dispatch(request("permission.claim", {
      ...external,
      arguments: { url: "https://example.test/tampered" },
      permit_id: response.permit_id,
    }, "external-claim-mismatch")));
    assert.equal(mismatch.claimed, false);

    const claimed = object(await port.runtime.dispatch(request("permission.claim", {
      ...external,
      permit_id: response.permit_id,
    }, "external-claim-exact")));
    assert.equal(claimed.claimed, true);
    assert.equal(claimed.permit_id, response.permit_id);
    assert.equal(claimed.canonical_owner, "typescript.PermissionCoordinator");
    const allowed = object(claimed.decision as JsonValue);
    assert.equal(allowed.effect, "allow");
    assert.equal(object(allowed.requestBinding as JsonValue).run_id, external.run_id);

    const replay = object(await port.runtime.dispatch(request("permission.claim", {
      ...external,
      permit_id: response.permit_id,
    }, "external-claim-replay")));
    assert.equal(replay.claimed, false);
  } finally {
    await disposePort(port);
  }
});

test("managed Phase 2 control permission denies any expanded scope", async () => {
  const port = await openPort("phase2-strongest-permission");
  const base: JsonObject = {
    run_id: "phase2-run",
    task_id: "phase2-task",
    session_id: "phase2-session",
    session_revision: 0,
    worker_request_id: "phase2-worker-request",
    tool_call_id: "phase2-tool-call",
    tool_name: "phase2.strongest-control",
    namespace: "builtin",
    server_id: "",
    operation: "phase2.strongest.activate",
    workspace_root: port.initialization.workspace_root,
    arguments: {
      requested_permissions: ["graph.write", "worker.dispatch"],
    },
    metadata: {
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        openWorldHint: false,
        idempotentHint: true,
      },
    },
    await_approval_delivery: false,
  };
  try {
    const exact = object(await port.runtime.dispatch(request(
      "permission.enforce",
      base,
      "phase2-scope-exact",
    )));
    assert.equal(exact.canonical_owner, "typescript.PermissionCoordinator");
    assert.equal(exact.allowed, true);
    assert.equal(object(exact.decision as JsonValue).effect, "allow");

    await assert.rejects(
      () => port.runtime.dispatch(request("permission.enforce", {
        ...base,
        tool_call_id: "phase2-tool-call-expanded",
        arguments: {
          requested_permissions: [
            "graph.write",
            "worker.dispatch",
            "workspace.admin",
          ],
        },
      }, "phase2-scope-expanded")),
      (error: unknown) => (
        error instanceof Error
        && (error as Error & { code?: string }).code
          === "phase2_strongest_permission_scope_invalid"
      ),
    );
  } finally {
    await disposePort(port);
  }
});

test("restart revokes an unconsumed ordinary host permit", async () => {
  const port = await openPort("ordinary-permit-restart");
  const executionPayload: JsonObject = {
    tool_name: "command",
    arguments: { input: "/e02-reload" },
    operation: "execute",
    tool_call_id: "ordinary-restart-call",
    actor_id: "behavior-test",
    correlation_id: "ordinary-restart-correlation",
  };
  try {
    await assert.rejects(
      () => port.runtime.dispatch(request("execute", executionPayload, "ordinary-restart-ask")),
      /capability requires exact approval/,
    );
    const projection = object(await port.runtime.dispatch(request("permission.get", {
      view: "requests",
    }, "ordinary-restart-list")));
    const approval = object((projection.requests as JsonValue[])[0]!);
    const response = object(await port.runtime.dispatch(request("permission.respond", {
      request_id: approval.request_id,
      response_id: "ordinary-restart-response",
      effect: "allow",
      responder: "behavior-test",
    }, "ordinary-restart-response")));
    assert.ok(response.permit_id);

    await port.runtime.close();
    port.runtime = await E02ApiPortRuntime.open({
      ...port.initialization,
      request_id: "initialize-ordinary-permit-restored",
    });
    await assert.rejects(
      () => port.runtime.dispatch(request("execute", {
        ...executionPayload,
        permit_id: response.permit_id,
      }, "ordinary-restart-replay")),
      /permit .* (?:revoked|does not exist|runtime epoch)/,
    );
  } finally {
    await disposePort(port);
  }
});

test("exclusive state lock rejects a second live logical owner", async () => {
  const port = await openPort("lock");
  try {
    await assert.rejects(
      () => E02ApiPortRuntime.open({
        ...port.initialization,
        request_id: "initialize-lock-contender",
      }),
      /already owned by process/,
    );
    assert.equal(object(port.runtime.health().state as JsonValue).lock_held, true);
  } finally {
    await disposePort(port);
  }
});

test("exclusive state lock reclaims an exited Windows owner", async () => {
  if (process.platform !== "win32") return;
  const root = await mkdtemp(join(tmpdir(), "zyra-e02-api-stale-windows-owner-"));
  const workspace = join(root, "workspace");
  const statePath = join(root, "state", "e02.json");
  const lockPath = `${statePath}.lock`;
  await mkdir(workspace, { recursive: true });
  await mkdir(join(root, "state"), { recursive: true });
  const exited = Bun.spawn([process.execPath, "-e", "process.exit(0)"], {
    stderr: "ignore",
    stdout: "ignore",
  });
  const exitedPid = exited.pid;
  await exited.exited;
  await writeFile(lockPath, JSON.stringify({
    version: "zyra.e02-api-lock/v1",
    pid: exitedPid,
    acquired_at: new Date().toISOString(),
    state_path: statePath,
  }));

  let runtime: E02ApiPortRuntime | null = null;
  try {
    runtime = await E02ApiPortRuntime.open({
      type: "initialize",
      request_id: "initialize-stale-windows-owner",
      workspace_root: workspace,
      state_path: statePath,
      artifact_root: join(root, "artifacts"),
      permission_mode: "default",
      sealed_autonomous: false,
      runtime_constraints: {
        projectRoot: resolve("."),
        test_label: "stale-windows-owner",
      },
    });
    const state = object(runtime.health().state as JsonValue);
    assert.equal(state.lock_held, true);
    const replacement = JSON.parse(await readFile(lockPath, "utf8")) as { pid: number };
    assert.equal(replacement.pid, process.pid);
  } finally {
    await runtime?.close();
    await rm(root, { recursive: true, force: true });
  }
});

test("checkpoint close and multiple reopens preserve prior-epoch history before bootstrap", async () => {
  const port = await openPort("restore");
  const first = object(await port.runtime.dispatch(request("snapshot", {}, "snapshot-before-close")));
  const firstHash = String(first.snapshotHash);
  await port.runtime.close();

  let restored: E02ApiPortRuntime | null = null;
  let restoredAgain: E02ApiPortRuntime | null = null;
  try {
    restored = await E02ApiPortRuntime.open({
      ...port.initialization,
      request_id: "initialize-restored",
    });
    const snapshot = object(await restored.dispatch(request("snapshot", {}, "snapshot-restored")));
    assert.equal(snapshot.opened, true);
    assert.ok(snapshot.snapshotHash);
    assert.ok(firstHash);
    const state = object(restored.health().state as JsonValue);
    assert.ok(Number(state.generation) >= 2);
    assert.equal(state.lock_held, true);
    await restored.close();
    restored = null;

    restoredAgain = await E02ApiPortRuntime.open({
      ...port.initialization,
      request_id: "initialize-restored-again",
    });
    const thirdSnapshot = object(await restoredAgain.dispatch(request(
      "snapshot",
      {},
      "snapshot-restored-again",
    )));
    assert.equal(thirdSnapshot.opened, true);
    const thirdState = object(restoredAgain.health().state as JsonValue);
    assert.ok(Number(thirdState.generation) >= 3);
    assert.equal(thirdState.lock_held, true);
  } finally {
    await restored?.close();
    await restoredAgain?.close();
    await rm(port.root, { recursive: true, force: true });
  }
});

test("tampered persisted snapshot binding is rejected before coordinator bootstrap", async () => {
  const port = await openPort("tamper");
  await port.runtime.close();
  const statePath = port.initialization.state_path;
  const envelope = JSON.parse(await readFile(statePath, "utf8")) as JsonObject;
  envelope.snapshot_hash = "sha256:tampered";
  await writeFile(statePath, JSON.stringify(envelope), "utf8");
  try {
    await assert.rejects(
      () => E02ApiPortRuntime.open({
        ...port.initialization,
        request_id: "initialize-tampered",
      }),
      /snapshot hash binding is invalid/,
    );
  } finally {
    await rm(port.root, { recursive: true, force: true });
  }
});

test("unknown API operations fail without checkpointing a fallback decision", async () => {
  const port = await openPort("unknown-operation");
  try {
    const before = object(port.runtime.health().state as JsonValue);
    await assert.rejects(
      () => port.runtime.dispatch(request("python.permission.evaluate", {}, "forbidden-fallback")),
      /Unknown E02 API operation/,
    );
    const after = object(port.runtime.health().state as JsonValue);
    assert.equal(after.generation, before.generation);
    assert.equal(port.runtime.health().python_permission_fallback, false);
  } finally {
    await disposePort(port);
  }
});

test("default API coordinator executes a live projected MCP tool and journals only durable identity", async () => {
  const root = await mkdtemp(join(tmpdir(), "zyra-e02-api-live-mcp-"));
  const workspace = join(root, "workspace");
  const serverPath = join(root, "server.mjs");
  const effectPath = join(root, "effect-count.txt");
  const statePath = join(root, "state", "e02.json");
  await mkdir(workspace, { recursive: true });
  await writeFile(serverPath, [
    'import { createInterface } from "node:readline";',
    'import { existsSync, readFileSync, writeFileSync } from "node:fs";',
    'const effectPath = process.argv[2];',
    'const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });',
    'const send = (id, result) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\\n");',
    'for await (const line of lines) {',
    '  const request = JSON.parse(line);',
    '  if (request.id === undefined || request.id === null) continue;',
    '  if (request.method === "initialize") send(request.id, { protocolVersion: "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "e02-api-live", version: "1.0.0" } });',
    '  else if (request.method === "tools/list") send(request.id, { tools: [{ name: "nonce_effect", description: "commit one test effect", inputSchema: { type: "object", properties: { nonce: { type: "string" } }, required: ["nonce"], additionalProperties: false }, annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false } }] });',
    '  else if (request.method === "tools/call") { const before = existsSync(effectPath) ? Number(readFileSync(effectPath, "utf8")) : 0; const count = before + 1; writeFileSync(effectPath, String(count), "utf8"); send(request.id, { content: [{ type: "text", text: `effect:${count}` }], structuredContent: { effect_count: count }, isError: false }); }',
    '  else send(request.id, {});',
    '}',
  ].join("\n"), "utf8");

  const initialization: E02ApiPortInitialization = {
    type: "initialize",
    request_id: "initialize-live-mcp",
    workspace_root: workspace,
    state_path: statePath,
    permission_mode: "default",
    sealed_autonomous: false,
    runtime_constraints: {
      typescriptMcpServers: {
        review: {
          transport: {
            kind: "stdio",
            command: process.execPath,
            arguments: [serverPath, effectPath],
            cwd: root,
            inherit_environment: [],
          },
          network_policy: "offline",
          allowed_operations: ["*"],
          timeouts: {
            connect_ms: 5_000,
            initialize_ms: 5_000,
            request_ms: 5_000,
            shutdown_ms: 2_000,
          },
        },
      },
    },
  };
  let runtime: E02ApiPortRuntime | null = null;
  try {
    runtime = await E02ApiPortRuntime.open(initialization);
    const policyRevision = Number(
      runtime.coordinator.permission.evaluator.rules.snapshot().revision,
    );
    assert.equal(policyRevision, 1);
    const policy = object(await runtime.dispatch(request("permission.policy", {
      action: "replace_rules",
      expected_revision: policyRevision,
      actor_id: "behavior-test",
      rules: [{
        ruleId: "allow-live-mcp-effect",
        effect: "allow",
        source: "session",
        kind: "tool",
        toolPattern: "mcp__review__nonce_effect",
        namespacePattern: "mcp",
        serverPattern: "review",
        operationPattern: "tools-call",
        priority: 10_000,
      }],
    }, "live-mcp-policy")));
    assert.equal(policy.canonical_owner, "typescript.PermissionCoordinator");
    assert.ok(policy.commit);

    const payload: JsonObject = {
      tool_name: "mcp__review__nonce_effect",
      arguments: { nonce: "api-live-mcp" },
      operation: "tools/call",
      namespace: "mcp",
      server_id: "review",
      tool_call_id: "api-live-mcp-call",
      session_revision: 0,
    };
    const first = object(await runtime.dispatch(request("execute", payload, "live-mcp-first")));
    const firstReceipt = object(first.receipt as JsonValue);
    assert.equal(firstReceipt.owner, "typescript-mcp");
    assert.equal(firstReceipt.replayed, false);
    assert.equal(await readFile(effectPath, "utf8"), "1");

    const mcp = runtime.coordinator.mcp.snapshot();
    const journalIdentity = mcp.journal.records.find((record) => record.identity.toolCallId === "api-live-mcp-call")?.identity;
    assert.ok(journalIdentity);
    assert.equal(journalIdentity.method, "tools/call");
    assert.equal("interactive" in journalIdentity, false);
    assert.equal("sealedAutonomous" in journalIdentity, false);
    assert.equal("workspaceRoot" in journalIdentity, false);

    await runtime.close();
    runtime = await E02ApiPortRuntime.open({ ...initialization, request_id: "initialize-live-mcp-restored" });
    const replay = object(await runtime.dispatch(request("execute", payload, "live-mcp-replay")));
    assert.equal(object(replay.receipt as JsonValue).replayed, true);
    assert.equal(await readFile(effectPath, "utf8"), "1");
  } finally {
    await runtime?.close();
    await rm(root, { recursive: true, force: true });
  }
});
