import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "bun:test";

import type { JsonObject, JsonValue } from "../../src/contracts.ts";
import { digest } from "../../src/e02/canonical.ts";
import {
  E02ApiPortRuntime,
  type E02ApiPortInitialization,
  type E02ApiPortRequest,
} from "../../src/e02/api-port-runtime.ts";

function object(value: JsonValue | undefined): JsonObject {
  assert.ok(value && typeof value === "object" && !Array.isArray(value));
  return value as JsonObject;
}

function request(
  operation: string,
  payload: JsonObject,
  suffix: string,
): E02ApiPortRequest {
  return {
    type: "request",
    request_id: `permission-console-${suffix}`,
    operation,
    payload,
  };
}

async function openRuntime(label: string): Promise<{
  root: string;
  runtime: E02ApiPortRuntime;
}> {
  const root = await mkdtemp(join(tmpdir(), `zyra-permission-console-${label}-`));
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
  return {
    root,
    runtime: await E02ApiPortRuntime.open(initialization),
  };
}

async function approval(
  runtime: E02ApiPortRuntime,
  suffix: string,
): Promise<JsonObject> {
  const result = object(await runtime.dispatch(request(
    "permission.enforce",
    {
      run_id: "console-run",
      task_id: "console-task",
      session_id: "console-session",
      session_revision: 11,
      worker_request_id: `console-worker-${suffix}`,
      tool_call_id: `console-call-${suffix}`,
      tool_name: "open_url",
      namespace: "browser",
      server_id: "",
      operation: "execute",
      arguments: { url: `https://example.test/${suffix}` },
      metadata: {
        annotations: {
          readOnlyHint: false,
          destructiveHint: false,
          openWorldHint: true,
          idempotentHint: false,
        },
      },
      await_approval_delivery: true,
    },
    `enforce-${suffix}`,
  )));
  assert.equal(result.pending_approval, true);
  return object(result.approval_request as JsonValue);
}

function consoleProof(
  envelope: JsonObject,
  responseId: string,
  effect: "allow" | "deny",
  decisionScope: "once" | "session" | "workspace" = "once",
): JsonObject {
  const challenge = object(envelope.response_challenge as JsonValue);
  const material: JsonObject = {
    version: String(challenge.version),
    nonce: String(challenge.nonce),
    canonical_owner: String(challenge.canonical_owner),
    envelope_id: String(envelope.envelope_id),
    request_id: String(envelope.request_id),
    response_id: responseId,
    effect,
    decision_scope: decisionScope,
    run_id: String(envelope.run_id),
    task_id: String(envelope.task_id),
    session_id: String(envelope.session_id),
    session_revision: Number(envelope.session_revision),
    worker_request_id: String(envelope.worker_request_id),
    tool_call_id: String(envelope.tool_call_id),
    request_fingerprint: String(envelope.request_fingerprint),
    arguments_digest: String(envelope.arguments_digest),
    policy_revision: Number(envelope.policy_revision),
    mode_revision: Number(envelope.mode_revision),
    expires_at: String(envelope.expires_at),
  };
  return { ...material, proof: digest(material) };
}

test("console proof resumes exactly one physical approval and issues one permit", async () => {
  const port = await openRuntime("allow-once");
  try {
    const envelope = await approval(port.runtime, "allow");
    const responseId = "console-response-allow";
    const proof = consoleProof(envelope, responseId, "allow");
    const response = object(await port.runtime.dispatch(request(
      "permission.respond",
      {
        request_id: envelope.request_id,
        response_id: responseId,
        effect: "allow",
        responder: "zyra-web-operator",
        metadata: {
          console_response: proof,
          display_responder: "zyra-web-operator",
        },
      },
      "respond-allow",
    )));
    assert.equal(response.accepted, true);
    assert.equal(response.response_proof_verified, true);
    assert.equal(response.response_proof_digest, proof.proof);
    assert.equal(
      response.response_challenge_digest,
      object(envelope.response_challenge as JsonValue).challenge_digest,
    );
    assert.ok(response.permit_id);
    assert.equal(response.final_arguments_digest, envelope.arguments_digest);
    assert.deepEqual(envelope.supported_decision_scopes, ["once", "session", "workspace"]);

    const external = {
      run_id: envelope.run_id,
      task_id: envelope.task_id,
      session_id: envelope.session_id,
      session_revision: envelope.session_revision,
      worker_request_id: envelope.worker_request_id,
      tool_call_id: envelope.tool_call_id,
      tool_name: envelope.tool_name,
      namespace: envelope.namespace,
      server_id: envelope.server_id,
      operation: envelope.operation,
      arguments: { url: "https://example.test/allow" },
      permit_id: response.permit_id,
    };
    const first = object(await port.runtime.dispatch(request(
      "permission.claim",
      external,
      "claim-once",
    )));
    assert.equal(first.claimed, true);
    const replay = object(await port.runtime.dispatch(request(
      "permission.claim",
      external,
      "claim-replay",
    )));
    assert.equal(replay.claimed, false);
  } finally {
    await port.runtime.close();
    await rm(port.root, { recursive: true, force: true });
  }
});

test("session-scoped approval is exact and cannot cross session or wildcard arguments", async () => {
  const port = await openRuntime("session-scope");
  try {
    const envelope = await approval(port.runtime, "literal-*");
    const responseId = "console-response-session";
    const proof = consoleProof(envelope, responseId, "allow", "session");
    const response = object(await port.runtime.dispatch(request(
      "permission.respond",
      {
        request_id: envelope.request_id,
        response_id: responseId,
        effect: "allow",
        decision_scope: "session",
        responder: "zyra-cli",
        metadata: { console_response: proof },
      },
      "respond-session",
    )));
    assert.equal(response.decision_scope, "session");
    assert.equal(object(response.scope_rule as JsonValue).installed, true);

    const repeated = object(await port.runtime.dispatch(request(
      "permission.enforce",
      {
        run_id: "console-run",
        task_id: "console-task",
        session_id: "console-session",
        session_revision: 12,
        worker_request_id: "console-worker-repeat",
        tool_call_id: "console-call-repeat",
        tool_name: "open_url",
        namespace: "browser",
        server_id: "",
        operation: "execute",
        arguments: { url: "https://example.test/literal-*" },
      },
      "repeat-session",
    )));
    assert.equal(repeated.allowed, true);
    assert.equal(repeated.pending_approval, false);

    const wildcardExpansion = object(await port.runtime.dispatch(request(
      "permission.enforce",
      {
        run_id: "console-run",
        task_id: "console-task",
        session_id: "console-session",
        session_revision: 12,
        worker_request_id: "console-worker-wildcard",
        tool_call_id: "console-call-wildcard",
        tool_name: "open_url",
        namespace: "browser",
        server_id: "",
        operation: "execute",
        arguments: { url: "https://example.test/literal-attacker" },
        await_approval_delivery: true,
      },
      "wildcard-session",
    )));
    assert.equal(wildcardExpansion.allowed, false);
    assert.equal(wildcardExpansion.pending_approval, true);

    const otherSession = object(await port.runtime.dispatch(request(
      "permission.enforce",
      {
        run_id: "console-run",
        task_id: "console-task",
        session_id: "console-session-other",
        session_revision: 1,
        worker_request_id: "console-worker-other-session",
        tool_call_id: "console-call-other-session",
        tool_name: "open_url",
        namespace: "browser",
        server_id: "",
        operation: "execute",
        arguments: { url: "https://example.test/literal-*" },
        await_approval_delivery: true,
      },
      "other-session",
    )));
    assert.equal(otherSession.allowed, false);
    assert.equal(otherSession.pending_approval, true);
  } finally {
    await port.runtime.close();
    await rm(port.root, { recursive: true, force: true });
  }
});

test("workspace-scoped approval persists across runtime restart", async () => {
  const port = await openRuntime("workspace-scope");
  try {
    const envelope = await approval(port.runtime, "workspace");
    const responseId = "console-response-workspace";
    const proof = consoleProof(envelope, responseId, "allow", "workspace");
    const response = object(await port.runtime.dispatch(request(
      "permission.respond",
      {
        request_id: envelope.request_id,
        response_id: responseId,
        effect: "allow",
        decision_scope: "workspace",
        responder: "zyra-cli",
        metadata: { console_response: proof },
      },
      "respond-workspace",
    )));
    assert.equal(response.decision_scope, "workspace");
    assert.equal(object(response.scope_rule as JsonValue).persistent, true);
    await port.runtime.close();
    port.runtime = await E02ApiPortRuntime.open({
      type: "initialize",
      request_id: "initialize-workspace-scope-restored",
      workspace_root: join(port.root, "workspace"),
      state_path: join(port.root, "state", "e02.json"),
      artifact_root: join(port.root, "artifacts"),
      permission_mode: "default",
      sealed_autonomous: false,
      runtime_constraints: { projectRoot: resolve("."), test_label: "workspace-scope-restored" },
    });
    const repeated = object(await port.runtime.dispatch(request(
      "permission.enforce",
      {
        run_id: "console-run-restored",
        task_id: "console-task-restored",
        session_id: "console-session-restored",
        session_revision: 1,
        worker_request_id: "console-worker-restored",
        tool_call_id: "console-call-restored",
        tool_name: "open_url",
        namespace: "browser",
        server_id: "",
        operation: "execute",
        arguments: { url: "https://example.test/workspace" },
      },
      "repeat-workspace-restored",
    )));
    assert.equal(repeated.allowed, true);
    assert.equal(repeated.pending_approval, false);
  } finally {
    await port.runtime.close();
    await rm(port.root, { recursive: true, force: true });
  }
});

test("console proof rejects stale exact binding before permission continuation", async () => {
  const port = await openRuntime("tampered");
  try {
    const envelope = await approval(port.runtime, "tampered");
    const responseId = "console-response-tampered";
    const proof = consoleProof(envelope, responseId, "deny");
    proof.arguments_digest = "f".repeat(64);
    const material = { ...proof };
    delete material.proof;
    proof.proof = digest(material);
    await assert.rejects(
      () => port.runtime.dispatch(request(
        "permission.respond",
        {
          request_id: envelope.request_id,
          response_id: responseId,
          effect: "deny",
          responder: "zyra-web-operator",
          metadata: { console_response: proof },
        },
        "respond-tampered",
      )),
      (error: unknown) => {
        const code =
          error && typeof error === "object"
            ? String((error as Record<string, unknown>).code ?? "")
            : "";
        assert.equal(
          code,
          "permission_response_arguments_digest_mismatch",
        );
        return true;
      },
    );
    const current = object(await port.runtime.dispatch(request(
      "permission.get",
      { view: "request", request_id: envelope.request_id },
      "get-after-tamper",
    )));
    const visible = object(
      (current.requests as JsonValue[])[0],
    );
    assert.equal(visible.status, "delivered");
    assert.equal(visible.response_accepted, false);
  } finally {
    await port.runtime.close();
    await rm(port.root, { recursive: true, force: true });
  }
});
