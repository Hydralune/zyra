import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "bun:test";

import {
  AcpPermissionTransport,
  PermissionContinuationRuntime,
  PermissionEvaluator,
  PermissionHookRuntime,
  PermissionIdentity,
  PermissionJournal,
  PermissionModeRuntime,
  PermissionRiskRuntime,
  PermissionRuleIndex,
  PermissionRuleParser,
  StandingGrantRuntime,
  defaultScope,
  type PermissionIdentityInput,
} from "../../src/permission/index.ts";
import type {
  E02RuntimeIdentity,
  PermissionApprovalResponse,
  PermissionDecisionRecord,
} from "../../src/e02/contracts.ts";
import {
  CommandDescriptorRuntime,
  CommandDispatchRuntime,
  CommandRegistryRuntime,
  LocalCommandRuntime,
  PluginCacheRuntime,
  PluginHookRuntime,
  PluginRuntime,
  PluginManifestRuntime,
  SkillSourceRuntime,
  SkillFrontmatterRuntime,
  SkillInvocationRuntime,
  SkillRegistryRuntime,
  SkillReloadRuntime,
  SkillResourceRuntime,
  type SkillDescriptor,
  type SkillInvocationRequest,
  type SkillSourceFile,
  type SkillToolScope,
} from "../../src/index.ts";
import {
  HttpMcpTransport,
  McpCapabilityCatalog,
  McpConfigStore,
  McpConnectionRuntime,
  McpElicitationRuntime,
  McpInstructionRuntime,
  McpOAuthRuntime,
  McpSamplingRuntime,
  McpServerPolicy,
  McpTaskRuntime,
  McpPromptProjection,
  McpRequestJournal,
  McpResourceProjection,
  McpSseParser,
  McpTokenVaultPort,
  McpToolProjection,
  StdioMcpTransport,
  type McpOAuthProviderConfig,
} from "../../../../integrations/claude-mcp/src/index.ts";

const instant = "2026-07-17T08:00:00.000Z";
function runtimeFor(id: string): E02RuntimeIdentity {
  return {
    runtimeId: `runtime-${id}`,
    runId: `run-${id}`,
    taskId: `task-${id}`,
    sessionId: `session-${id}`,
    workerRequestId: `worker-${id}`,
    epoch: 1,
  };
}

function permissionInput(id: string, toolName = "file_read"): PermissionIdentityInput {
  return {
    runId: `run-${id}`,
    taskId: `task-${id}`,
    sessionId: `session-${id}`,
    sessionRevision: 2,
    workerRequestId: `worker-${id}`,
    toolCallId: `call-${id}`,
    toolName,
    namespace: toolName === "shell" ? "builtin" : "builtin",
    operation: toolName === "file_read" ? "read" : "execute",
    workspaceRoot: "G:/workspace",
    arguments: toolName === "shell" ? { command: "Get-ChildItem" } : { path: "README.md" },
    metadata: { mutation_id: id },
  };
}

function approvalResponse(requestId: string, id: string): PermissionApprovalResponse {
  return {
    responseId: `response-${id}`,
    requestId,
    runId: `run-${id}`,
    sessionId: `session-${id}`,
    sessionRevision: 2,
    workerRequestId: `worker-${id}`,
    toolCallId: `call-${id}`,
    effect: "allow",
    responder: "mutation-test",
    respondedAt: instant,
    metadata: { mutation_id: id },
  };
}

function askDecision(identity: ReturnType<typeof PermissionIdentity.create>): PermissionDecisionRecord {
  return {
    decisionId: `decision-${identity.context.toolCallId}`,
    effect: "ask",
    requestFingerprint: identity.requestFingerprint,
    policyRevision: 0,
    modeRevision: 0,
  } as unknown as PermissionDecisionRecord;
}

test("e02.mutation.permission-1", () => {
  const rule = new PermissionRuleParser().parse("allow:tool:file_read", { now: instant });
  assert.equal(rule.effect, "allow");
});

test("e02.mutation.permission-2", () => {
  const parser = new PermissionRuleParser();
  const rule = parser.parse("deny:tool:shell", { now: instant });
  assert.match(parser.serialize(rule), /^deny:tool:shell/);
});

test("e02.mutation.permission-3", () => {
  const parser = new PermissionRuleParser();
  const index = new PermissionRuleIndex();
  index.replace([parser.parse("allow:tool:file_read", { now: instant })]);
  assert.equal(index.resolve(PermissionIdentity.create(permissionInput("permission-3"))).winner?.effect, "allow");
});

test("e02.mutation.permission-4", () => {
  const parser = new PermissionRuleParser();
  const index = new PermissionRuleIndex();
  assert.equal(index.replace([parser.parse("ask:tool:shell", { now: instant })]), 1);
});

test("e02.mutation.permission-5", () => {
  const modes = new PermissionModeRuntime();
  const transition = modes.transition("sealed", { changedBy: "test", reason: "mutation boundary", at: instant });
  assert.equal(transition.to, "sealed");
  assert.equal(modes.state.interactive, false);
});

test("e02.mutation.permission-6", () => {
  const assessment = new PermissionRiskRuntime().classify(PermissionIdentity.create(permissionInput("permission-6", "shell")));
  assert.ok(assessment.deterministicSignals.length > 0);
});

test("e02.mutation.permission-7", async () => {
  const identity = PermissionIdentity.create(permissionInput("permission-7"));
  const risk = new PermissionRiskRuntime().classify(identity);
  const result = await new PermissionHookRuntime(() => instant).runBeforeTool(identity, risk);
  assert.equal(result.identity.requestFingerprint, identity.requestFingerprint);
});

test("e02.mutation.permission-8", () => {
  const hooks = new PermissionHookRuntime(() => instant);
  const identity = PermissionIdentity.create(permissionInput("permission-8"));
  const rebound = hooks.validateMutation(identity, { path: "docs/guide.md" });
  assert.notEqual(rebound.argumentsDigest, identity.argumentsDigest);
  assert.equal(rebound.context.toolCallId, identity.context.toolCallId);
});

test("e02.mutation.permission-9", () => {
  const evaluator = new PermissionEvaluator({ runtime: runtimeFor("permission-9"), clock: () => instant });
  const decision = evaluator.evaluate(permissionInput("permission-9"));
  assert.equal(decision.canonicalOwner, "typescript");
});

test("e02.mutation.permission-10", () => {
  const evaluator = new PermissionEvaluator({ runtime: runtimeFor("permission-10"), clock: () => instant });
  const identity = PermissionIdentity.create(permissionInput("permission-10"));
  const risk = evaluator.risks.classify(identity);
  const hookResult = evaluator.hooks.runBeforeToolSync(identity, risk);
  const decision = evaluator.finalize(identity, hookResult, risk);
  assert.equal(decision.requestFingerprint, identity.requestFingerprint);
});

test("e02.mutation.permission-11", () => {
  const grants = new StandingGrantRuntime(() => instant);
  const identity = PermissionIdentity.create(permissionInput("permission-11"));
  grants.issue({
    sessionId: identity.context.sessionId,
    workspaceRoot: identity.context.workspaceRoot,
    scope: defaultScope(),
    issuedForDecisionId: "decision-permission-11",
    expiresAt: "2026-07-18T08:00:00.000Z",
    maxUses: 2,
  });
  assert.equal(grants.consume(identity)?.consumed, true);
});

test("e02.mutation.permission-12", () => {
  const grants = new StandingGrantRuntime(() => instant);
  const grant = grants.issue({
    sessionId: "session-permission-12",
    workspaceRoot: "G:/workspace",
    scope: defaultScope(),
    issuedForDecisionId: "decision-permission-12",
    expiresAt: "2026-07-18T08:00:00.000Z",
    maxUses: 1,
  });
  assert.equal(grants.revoke(grant.grantId, "test", grant.revision).revocationReason, "test");
});

test("e02.mutation.permission-13", () => {
  const continuations = new PermissionContinuationRuntime(() => instant);
  const identity = PermissionIdentity.create(permissionInput("permission-13"));
  const continuation = continuations.park(identity, askDecision(identity), { ttlMs: 60_000 });
  assert.equal(continuation.status, "pending");
});

test("e02.mutation.permission-14", () => {
  const continuations = new PermissionContinuationRuntime(() => instant);
  const identity = PermissionIdentity.create(permissionInput("permission-14"));
  const continuation = continuations.park(identity, askDecision(identity), { ttlMs: 60_000 });
  const result = continuations.resume(approvalResponse(continuation.requestId, "permission-14"), {
    policyRevision: 0,
    modeRevision: 0,
  });
  assert.equal(result.accepted, true);
});

test("e02.mutation.permission-15", () => {
  const continuations = new PermissionContinuationRuntime(() => instant);
  const result = continuations.rejectResponse(approvalResponse("missing-request", "permission-15"), "unknown", "not found");
  assert.equal(result.accepted, false);
  assert.equal(result.exactBinding, false);
});

test("e02.mutation.permission-16", () => {
  const identity = PermissionIdentity.create(permissionInput("permission-16"));
  const prepared = new PermissionJournal(runtimeFor("permission-16"), () => instant).prepare(identity, 0);
  assert.equal(prepared.transition.domain, "permission");
});

test("e02.mutation.permission-17", () => {
  const identity = PermissionIdentity.create(permissionInput("permission-17"));
  assert.equal(identity.context.toolCallId, "call-permission-17");
  assert.equal(identity.requestFingerprint.length, 64);
});

test("e02.mutation.permission-18", () => {
  const identity = PermissionIdentity.create(permissionInput("permission-18"));
  const journal = new PermissionJournal(runtimeFor("permission-18"), () => instant);
  const prepared = journal.prepare(identity, 0);
  const receipt = journal.commit(prepared.transition.transitionId, askDecision(identity));
  assert.equal(receipt.transitionId, prepared.transition.transitionId);
  assert.equal(receipt.commitHash.length, 64);
});

test("e02.mutation.permission-19", () => {
  const identity = PermissionIdentity.create(permissionInput("permission-19"));
  const original = new PermissionJournal(runtimeFor("permission-19"), () => instant);
  original.prepare(identity, 0);
  const restored = new PermissionJournal({ ...runtimeFor("permission-19"), epoch: 2 }, () => instant);
  restored.restore(original.snapshot(), 2);
  assert.equal((restored.state().runtime as any).epoch, 2);
  assert.equal((restored.state().pending as unknown[]).length, 1);
});

test("e02.mutation.permission-20", () => {
  const identity = PermissionIdentity.create(permissionInput("permission-20"));
  const continuation = new PermissionContinuationRuntime(() => instant).park(
    identity,
    askDecision(identity),
    { ttlMs: 60_000 },
  );
  const correlated = new AcpPermissionTransport().correlate(continuation, "Allow exact mutation test request?");
  assert.equal(correlated.requestId, continuation.requestId);
  assert.equal(correlated.requestDigest.length, 64);
});

function fakeTransportRequest(id: string) {
  const controller = new AbortController();
  controller.abort("mutation-test");
  return {
    requestId: id,
    method: "tools/list",
    message: { jsonrpc: "2.0", id, method: "tools/list", params: {} },
    timeoutMs: 1_000,
    idempotent: true,
    idempotencyKey: id,
    authorization: null,
    headers: {},
    signal: controller.signal,
    metadata: {},
  } as const;
}

test("e02.mutation.mcp-1", async () => {
  const transport = { transportId: "transport-mcp-1" };
  const owner = {
    record: { phase: "ready", configDigest: "digest-mcp-1", serverId: "peer-mcp-1" },
    initialize: { protocolVersion: "2025-06-18" },
    policyDecision: { effect: "allow" },
    transport,
    connectPromise: null,
  };
  const result = await (McpConnectionRuntime.prototype.connect as any).call(
    { owners: new Map([["peer-mcp-1", owner]]) },
    { serverId: "peer-mcp-1", configDigest: "digest-mcp-1" },
  );
  assert.equal(result.transport, transport);
});

test("e02.mutation.mcp-2", async () => {
  const marker = { record: { serverId: "peer-mcp-2" } };
  const result = await (McpConnectionRuntime.prototype.reconnect as any).call(
    { requireOwner: () => ({ connectPromise: Promise.resolve(marker) }) },
    "peer-mcp-2",
  );
  assert.equal(result, marker);
});

test("e02.mutation.mcp-3", async () => {
  const result = await (McpConnectionRuntime.prototype.disconnect as any).call(
    { owners: new Map() },
    "peer-mcp-3",
  );
  assert.equal(result.metadata.already_absent, true);
});

test("e02.mutation.mcp-4", async () => {
  await assert.rejects(
    (StdioMcpTransport.prototype.start as any).call({ phase: "closing", serverId: "peer-mcp-4" }),
    /closing stdio transport/i,
  );
});

test("e02.mutation.mcp-5", async () => {
  await assert.rejects(
    (StdioMcpTransport.prototype.request as any).call({ serverId: "peer-mcp-5" }, fakeTransportRequest("mcp-5")),
    (error: unknown) => error instanceof Error && error.name === "AbortError",
  );
});

test("e02.mutation.mcp-6", async () => {
  await assert.rejects(
    (HttpMcpTransport.prototype.request as any).call({ serverId: "peer-mcp-6" }, fakeTransportRequest("mcp-6")),
    (error: unknown) => error instanceof Error && error.name === "AbortError",
  );
});

test("e02.mutation.mcp-7", () => {
  const events = new McpSseParser().push("id: mcp-7\nevent: message\ndata: {\"ok\":true}\n\n");
  assert.equal(events[0]?.eventId, "mcp-7");
});

function oauthProvider(id: string): McpOAuthProviderConfig {
  return {
    providerId: `provider-${id}`,
    serverId: `server-${id}`,
    issuer: "https://identity.example.test",
    authorizationEndpoint: "https://identity.example.test/authorize",
    tokenEndpoint: "https://identity.example.test/token",
    registrationEndpoint: null,
    revocationEndpoint: null,
    clientId: "zyra-client",
    clientSecretHandle: null,
    clientAuthentication: "none",
    redirectUri: "http://127.0.0.1:8471/callback",
    scopes: ["mcp.read"],
    resource: null,
    audience: null,
    allowDynamicRegistration: false,
    tokenEndpointHeaders: {},
    authorizationParameters: {},
    clockSkewSeconds: 30,
    metadata: { mutation_id: id },
  };
}

function oauthRuntime(id: string): McpOAuthRuntime {
  const oauth = new McpOAuthRuntime({
    vault: new McpTokenVaultPort({ now: () => new Date(instant) }),
    now: () => new Date(instant),
  });
  oauth.register(oauthProvider(id));
  return oauth;
}

test("e02.mutation.mcp-8", () => {
  const oauth = oauthRuntime("mcp-8");
  const challenge = oauth.challenge({
    serverId: "server-mcp-8",
    providerId: "provider-mcp-8",
    sessionId: "session-mcp-8",
    requestId: "request-mcp-8",
    challenge: "Bearer",
    returnTo: null,
  });
  assert.match(challenge.authorizationUrl, /code_challenge=/);
});

test("e02.mutation.mcp-9", async () => {
  const oauth = oauthRuntime("mcp-9");
  await assert.rejects(oauth.callback({
    state: "unknown-state",
    code: "code",
    error: null,
    errorDescription: null,
    sessionId: "session-mcp-9",
    requestId: "request-mcp-9",
  }), /state does not match/i);
});

test("e02.mutation.mcp-10", async () => {
  const oauth = oauthRuntime("mcp-10");
  await assert.rejects(oauth.refresh("provider-mcp-10"), /refresh token is not available/i);
});

test("e02.mutation.mcp-11", () => {
  const oauth = oauthRuntime("mcp-11");
  assert.equal(oauth.poisonClient("provider-mcp-11", "invalid client").reason, "invalid client");
});

test("e02.mutation.mcp-12", async () => {
  const vault = new McpTokenVaultPort({ now: () => new Date(instant) });
  const metadata = await vault.store({
    binding: {
      serverId: "server-mcp-12",
      providerId: "provider-mcp-12",
      subject: "subject-mcp-12",
      audience: "audience-mcp-12",
      scopes: ["mcp.read"],
      clientId: "zyra-client",
      kind: "access_token",
    },
    value: "secret-mcp-12",
  });
  assert.equal(metadata.revision, 1);
});

function catalogInput(serverId: string) {
  return {
    serverId,
    connectionId: `connection-${serverId}`,
    connectionEpoch: 1,
    initialize: {
      protocolVersion: "2025-06-18",
      capabilities: {
        experimental: {}, logging: {}, completions: {}, prompts: { listChanged: true },
        resources: { subscribe: true, listChanged: true }, tools: { listChanged: true },
        tasks: { list: true, cancel: true, requests: {} },
      },
      serverInfo: { name: serverId, title: serverId, version: "1.0.0", websiteUrl: null, icons: [] },
      instructions: `Instructions for ${serverId}`,
      meta: {},
    },
    tools: [{
      name: "inspect",
      title: "Inspect",
      description: "Inspect state",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      outputSchema: { type: "object", properties: { ok: { type: "boolean" } } },
      annotations: { title: "Inspect", readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
      icons: [],
      meta: {},
    }],
    resources: [{
      uri: `memo://${serverId}/state`, name: "state", title: "State", description: "State resource",
      mimeType: "application/json", size: 10,
      annotations: { audience: ["assistant"], priority: 0.5, lastModified: instant }, icons: [], meta: {},
    }],
    resourceTemplates: [{
      uriTemplate: `memo://${serverId}/{name}`, name: "by-name", title: "By name", description: "Named resource",
      mimeType: "application/json", annotations: { audience: ["assistant"], priority: 0.5, lastModified: null }, icons: [], meta: {},
    }],
    prompts: [{
      name: "explain", title: "Explain", description: "Explain state",
      arguments: [{ name: "subject", title: "Subject", description: "Subject", required: false }], icons: [], meta: {},
    }],
    expectedRevision: undefined,
    metadata: {},
  } as any;
}

function catalogServer(serverId: string) {
  const catalog = new McpCapabilityCatalog({ now: () => new Date(instant) });
  catalog.applySnapshot(catalogInput(serverId));
  return { catalog, server: catalog.require(serverId) };
}

test("e02.mutation.mcp-13", () => {
  const catalog = new McpCapabilityCatalog({ now: () => new Date(instant) });
  assert.equal(catalog.applySnapshot(catalogInput("server-mcp-13")).revision, 1);
});

test("e02.mutation.mcp-14", () => {
  const { catalog } = catalogServer("server-mcp-14");
  const diff = catalog.applyNotification({
    serverId: "server-mcp-14",
    connectionId: "connection-server-mcp-14",
    connectionEpoch: 1,
    method: "notifications/tools/list_changed",
    params: {},
    observedAt: instant,
    metadata: {},
  });
  assert.deepEqual(diff.invalidated, ["tool"]);
});

test("e02.mutation.mcp-15", () => {
  const { catalog } = catalogServer("server-mcp-15");
  assert.equal(catalog.remove("server-mcp-15", "mutation")?.kind, "server_removed");
});

test("e02.mutation.mcp-16", () => {
  const { server } = catalogServer("server-mcp-16");
  assert.equal(new McpToolProjection().materialize(server)[0]?.originalName, "inspect");
});

test("e02.mutation.mcp-17", () => {
  const { server } = catalogServer("server-mcp-17");
  assert.equal(new McpResourceProjection().materialize(server).resources[0]?.name, "state");
});

test("e02.mutation.mcp-18", () => {
  const { server } = catalogServer("server-mcp-18");
  assert.equal(new McpPromptProjection().materialize(server)[0]?.originalName, "explain");
});

test("e02.mutation.mcp-19", () => {
  const instructions = new McpInstructionRuntime({ now: () => new Date(instant) });
  const delta = instructions.applyDelta({
    serverId: "server-mcp-19",
    connectionId: "connection-mcp-19",
    connectionEpoch: 1,
    catalogRevision: 1,
    instructions: "Keep operations read only.",
    metadata: {},
  });
  assert.equal(delta.operation, "add");
});

function requestJournalInput(id: string) {
  return {
    identity: {
      runId: `run-${id}`,
      taskId: `task-${id}`,
      sessionId: `session-${id}`,
      sessionRevision: 1,
      workerRequestId: `worker-${id}`,
      toolCallId: `call-${id}`,
      serverId: `server-${id}`,
      connectionId: `connection-${id}`,
      connectionEpoch: 1,
      requestId: `request-${id}`,
      method: "tools/call",
    },
    message: { jsonrpc: "2.0", id: `request-${id}`, method: "tools/call", params: { name: "inspect", arguments: {} } } as any,
    arguments: {},
    idempotent: true,
    idempotencyKey: `idempotency-${id}`,
    metadata: {},
  };
}

test("e02.mutation.mcp-20", () => {
  const journal = new McpRequestJournal({ now: () => new Date(instant) });
  assert.equal(journal.prepare(requestJournalInput("mcp-20")).status, "prepared");
});

test("e02.mutation.mcp-21", () => {
  const journal = new McpRequestJournal({ now: () => new Date(instant) });
  const prepared = journal.prepare(requestJournalInput("mcp-21"));
  const receipt = journal.recordEffect({
    journalId: prepared.journalId,
    transitionId: prepared.transitionId,
    effectKind: "remote-read",
    effectKey: "inspect",
    payload: { observed: true },
    reversible: true,
  });
  assert.equal(receipt.journalId, prepared.journalId);
});

test("e02.mutation.mcp-22", () => {
  const journal = new McpRequestJournal({ now: () => new Date(instant) });
  const prepared = journal.prepare(requestJournalInput("mcp-22"));
  const committed = journal.commit({
    journalId: prepared.journalId,
    transitionId: prepared.transitionId,
    status: "committed",
    response: { jsonrpc: "2.0", id: "request-mcp-22", result: { ok: true } },
    failure: null,
  });
  assert.equal(committed.status, "committed");
});

test("e02.mutation.mcp-23", () => {
  const store = new McpConfigStore();
  assert.throws(() => store.merge({} as any, 1), /revision/i);
});

test("e02.mutation.mcp-24", () => {
  const policy = new McpServerPolicy();
  assert.throws(() => policy.evaluate({
    serverId: "policy-a",
    config: { serverId: "policy-b" },
  } as any), /differs from config/i);
});

test("e02.mutation.mcp-25", () => {
  const original = new McpRequestJournal({ now: () => new Date(instant) });
  original.prepare(requestJournalInput("mcp-25"));
  const restored = new McpRequestJournal({ now: () => new Date(instant) });
  restored.restore(original.snapshot());
  assert.equal(restored.snapshot().records.length, 1);
});

test("e02.mutation.mcp-26", async () => {
  const sampling = new McpSamplingRuntime({
    provider: async () => {
      throw new Error("provider must not be reached");
    },
  });
  await assert.rejects(sampling.sample({} as any, { maxTokens: 1 } as any), /identity is incomplete/i);
});

function elicitationIdentity(id: string) {
  return {
    runId: `run-${id}`,
    taskId: `task-${id}`,
    sessionId: `session-${id}`,
    sessionRevision: 1,
    workerRequestId: `worker-${id}`,
    toolCallId: `call-${id}`,
    serverId: `server-${id}`,
    connectionId: `connection-${id}`,
    connectionEpoch: 1,
    requestId: `request-${id}`,
  };
}

test("e02.mutation.mcp-27", () => {
  const elicitation = new McpElicitationRuntime({ allowUrlMode: false });
  assert.throws(() => elicitation.request(elicitationIdentity("mcp-27"), {
    mode: "url",
    message: "Open callback",
    url: "https://identity.example.test/callback",
    elicitationId: "remote-mcp-27",
    meta: {},
  }), /disabled/i);
});

test("e02.mutation.mcp-28", () => {
  const elicitation = new McpElicitationRuntime();
  assert.throws(() => elicitation.resume({
    ...elicitationIdentity("mcp-28"),
    elicitationId: "missing-elicitation",
    continuationId: "missing-continuation",
    result: { action: "decline", content: null, meta: {} },
  }), /not found|unknown/i);
});

test("e02.mutation.mcp-29", async () => {
  const tasks = new McpTaskRuntime();
  await assert.rejects(tasks.poll("server-mcp-29", "missing-task", {} as any), /not found/i);
});

test("e02.mutation.mcp-30", async () => {
  const tasks = new McpTaskRuntime();
  await assert.rejects(tasks.cancel("server-mcp-30", "missing-task", {} as any), /not found/i);
});

const skillMarkdown = `---
name: mutation-skill
description: Mutation contract skill
---
Inspect the target and report the observed state.`;

function skillSource(path: string): SkillSourceFile {
  return {
    sourceId: "mutation-source",
    sourceKind: "project",
    sourcePriority: 300,
    pluginId: null,
    skillDirectory: join(path, ".."),
    manifestPath: path,
    relativePath: "SKILL.md",
    realPath: path,
    sizeBytes: Buffer.byteLength(skillMarkdown),
    modifiedAtMs: 1,
    inode: null,
    contentDigest: "mutation-content",
    discoveredAt: instant,
    metadata: {},
  };
}

function skillDescriptor(root = "G:/workspace/skills/mutation-skill"): SkillDescriptor {
  const source = skillSource(join(root, "SKILL.md"));
  return new SkillFrontmatterRuntime({ now: () => new Date(instant) }).parseText(source, skillMarkdown);
}

function toolScope(overrides: Partial<SkillToolScope> = {}): SkillToolScope {
  return {
    allowed: ["*"],
    denied: [],
    namespaces: ["*"],
    mcpServers: ["*"],
    readOnly: false,
    inheritParent: true,
    maximumCalls: 10,
    maximumParallel: 2,
    requireApproval: [],
    ...overrides,
  };
}

function skillRuntime(root: string) {
  const descriptor = skillDescriptor(root);
  const registry = new SkillRegistryRuntime({ now: () => new Date(instant) });
  registry.commitRevision({ baseRevision: 0, descriptors: [descriptor] });
  const resources = new SkillResourceRuntime({ workspaceRoot: root, allowOutsideWorkspace: true, now: () => new Date(instant) });
  const invocation = new SkillInvocationRuntime({
    registry,
    resources,
    now: () => new Date(instant),
    executor: async () => ({ output: { ok: true }, inputTokens: 1, outputTokens: 1 }),
  });
  return { descriptor, registry, resources, invocation };
}

test("e02.mutation.skill-1", async () => {
  const root = await mkdtemp(join(tmpdir(), "zyra-e02-skill-1-"));
  const path = join(root, "SKILL.md");
  try {
    await writeFile(path, skillMarkdown, "utf8");
    assert.equal((await new SkillFrontmatterRuntime().parse(skillSource(path))).name, "mutation-skill");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("e02.mutation.skill-2", () => {
  const registry = new SkillRegistryRuntime({ now: () => new Date(instant) });
  assert.equal(registry.commitRevision({ baseRevision: 0, descriptors: [skillDescriptor()] }).revision, 1);
});

test("e02.mutation.skill-3", () => {
  const registry = new SkillRegistryRuntime({ now: () => new Date(instant) });
  registry.commitRevision({ baseRevision: 0, descriptors: [skillDescriptor()] });
  assert.equal(registry.resolve("mutation-skill").skillId, "mutation-skill");
});

test("e02.mutation.skill-4", async () => {
  const descriptor = skillDescriptor();
  const resources = new SkillResourceRuntime({ workspaceRoot: "G:/workspace", allowOutsideWorkspace: true });
  assert.deepEqual(await resources.load(descriptor), []);
});

test("e02.mutation.skill-required-resources-survive-optional-selection", async () => {
  const root = await mkdtemp(join(tmpdir(), "zyra-e02-required-resource-"));
  try {
    await writeFile(join(root, "required.md"), "required evidence", "utf8");
    await writeFile(join(root, "optional.md"), "optional notes", "utf8");
    const base = skillDescriptor(root);
    const descriptor: SkillDescriptor = {
      ...base,
      resources: [{
        resourceId: "required-evidence",
        path: "required.md",
        kind: "markdown",
        required: true,
        maximumBytes: 4_096,
        charset: "utf-8",
        mediaType: "text/markdown",
        digest: null,
        metadata: {},
      }, {
        resourceId: "optional-notes",
        path: "optional.md",
        kind: "markdown",
        required: false,
        maximumBytes: 4_096,
        charset: "utf-8",
        mediaType: "text/markdown",
        digest: null,
        metadata: {},
      }],
    };
    const resources = new SkillResourceRuntime({ workspaceRoot: root });
    const selected = await resources.load(descriptor, ["missing-optional"]);
    assert.deepEqual(selected.map((resource) => resource.resourceId), ["required-evidence"]);
    assert.equal(selected[0]?.text, "required evidence");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("e02.mutation.skill-5", async () => {
  const { invocation, registry } = skillRuntime("G:/workspace");
  const request: SkillInvocationRequest = {
    identity: {
      runId: "run-skill-5", taskId: "task-skill-5", sessionId: "session-skill-5", sessionRevision: 1,
      workerRequestId: "worker-skill-5", toolCallId: "call-skill-5", invocationId: "",
    },
    skillName: "mutation-skill",
    registryRevision: registry.revision,
    arguments: {},
    parentContext: {},
    parentToolScope: toolScope(),
    workspaceRoot: "G:/workspace",
    metadata: {},
  };
  assert.equal((await invocation.invoke(request)).status, "completed");
});

test("e02.mutation.skill-6", () => {
  const { descriptor, invocation } = skillRuntime("G:/workspace");
  const result = invocation.applyContext(descriptor, { system: "system" }, [], {});
  assert.equal(result.context.skill_id, descriptor.skillId);
});

test("e02.mutation.skill-7", () => {
  const { invocation } = skillRuntime("G:/workspace");
  const scope = invocation.applyToolScope(toolScope({ allowed: ["file_*"] }), toolScope({ allowed: ["file_read"], maximumCalls: 3 }));
  assert.deepEqual(scope.allowed, ["file_read"]);
  assert.equal(scope.maximumCalls, 3);
});

function reloadRuntime() {
  const registry = new SkillRegistryRuntime({ now: () => new Date(instant) });
  const sources = { discover: async () => ({ files: [], errors: [] }) } as any;
  const reload = new SkillReloadRuntime({
    sources,
    parser: new SkillFrontmatterRuntime({ now: () => new Date(instant) }),
    registry,
    now: () => new Date(instant),
  });
  return { registry, reload };
}

test("e02.mutation.skill-8", async () => {
  const { reload } = reloadRuntime();
  const scan = await reload.scan([]);
  assert.equal(scan.baseRevision, 0);
});

test("e02.mutation.skill-9", async () => {
  const { registry, reload } = reloadRuntime();
  const scan = await reload.scan([]);
  assert.equal(reload.commit(scan.scanId, registry.revision).revision, 1);
});

test("e02.mutation.skill-10", async () => {
  const root = await mkdtemp(join(tmpdir(), "zyra-e02-plugin-"));
  const path = join(root, "plugin.json");
  try {
    await writeFile(path, JSON.stringify({
      manifest_version: 1,
      id: "mutation-plugin",
      name: "Mutation Plugin",
      version: "1.0.0",
      skills: [],
    }), "utf8");
    const manifest = await new PluginManifestRuntime({ workspaceRoot: root }).parse(path, "project");
    assert.equal(manifest.pluginId, "mutation-plugin");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("e02.mutation.skill-11", async () => {
  const discovery = await new SkillSourceRuntime({ now: () => new Date(instant) }).discover([]);
  assert.equal(discovery.files.length, 0);
  assert.equal(discovery.errors.length, 0);
});

test("e02.mutation.skill-12", async () => {
  await assert.rejects(
    (PluginRuntime.prototype.load as any).call({
      manifests: { parse: async () => { throw new Error("manifest parse boundary"); } },
    }, "missing-plugin.json", "project"),
    /manifest parse boundary/i,
  );
});

test("e02.mutation.skill-13", async () => {
  await assert.rejects(
    (PluginRuntime.prototype.reload as any).call({
      records: new Map(),
      load: async () => { throw new Error("plugin reload boundary"); },
    }, "missing-plugin", "missing-plugin.json", "project"),
    /plugin reload boundary/i,
  );
});

test("e02.mutation.skill-14", async () => {
  const hooks = new PluginHookRuntime({
    executor: async () => { throw new Error("executor must not be reached"); },
  });
  await assert.rejects(hooks.beforeTool({
    arguments: {},
    argumentsDigest: "invalid-digest",
  } as any), /context arguments digest mismatch/i);
});

test("e02.mutation.skill-15", () => {
  const cache = new PluginCacheRuntime();
  assert.throws(() => cache.commit({
    manifest: {
      pluginId: "mutation-cache-plugin",
      version: "1.0.0",
      manifestDigest: "a".repeat(64),
    } as any,
    sourceDigest: "b".repeat(64),
    compiledCapabilities: {},
    status: "active",
    expectedRevision: 1,
  }), /revision/i);
});

test("e02.mutation.skill-16", async () => {
  const root = await mkdtemp(join(tmpdir(), "zyra-e02-command-parse-"));
  try {
    await assert.rejects(new CommandDescriptorRuntime().parse({
      path: join(root, "missing-command.md"),
      sourceKind: "project",
      sourceId: "mutation-command",
      sourcePriority: 100,
    }), /ENOENT|no such file/i);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("e02.mutation.skill-17", () => {
  const registry = new CommandRegistryRuntime({ now: () => new Date(instant) });
  assert.throws(() => registry.register([], 1), /revision/i);
});

test("e02.mutation.skill-18", async () => {
  await assert.rejects(
    (CommandDispatchRuntime.prototype.dispatch as any).call({}, { identity: {} }),
    /identity is incomplete/i,
  );
});

test("e02.mutation.skill-19", async () => {
  const descriptor = {
    commandId: "mutation-command",
    descriptorDigest: "descriptor-digest",
    permission: {},
  } as any;
  const request = { identity: { commandCallId: "mutation-call" }, sealedAutonomous: false } as any;
  await assert.rejects(
    (CommandDispatchRuntime.prototype.requirePermission as any).call({
      permission: async () => ({
        effect: "allow",
        requestDigest: "incorrect-digest",
      }),
    }, descriptor, request, {}),
    /request digest mismatch/i,
  );
});

test("e02.mutation.skill-20", async () => {
  const local = new LocalCommandRuntime();
  await assert.rejects(local.execute({
    commandId: "mutation-local",
    descriptorDigest: "descriptor-digest",
    handler: { handlerId: "missing-local-handler" },
  } as any, {} as any, {}), /not registered/i);
});
