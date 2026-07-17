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

test("checkpoint close and reopen restores state before bootstrap", async () => {
  const port = await openPort("restore");
  const first = object(await port.runtime.dispatch(request("snapshot", {}, "snapshot-before-close")));
  const firstHash = String(first.snapshotHash);
  await port.runtime.close();

  let restored: E02ApiPortRuntime | null = null;
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
  } finally {
    await restored?.close();
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
