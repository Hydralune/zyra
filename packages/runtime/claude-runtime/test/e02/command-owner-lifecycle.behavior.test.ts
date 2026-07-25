import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "bun:test";

import type { JsonObject, JsonValue, RuntimeRunInput } from "../../src/contracts.ts";
import {
  E02CapabilityCoordinator,
  type E02ExecutionReceipt,
} from "../../src/e02/coordinator.ts";

function object(value: JsonValue | undefined): JsonObject {
  assert.ok(value && typeof value === "object" && !Array.isArray(value));
  return value as JsonObject;
}

function runtimeInput(
  workspaceRoot: string,
  label: string,
  options: {
    sealed?: boolean;
    mcpServers?: JsonObject;
  } = {},
): RuntimeRunInput {
  return {
    runId: `run-${label}`,
    taskId: `task-${label}`,
    workerRequestId: `worker-${label}`,
    sessionId: `session-${label}`,
    messages: [],
    turns: [],
    tools: [],
    config: {
      runtimeConstraints: {
        workspaceRoot,
        projectRoot: workspaceRoot,
        watchSkills: false,
        watchPlugins: false,
        ...(options.mcpServers
          ? { typescriptMcpServers: options.mcpServers }
          : {}),
      },
      permissionPolicy: {
        mode: options.sealed ? "sealed" : "auto",
        rules: [],
      },
    },
    restoredState: null,
    metadata: {
      session_revision: 0,
      test_label: label,
    },
  };
}

async function executeCommand(
  coordinator: E02CapabilityCoordinator,
  input: string,
  suffix: string,
  structuredArguments: JsonObject = {},
): Promise<E02ExecutionReceipt> {
  return coordinator.execute("command", {
    input,
    ...(Object.keys(structuredArguments).length
      ? { arguments: structuredArguments }
      : {}),
  }, {
    toolCallId: `command-${suffix}`,
    operation: "read",
    metadata: {
      actor: "owner-lifecycle-test",
      correlation_id: `correlation-${suffix}`,
    },
  });
}

function commandOutput(receipt: E02ExecutionReceipt): JsonObject {
  const invocation = object(receipt.result.output.invocation);
  assert.equal(invocation.status, "completed");
  return object(invocation.output);
}

async function writeMcpServer(path: string): Promise<void> {
  await writeFile(path, [
    'import { createInterface } from "node:readline";',
    'const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });',
    'const send = (id, result) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\\n");',
    'for await (const line of lines) {',
    '  const request = JSON.parse(line);',
    '  if (request.id === undefined || request.id === null) continue;',
    '  if (request.method === "initialize") send(request.id, { protocolVersion: "2025-06-18", capabilities: { tools: {}, resources: {}, prompts: {} }, serverInfo: { name: "owner-lifecycle", version: "1.0.0" } });',
    '  else if (request.method === "tools/list") send(request.id, { tools: [] });',
    '  else if (request.method === "resources/list") send(request.id, { resources: [] });',
    '  else if (request.method === "resources/templates/list") send(request.id, { resourceTemplates: [] });',
    '  else if (request.method === "prompts/list") send(request.id, { prompts: [] });',
    '  else send(request.id, {});',
    '}',
  ].join("\n"), "utf8");
}

test("slash MCP lifecycle commands mutate the real owner and keep elicitation secrets out of receipts", async () => {
  const root = await mkdtemp(join(tmpdir(), "zyra-e02-command-mcp-"));
  const workspace = join(root, "workspace");
  const serverPath = join(root, "server.mjs");
  await mkdir(workspace, { recursive: true });
  await writeMcpServer(serverPath);
  const input = runtimeInput(workspace, "mcp-owner", {
    mcpServers: {
      review: {
        transport: {
          kind: "stdio",
          command: process.execPath,
          arguments: [serverPath],
          cwd: root,
          inherit_environment: [],
        },
        network_policy: "offline",
        allowed_operations: ["*"],
        allow_elicitation: true,
        timeouts: {
          connect_ms: 5_000,
          initialize_ms: 5_000,
          request_ms: 5_000,
          shutdown_ms: 2_000,
        },
      },
    },
  });
  const coordinator = await E02CapabilityCoordinator.open(input);
  try {
    const connection = coordinator.mcp.connections.requireConnected("review").record;
    const elicitation = coordinator.mcp.elicitation.request({
      runId: coordinator.runtime.runId,
      taskId: coordinator.runtime.taskId,
      sessionId: coordinator.runtime.sessionId,
      sessionRevision: 0,
      workerRequestId: coordinator.runtime.workerRequestId,
      toolCallId: "server-elicit-call",
      serverId: "review",
      connectionId: connection.connectionId,
      connectionEpoch: connection.epoch,
      requestId: "server-elicit-request",
    }, {
      mode: "form",
      message: "Provide a bounded value",
      requestedSchema: {
        type: "object",
        properties: {
          answer: {
            type: "string",
            title: "Answer",
            description: null,
            default: null,
            enum: [],
            enumNames: [],
            minimum: null,
            maximum: null,
            minLength: 1,
            maxLength: 128,
            format: null,
          },
        },
        required: ["answer"],
      },
      meta: {},
    });
    const secret = "owner-secret-must-not-project";
    const elicitationReceipt = await executeCommand(
      coordinator,
      `/mcp elicit review --request "${elicitation.elicitationId}" --response {}`,
      "mcp-elicit",
      {
        response: {
          request_id: elicitation.elicitationId,
          server_id: "review",
          response: { answer: secret },
          response_digest: "browser-bound-digest",
        },
      },
    );
    const elicitationOutput = commandOutput(elicitationReceipt);
    assert.equal(elicitationOutput.canonical_owner, "typescript.McpRuntimeCoordinator");
    assert.equal(elicitationOutput.action, "elicit");
    assert.equal(
      object(object(elicitationOutput.result).elicitation).status,
      "accepted",
    );
    assert.doesNotMatch(JSON.stringify(elicitationReceipt), new RegExp(secret));
    assert.doesNotMatch(JSON.stringify(coordinator.events.snapshot()), new RegExp(secret));
    assert.doesNotMatch(JSON.stringify(coordinator.journal.snapshot()), new RegExp(secret));

    const refreshed = commandOutput(await executeCommand(
      coordinator,
      "/mcp refresh review",
      "mcp-refresh",
    ));
    assert.equal(refreshed.action, "refresh");
    assert.ok(String(refreshed.effect_id).startsWith("mcp-lifecycle-effect-"));

    const epochBefore = coordinator.mcp.connections.requireConnected("review").record.epoch;
    const reconnected = commandOutput(await executeCommand(
      coordinator,
      "/mcp reconnect review",
      "mcp-reconnect",
    ));
    assert.equal(reconnected.action, "reconnect");
    assert.ok(coordinator.mcp.connections.requireConnected("review").record.epoch > epochBefore);

    const disabled = commandOutput(await executeCommand(
      coordinator,
      "/mcp disable review",
      "mcp-disable",
    ));
    assert.equal(disabled.action, "disable");
    assert.equal(coordinator.mcp.config.require("review").enabled, false);
    assert.equal(coordinator.mcp.connections.get("review")?.phase, "closed");
    assert.equal(object(disabled.state_after).server !== null, true);

    const enabled = commandOutput(await executeCommand(
      coordinator,
      "/mcp enable review",
      "mcp-enable",
    ));
    assert.equal(enabled.action, "enable");
    assert.equal(coordinator.mcp.config.require("review").enabled, true);
    assert.equal(coordinator.mcp.connections.requireConnected("review").record.phase, "ready");

    await assert.rejects(
      executeCommand(coordinator, "/mcp auth-refresh review", "mcp-auth-error"),
      /no configured OAuth provider/i,
    );
  } finally {
    await coordinator.close();
    await rm(root, { recursive: true, force: true });
  }
});

test("slash skill update and invoke use SkillCoordinator revisions, hashes, and invocation receipts", async () => {
  const root = await mkdtemp(join(tmpdir(), "zyra-e02-command-skill-"));
  const workspace = join(root, "workspace");
  const skillDirectory = join(workspace, "skills", "review-skill");
  const skillPath = join(skillDirectory, "SKILL.md");
  await mkdir(skillDirectory, { recursive: true });
  await writeFile(skillPath, [
    "---",
    "name: review-skill",
    "description: Review canonical state",
    'arguments: [{"name":"focus","type":"string"},{"name":"depth","type":"integer"}]',
    "---",
    "Inspect the canonical state and report the result.",
  ].join("\n"), "utf8");
  const calls: JsonObject[] = [];
  const coordinator = await E02CapabilityCoordinator.open(
    runtimeInput(workspace, "skill-owner"),
    {
      skillExecutor: async ({ plan }) => {
        calls.push(plan.arguments);
        return {
          output: {
            invoked: true,
            arguments: plan.arguments,
          },
          artifacts: [],
          inputTokens: plan.inputTokenEstimate,
          outputTokens: 3,
          costMicros: 0,
          metadata: {
            executor: "focused-owner-test",
          },
        };
      },
    },
  );
  try {
    const before = coordinator.skills.registry.resolve("review-skill");
    await writeFile(skillPath, [
      "---",
      "name: review-skill",
      "description: Review canonical state",
      'arguments: [{"name":"focus","type":"string"},{"name":"depth","type":"integer"}]',
      "---",
      "Inspect the canonical state, verify it, and report the result.",
    ].join("\n"), "utf8");
    const update = commandOutput(await executeCommand(
      coordinator,
      [
        "/skills update",
        '--skill "review-skill"',
        `--expected-hash "sha256:${before.descriptor.bodyDigest}"`,
        `--expected-revision ${before.revision}`,
        '--dependency-digest "dependency-test"',
        '--supply-digest "supply-test"',
        '--approval-id "approval-test"',
        '--nonce "nonce-update"',
        '--idempotency-key "skill-update-owner-test"',
      ].join(" "),
      "skill-update",
    ));
    const after = coordinator.skills.registry.resolve("review-skill");
    assert.equal(update.canonical_owner, "typescript.SkillCoordinator");
    assert.equal(update.action, "update");
    assert.ok(after.revision > before.revision);
    assert.notEqual(after.descriptor.bodyDigest, before.descriptor.bodyDigest);
    assert.ok(String(update.receipt_id).startsWith("skill-reload-receipt-"));
    const ownerAdmission = object(update.owner_admission);
    assert.equal(ownerAdmission.canonical_owner_verified, true);
    assert.equal(ownerAdmission.canonical_owner, "typescript.SkillCoordinator");
    assert.equal(ownerAdmission.approval_owner, "typescript.PermissionCoordinator");
    assert.ok(String(ownerAdmission.approval_decision_id));
    assert.ok(String(ownerAdmission.dependency_digest));
    assert.ok(String(ownerAdmission.supply_digest));
    assert.equal(object(update.caller_attestations).authoritative, false);

    const invocationArguments = { focus: "owner-state", depth: 2 };
    const argumentsDigest = skillArgumentsDigest(invocationArguments);
    const invoke = commandOutput(await executeCommand(
      coordinator,
      [
        "/skills invoke",
        '--skill "review-skill"',
        `--registry-revision ${after.revision}`,
        `--descriptor-digest "sha256:${after.descriptor.descriptorDigest}"`,
        `--content-hash "sha256:${after.descriptor.bodyDigest}"`,
        `--arguments-json '${JSON.stringify(invocationArguments)}'`,
        `--arguments-digest "${argumentsDigest}"`,
        '--nonce "nonce-invoke"',
        '--idempotency-key "skill-invoke-owner-test"',
      ].join(" "),
      "skill-invoke",
    ));
    assert.equal(invoke.action, "invoke");
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0], invocationArguments);
    assert.match(String(invoke.effect_id), /^skill-invocation-/);
    assert.equal(object(invoke.state_after).invocation_status, "completed");

    await assert.rejects(
      executeCommand(
        coordinator,
        [
          "/skills invoke",
          '--skill "review-skill"',
          `--registry-revision ${after.revision}`,
          `--descriptor-digest "sha256:${"0".repeat(64)}"`,
          `--content-hash "sha256:${after.descriptor.bodyDigest}"`,
          "--arguments-json {}",
          `--arguments-digest "${skillArgumentsDigest({})}"`,
        ].join(" "),
        "skill-stale-error",
      ),
      /descriptor digest is stale/i,
    );
  } finally {
    await coordinator.close();
    await rm(root, { recursive: true, force: true });
  }
});

test("sealed slash mutations are refused before owner state changes and remain permission-auditable", async () => {
  const root = await mkdtemp(join(tmpdir(), "zyra-e02-command-sealed-"));
  const workspace = join(root, "workspace");
  const serverPath = join(root, "server.mjs");
  await mkdir(workspace, { recursive: true });
  await writeMcpServer(serverPath);
  const coordinator = await E02CapabilityCoordinator.open(runtimeInput(
    workspace,
    "sealed-owner",
    {
      sealed: true,
      mcpServers: {
        review: {
          transport: {
            kind: "stdio",
            command: process.execPath,
            arguments: [serverPath],
            cwd: root,
            inherit_environment: [],
          },
          network_policy: "offline",
          allowed_operations: ["*"],
        },
      },
    },
  ));
  try {
    const revisionBefore = coordinator.mcp.config.revision;
    await assert.rejects(
      executeCommand(coordinator, "/mcp disable review", "sealed-disable"),
      /permission denied|sealed/i,
    );
    assert.equal(coordinator.mcp.config.revision, revisionBefore);
    assert.equal(coordinator.mcp.config.require("review").enabled, true);
    const permissionSnapshot = object(coordinator.permission.snapshot().evaluator);
    const decisionValues = permissionSnapshot.decisions;
    assert.ok(Array.isArray(decisionValues));
    const latest = object(decisionValues.at(-1));
    assert.equal(latest.effect, "deny");
    assert.equal(object(latest.requestBinding).operation, "mcp.lifecycle.disable");
    assert.equal(latest.humanInterventionCount, 0);
  } finally {
    await coordinator.close();
    await rm(root, { recursive: true, force: true });
  }
});

function skillArgumentsDigest(value: JsonObject): string {
  const canonical = (input: unknown): unknown => {
    if (input === null || typeof input !== "object") return input;
    if (Array.isArray(input)) return input.map(canonical);
    return Object.fromEntries(
      Object.entries(input as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, child]) => [key, canonical(child)]),
    );
  };
  const serialized = JSON.stringify(canonical(value));
  const input = JSON.stringify([serialized]);
  let first = 0x811c9dc5;
  let second = 0x9e3779b9;
  for (let index = 0; index < input.length; index += 1) {
    const code = input.charCodeAt(index);
    first ^= code;
    first = Math.imul(first, 0x01000193);
    second ^= first + code + Math.imul(second, 33);
    second = Math.imul(second ^ (second >>> 16), 0x85ebca6b);
  }
  return `skill:${(first >>> 0).toString(16).padStart(8, "0")}${(second >>> 0).toString(16).padStart(8, "0")}`;
}
