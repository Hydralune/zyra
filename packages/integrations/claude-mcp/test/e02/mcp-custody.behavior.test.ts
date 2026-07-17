import assert from "node:assert/strict";
import { test } from "bun:test";

import {
  McpCapabilityCatalog,
  McpConfigStore,
  McpElicitationRuntime,
  McpInstructionRuntime,
  McpOAuthRuntime,
  McpPaginationRuntime,
  McpPromptProjection,
  McpRequestJournal,
  McpResourceProjection,
  McpRetryRuntime,
  McpRuntimeError,
  McpSamplingRuntime,
  McpSchemaRuntime,
  McpServerPolicy,
  McpSseParser,
  McpTokenVaultPort,
  McpToolProjection,
  type JsonObject,
  type McpCatalogSnapshotInput,
  type McpOAuthProviderConfig,
  type McpPolicyRule,
  type McpRequestIdentity,
  type McpServerConfigRecord,
} from "../../src/index.ts";
import { McpProtocolCodec } from "../../src/core/protocol.ts";

const instant = "2026-07-17T08:00:00.000Z";

function fixedNow(): Date {
  return new Date(instant);
}

function catalogSnapshot(serverId: string): McpCatalogSnapshotInput {
  return {
    serverId,
    connectionId: `connection-${serverId}`,
    connectionEpoch: 3,
    initialize: {
      protocolVersion: "2025-06-18",
      capabilities: {
        experimental: { zyra: { custody: "typescript" } },
        logging: {},
        completions: {},
        prompts: { listChanged: true },
        resources: { subscribe: true, listChanged: true },
        tools: { listChanged: true },
        tasks: { list: true, cancel: true, requests: {} },
      },
      serverInfo: {
        name: serverId,
        title: `Server ${serverId}`,
        version: "2.1.0",
        websiteUrl: "https://mcp.example.test",
        icons: [],
      },
      instructions: "Inspect state without mutating the remote peer.",
      meta: { canonical_owner: "typescript" },
    },
    tools: [{
      name: "inspect_state",
      title: "Inspect state",
      description: "Read a canonical state snapshot",
      inputSchema: {
        type: "object",
        properties: {
          scope: { type: "string", enum: ["session", "task"] },
          include_history: { type: "boolean", default: false },
        },
        required: ["scope"],
        additionalProperties: false,
      },
      outputSchema: {
        type: "object",
        properties: {
          revision: { type: "integer" },
          digest: { type: "string" },
        },
        required: ["revision", "digest"],
      },
      annotations: {
        title: "Inspect state",
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
      icons: [],
      meta: { operation: "read" },
    }, {
      name: "replace_label",
      title: "Replace label",
      description: "Replace a task label",
      inputSchema: {
        type: "object",
        properties: {
          task_id: { type: "string" },
          label: { type: "string" },
        },
        required: ["task_id", "label"],
        additionalProperties: false,
      },
      outputSchema: {
        type: "object",
        properties: { updated: { type: "boolean" } },
      },
      annotations: {
        title: "Replace label",
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
      icons: [],
      meta: { operation: "write" },
    }],
    resources: [{
      uri: `memo://${serverId}/state`,
      name: "canonical-state",
      title: "Canonical state",
      description: "Current canonical state",
      mimeType: "application/json",
      size: 512,
      annotations: {
        audience: ["assistant"],
        priority: 0.9,
        lastModified: instant,
      },
      icons: [],
      meta: { durable: true },
    }],
    resourceTemplates: [{
      uriTemplate: `memo://${serverId}/task/{task_id}/artifact/{artifact_id}`,
      name: "task-artifact",
      title: "Task artifact",
      description: "Artifact owned by a task",
      mimeType: "application/octet-stream",
      annotations: {
        audience: ["assistant"],
        priority: 0.7,
        lastModified: null,
      },
      icons: [],
      meta: { encoded: true },
    }],
    prompts: [{
      name: "explain_state",
      title: "Explain state",
      description: "Explain a canonical state revision",
      arguments: [{
        name: "revision",
        title: "Revision",
        description: "Revision to explain",
        required: true,
      }, {
        name: "focus",
        title: "Focus",
        description: "Optional explanation focus",
        required: false,
      }],
      icons: [],
      meta: { read_only: true },
    }],
    expectedRevision: undefined,
    metadata: { source: "mcp-custody-behavior" },
  };
}

function oauthProvider(providerId: string, serverId: string): McpOAuthProviderConfig {
  return {
    providerId,
    serverId,
    issuer: "https://identity.example.test",
    authorizationEndpoint: "https://identity.example.test/authorize",
    tokenEndpoint: "https://identity.example.test/token",
    registrationEndpoint: null,
    revocationEndpoint: "https://identity.example.test/revoke",
    clientId: "zyra-e02-client",
    clientSecretHandle: null,
    clientAuthentication: "none",
    redirectUri: "http://127.0.0.1:8471/oauth/callback",
    scopes: ["mcp.read", "mcp.execute"],
    resource: "https://mcp.example.test",
    audience: "zyra-runtime",
    allowDynamicRegistration: false,
    tokenEndpointHeaders: { "x-runtime-owner": "typescript" },
    authorizationParameters: { prompt: "consent" },
    clockSkewSeconds: 0,
    metadata: { execution_id: "E02" },
  };
}

function journalInput(id: string, idempotent = true) {
  const identity: McpRequestIdentity = {
    runId: `run-${id}`,
    taskId: `task-${id}`,
    sessionId: `session-${id}`,
    sessionRevision: 7,
    workerRequestId: `worker-${id}`,
    toolCallId: `call-${id}`,
    serverId: `server-${id}`,
    connectionId: `connection-${id}`,
    connectionEpoch: 3,
    requestId: `request-${id}`,
    method: "tools/call",
  };
  return {
    identity,
    message: {
      jsonrpc: "2.0" as const,
      id: identity.requestId,
      method: identity.method,
      params: {
        name: "inspect_state",
        arguments: { scope: "task" },
      },
    },
    arguments: { scope: "task" },
    idempotent,
    idempotencyKey: idempotent ? `idempotency-${id}` : null,
    metadata: { execution_id: "E02" },
  };
}

function configLayer(layerId: string, revision: number, serverId: string, enabled = true): JsonObject {
  return {
    layerId,
    source: "project",
    revision,
    servers: {
      [serverId]: {
        displayName: `MCP ${serverId}`,
        enabled,
        transport: {
          kind: "streamable_http",
          url: "https://mcp.example.test/rpc",
          headers: { "x-client": "zyra" },
          credentialHandles: {},
          allowedRedirectOrigins: ["https://mcp.example.test"],
          maximumRedirects: 2,
          preferSse: true,
        },
        retry: {
          maximumAttempts: 3,
          initialDelayMs: 1,
          maximumDelayMs: 8,
          multiplier: 2,
          jitterRatio: 0,
          retryStatusCodes: [429, 503],
          retryJsonRpcCodes: [-32001],
        },
        timeouts: {
          connectMs: 2_000,
          initializeMs: 3_000,
          requestMs: 4_000,
          idleMs: 5_000,
          shutdownMs: 1_000,
          authMs: 6_000,
          taskPollMs: 500,
        },
        rateLimit: {
          maximumConcurrent: 4,
          requestsPerMinute: 60,
          burst: 8,
          queueCapacity: 16,
          queueTimeoutMs: 1_000,
        },
        restartPolicy: "on_failure",
        networkPolicy: "allowlisted",
        allowedOperations: ["tools/*", "resources/*", "prompts/*"],
        deniedOperations: ["sampling/*"],
        allowedRoots: ["G:/workspace"],
        authProviderId: "provider-main",
        protocolVersions: ["2025-06-18"],
        autoInitialize: true,
        autoReconnect: true,
        allowSampling: false,
        allowElicitation: true,
        allowTasks: true,
        metadata: { canonical_owner: "typescript" },
      },
    },
    tombstones: [],
    metadata: { execution_id: "E02" },
  };
}

test("e02.custody.mcp.default-path", () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const catalogEvents: string[] = [];
  const catalog = new McpCapabilityCatalog({
    now,
    maximumEvents: 64,
  });
  const unsubscribe = catalog.onEvent((event) => {
    catalogEvents.push(event.kind);
  });
  const first = catalog.applySnapshot(catalogSnapshot("alpha"));
  assert.equal(first.serverId, "alpha");
  assert.equal(first.priorRevision, 0);
  assert.equal(first.revision, 1);
  assert.deepEqual(first.added.tool, ["inspect_state", "replace_label"]);
  assert.deepEqual(first.added.resource, ["memo://alpha/state"]);
  assert.deepEqual(first.added.prompt, ["explain_state"]);
  assert.deepEqual(first.removed.tool, []);
  assert.deepEqual(first.invalidated, []);
  assert.ok(catalogEvents.length >= 1);
  assert.ok(catalogEvents.includes("snapshot_applied"));
  const server = catalog.require("alpha");
  assert.equal(server.serverId, "alpha");
  assert.equal(server.connectionId, "connection-alpha");
  assert.equal(server.connectionEpoch, 3);
  assert.equal(server.revision, 1);
  assert.equal(server.tools.length, 2);
  assert.equal(server.resources.length, 1);
  assert.equal(server.resourceTemplates.length, 1);
  assert.equal(server.prompts.length, 1);
  assert.equal(server.instructions, "Inspect state without mutating the remote peer.");
  assert.equal(catalog.findTool("alpha", "inspect_state")?.annotations?.readOnlyHint, true);
  assert.equal(catalog.findTool("alpha", "replace_label")?.annotations?.readOnlyHint, false);
  assert.equal(catalog.findResource("alpha", "memo://alpha/state")?.name, "canonical-state");
  assert.equal(catalog.findPrompt("alpha", "explain_state")?.arguments.length, 2);
  const tools = new McpToolProjection({
    prefix: "mcp",
    includeServerInstructions: true,
    collisionPolicy: "digest_suffix",
  }).materialize(server);
  assert.equal(tools.length, 2);
  assert.equal(tools[0]?.serverId, "alpha");
  assert.equal(tools[0]?.originalName, "inspect_state");
  assert.equal(tools[0]?.readOnly, true);
  assert.equal(tools[0]?.destructive, false);
  assert.equal(tools[0]?.idempotent, true);
  assert.match(tools[0]?.name ?? "", /^mcp__alpha__/);
  assert.match(tools[0]?.description ?? "", /without mutating/);
  assert.equal(tools[1]?.originalName, "replace_label");
  assert.equal(tools[1]?.readOnly, false);
  const resources = new McpResourceProjection().materialize(server);
  assert.equal(resources.resources.length, 1);
  assert.equal(resources.templates.length, 1);
  assert.equal(resources.resources[0]?.serverId, "alpha");
  assert.equal(resources.resources[0]?.uri, "memo://alpha/state");
  assert.equal(resources.resources[0]?.mimeType, "application/json");
  assert.deepEqual(resources.templates[0]?.variables, ["task_id", "artifact_id"]);
  const resolved = new McpResourceProjection().resolveTemplate(
    resources.templates[0]!,
    {
      task_id: "task 7",
      artifact_id: "result/report.json",
    },
  );
  assert.equal(resolved, "memo://alpha/task/task%207/artifact/result%2Freport.json");
  const prompts = new McpPromptProjection({
    namespace: "mcp",
    maximumNameLength: 80,
  }).materialize(server);
  assert.equal(prompts.length, 1);
  assert.equal(prompts[0]?.serverId, "alpha");
  assert.equal(prompts[0]?.originalName, "explain_state");
  assert.equal(prompts[0]?.arguments.length, 2);
  const promptArguments = new McpPromptProjection().validateArguments(
    prompts[0]!,
    {
      revision: "17",
      focus: "permission journal",
    },
  );
  assert.equal(promptArguments.revision, "17");
  assert.equal(promptArguments.focus, "permission journal");
  const instructions = new McpInstructionRuntime({
    now,
    maximumInstructionBytes: 16_384,
    maximumTotalTokens: 4_096,
  });
  const instructionDelta = instructions.applyDelta({
    serverId: server.serverId,
    connectionId: server.connectionId,
    connectionEpoch: server.connectionEpoch,
    catalogRevision: server.revision,
    instructions: server.instructions,
    metadata: { custody: "typescript" },
  });
  assert.equal(instructionDelta.operation, "add");
  assert.equal(instructionDelta.serverId, "alpha");
  assert.equal(instructions.get("alpha")?.connectionEpoch, 3);
  const composition = instructions.compose(["alpha"]);
  assert.deepEqual(composition.servers, ["alpha"]);
  assert.match(composition.text, /Inspect state/);
  assert.ok(composition.tokenEstimate > 0);
  assert.equal(composition.digest.length, 64);
  const notification = catalog.applyNotification({
    serverId: "alpha",
    connectionId: "connection-alpha",
    connectionEpoch: 3,
    method: "notifications/tools/list_changed",
    params: { reason: "peer-reload" },
    observedAt: now().toISOString(),
    metadata: { custody: "typescript" },
  });
  assert.deepEqual(notification.invalidated, ["tool"]);
  assert.equal(notification.revision, 2);
  assert.deepEqual(catalog.require("alpha").invalidatedKinds, ["tool"]);
  const catalogSnapshotValue = catalog.snapshot();
  const instructionSnapshotValue = instructions.snapshot();
  const restoredCatalog = new McpCapabilityCatalog({
    now,
    snapshot: catalogSnapshotValue,
  });
  const restoredInstructions = new McpInstructionRuntime({
    now,
    snapshot: instructionSnapshotValue,
  });
  assert.equal(restoredCatalog.require("alpha").digest, catalog.require("alpha").digest);
  assert.equal(restoredCatalog.require("alpha").revision, 2);
  assert.deepEqual(restoredCatalog.require("alpha").invalidatedKinds, ["tool"]);
  assert.equal(restoredInstructions.get("alpha")?.digest, instructionDelta.digest);
  assert.equal(restoredInstructions.compose().digest, composition.digest);
  assert.equal(restoredCatalog.list().length, 1);
  assert.equal(restoredCatalog.snapshot().servers.length, 1);
  assert.equal(restoredInstructions.snapshot().records.length, 1);
  const eventCountBeforeUnsubscribe = catalogEvents.length;
  unsubscribe();
  restoredCatalog.applyNotification({
    serverId: "alpha",
    connectionId: "connection-alpha",
    connectionEpoch: 3,
    method: "notifications/resources/list_changed",
    params: {},
    observedAt: now().toISOString(),
    metadata: {},
  });
  assert.equal(catalogEvents.length, eventCountBeforeUnsubscribe);
});

test("e02.custody.mcp.failure-path", async () => {
  const catalog = new McpCapabilityCatalog({
    now: fixedNow,
    maximumEvents: 8,
  });
  const applied = catalog.applySnapshot(catalogSnapshot("failure-peer"));
  assert.equal(applied.revision, 1);
  assert.throws(() => catalog.applySnapshot({
    ...catalogSnapshot("failure-peer"),
    expectedRevision: 0,
  }), /revision/i);
  assert.throws(() => catalog.applyNotification({
    serverId: "failure-peer",
    connectionId: "wrong-connection",
    connectionEpoch: 3,
    method: "notifications/tools/list_changed",
    params: {},
    observedAt: instant,
    metadata: {},
  }), /connection/i);
  assert.throws(() => catalog.applyNotification({
    serverId: "failure-peer",
    connectionId: "connection-failure-peer",
    connectionEpoch: 2,
    method: "notifications/resources/list_changed",
    params: {},
    observedAt: instant,
    metadata: {},
  }), /epoch/i);
  assert.throws(() => catalog.applyNotification({
    serverId: "failure-peer",
    connectionId: "connection-failure-peer",
    connectionEpoch: 3,
    method: "notifications/unknown/list_changed",
    params: {},
    observedAt: instant,
    metadata: {},
  }), /notification/i);
  assert.throws(() => catalog.require("absent-peer"), /not found/i);
  assert.equal(catalog.findTool("absent-peer", "inspect_state"), null);
  assert.equal(catalog.findResource("absent-peer", "memo://absent/state"), null);
  assert.equal(catalog.findPrompt("absent-peer", "explain_state"), null);
  const server = catalog.require("failure-peer");
  const toolProjection = new McpToolProjection({
    prefix: "mcp",
    maximumNameLength: 24,
    collisionPolicy: "reject",
  });
  const projectedTools = toolProjection.materialize(server);
  assert.equal(projectedTools.length, 2);
  const promptProjection = new McpPromptProjection({
    namespace: "mcp",
    maximumNameLength: 24,
  });
  const prompt = promptProjection.materialize(server)[0]!;
  assert.throws(() => promptProjection.validateArguments(prompt, {}), /requires revision/i);
  assert.throws(() => promptProjection.validateArguments(prompt, {
    revision: "4",
    unknown: "not declared",
  }), /unknown|not allowed/i);
  const resourceProjection = new McpResourceProjection();
  const template = resourceProjection.materialize(server).templates[0]!;
  assert.throws(() => resourceProjection.resolveTemplate(template, {
    task_id: "task-1",
  }), /artifact_id/i);
  const ignoredExtraTemplateValue = resourceProjection.resolveTemplate(template, {
    task_id: "task-1",
    artifact_id: "result.json",
    unexpected: "value",
  });
  assert.equal(ignoredExtraTemplateValue, "memo://failure-peer/task/task-1/artifact/result.json");
  const instructions = new McpInstructionRuntime({
    now: fixedNow,
    maximumInstructionBytes: 32,
    maximumTotalTokens: 8,
  });
  assert.throws(() => instructions.applyDelta({
    serverId: "failure-peer",
    connectionId: "connection-failure-peer",
    connectionEpoch: 3,
    catalogRevision: 1,
    instructions: "x".repeat(128),
    metadata: {},
  }), /instruction/i);
  const parser = new McpSseParser({
    now: fixedNow,
    maximumDataLines: 2,
    maximumEventBytes: 1_024,
    maximumBufferBytes: 2_048,
  });
  assert.throws(() => parser.push("data: first\ndata: second\ndata: third\n\n"), /data lines/i);
  parser.reset();
  assert.throws(() => parser.push(`data: ${"x".repeat(2_100)}\n\n`), /bytes|buffer/i);
  const journal = new McpRequestJournal({
    now: fixedNow,
    maximumRecords: 4,
  });
  const prepared = journal.prepare(journalInput("failure", false));
  assert.equal(prepared.status, "prepared");
  assert.throws(() => journal.prepare({
    ...journalInput("failure", false),
    arguments: { scope: "session" },
  }), /identity|payload|bound|conflict/i);
  assert.throws(() => journal.markSent(
    prepared.journalId,
    "wrong-transition",
    3,
  ), /transition/i);
  assert.throws(() => journal.commit({
    journalId: prepared.journalId,
    transitionId: "wrong-transition",
    status: "committed",
    response: {
      jsonrpc: "2.0",
      id: "request-failure",
      result: { ok: true },
    },
    failure: null,
  }), /transition/i);
  const schema = new McpSchemaRuntime({
    maximumDepth: 8,
    maximumIssues: 16,
    coerceScalars: false,
    applyDefaults: false,
    removeAdditional: false,
  });
  const validation = schema.validate({
    type: "object",
    properties: {
      name: { type: "string", minLength: 3 },
      count: { type: "integer", minimum: 1 },
    },
    required: ["name", "count"],
    additionalProperties: false,
  }, {
    name: "x",
    count: 0,
    extra: true,
  });
  assert.equal(validation.valid, false);
  assert.ok(validation.issues.some((issue) => issue.keyword === "minLength"));
  assert.ok(validation.issues.some((issue) => issue.keyword === "minimum"));
  assert.ok(validation.issues.some((issue) => issue.keyword === "additionalProperties"));
  assert.throws(() => schema.require({
    type: "integer",
    minimum: 1,
  }, 0, "positive revision"), /positive revision/i);
  const pagination = new McpPaginationRuntime({
    maximumPages: 3,
    maximumItems: 4,
    rejectDuplicateCursors: true,
  });
  await assert.rejects(pagination.collect(
    "tools/list",
    async () => ({
      items: [{ name: "same" }],
      nextCursor: "cycle",
      metadata: {},
    }),
  ), /cursor/i);
  assert.equal(catalog.remove("failure-peer", "failure-test")?.kind, "server_removed");
  assert.equal(catalog.get("failure-peer"), null);
  assert.equal(catalog.remove("failure-peer", "already-removed"), null);
});

test("MCP OAuth code exchange stores bounded credentials and restores token metadata", async () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const vault = new McpTokenVaultPort({
    now,
    leaseDurationMs: 30_000,
  });
  const requests: {
    url: string;
    body: URLSearchParams;
    headers: Record<string, string>;
  }[] = [];
  const oauth = new McpOAuthRuntime({
    vault,
    now,
    challengeTtlMs: 120_000,
    httpClient: async (input) => {
      requests.push({
        url: input.url,
        body: new URLSearchParams(input.body),
        headers: { ...input.headers },
      });
      return {
        status: 200,
        headers: { "content-type": "application/json" },
        body: {
          access_token: "access-secret-value",
          refresh_token: "refresh-secret-value",
          token_type: "Bearer",
          expires_in: 3_600,
          scope: "mcp.execute mcp.read",
          resource: "https://mcp.example.test",
        },
      };
    },
  });
  const provider = oauth.register(oauthProvider("provider-code", "server-code"));
  assert.equal(provider.providerId, "provider-code");
  assert.equal(provider.serverId, "server-code");
  assert.equal(provider.clientAuthentication, "none");
  assert.deepEqual(provider.scopes, ["mcp.execute", "mcp.read"]);
  const challenge = oauth.challenge({
    serverId: "server-code",
    providerId: "provider-code",
    sessionId: "session-code",
    requestId: "request-code",
    challenge: "Bearer realm=mcp",
    returnTo: "/sessions/session-code",
    requestedScopes: ["mcp.read", "mcp.admin"],
    metadata: { tool_call_id: "call-code" },
  });
  assert.equal(challenge.serverId, "server-code");
  assert.equal(challenge.providerId, "provider-code");
  assert.equal(challenge.sessionId, "session-code");
  assert.equal(challenge.requestId, "request-code");
  assert.equal(challenge.returnTo, "/sessions/session-code");
  assert.equal(challenge.codeChallengeMethod, "S256");
  assert.equal(challenge.consumedAt, null);
  assert.match(challenge.state, /^\[sha256:[0-9a-f]{64}\]$/);
  assert.deepEqual(challenge.scopes, ["mcp.admin", "mcp.execute", "mcp.read"]);
  const authorization = new URL(challenge.authorizationUrl);
  assert.equal(authorization.origin, "https://identity.example.test");
  assert.equal(authorization.pathname, "/authorize");
  assert.equal(authorization.searchParams.get("response_type"), "code");
  assert.equal(authorization.searchParams.get("client_id"), "zyra-e02-client");
  assert.equal(authorization.searchParams.get("redirect_uri"), provider.redirectUri);
  assert.equal(authorization.searchParams.get("code_challenge_method"), "S256");
  assert.equal(authorization.searchParams.get("code_challenge"), challenge.codeChallenge);
  assert.equal(authorization.searchParams.get("resource"), "https://mcp.example.test");
  assert.equal(authorization.searchParams.get("audience"), "zyra-runtime");
  assert.equal(authorization.searchParams.get("prompt"), "consent");
  const rawState = authorization.searchParams.get("state");
  assert.ok(rawState);
  const tokenSet = await oauth.callback({
    state: rawState!,
    code: "authorization-code-value",
    error: null,
    errorDescription: null,
    sessionId: "session-code",
    requestId: "request-code",
  });
  assert.equal(tokenSet.providerId, "provider-code");
  assert.equal(tokenSet.serverId, "server-code");
  assert.equal(tokenSet.tokenType, "Bearer");
  assert.deepEqual(tokenSet.scopes, ["mcp.execute", "mcp.read"]);
  assert.equal(tokenSet.subject, "");
  assert.equal(tokenSet.audience, "https://mcp.example.test");
  assert.equal(tokenSet.revision, 1);
  assert.ok(tokenSet.expiresAt);
  assert.match(tokenSet.accessTokenHandle, /^mcp-credential-/);
  assert.match(tokenSet.refreshTokenHandle ?? "", /^mcp-credential-/);
  assert.equal(requests.length, 1);
  assert.equal(requests[0]?.url, "https://identity.example.test/token");
  assert.equal(requests[0]?.body.get("grant_type"), "authorization_code");
  assert.equal(requests[0]?.body.get("code"), "authorization-code-value");
  assert.equal(requests[0]?.body.get("redirect_uri"), provider.redirectUri);
  assert.ok(requests[0]?.body.get("code_verifier"));
  assert.equal(requests[0]?.body.get("client_id"), "zyra-e02-client");
  assert.equal(requests[0]?.body.get("resource"), "https://mcp.example.test");
  assert.equal(requests[0]?.headers["x-runtime-owner"], "typescript");
  const accessMetadata = await vault.metadata(tokenSet.accessTokenHandle);
  assert.equal(accessMetadata?.binding.serverId, "server-code");
  assert.equal(accessMetadata?.binding.providerId, "provider-code");
  assert.equal(accessMetadata?.binding.kind, "access_token");
  assert.equal(accessMetadata?.revokedAt, null);
  assert.equal(accessMetadata?.revision, 1);
  const refreshMetadata = await vault.metadata(tokenSet.refreshTokenHandle!);
  assert.equal(refreshMetadata?.binding.kind, "refresh_token");
  assert.equal(refreshMetadata?.binding.serverId, "server-code");
  const header = await oauth.authorization("provider-code", 1_000);
  assert.equal(header, "Bearer access-secret-value");
  assert.equal(oauth.get("provider-code")?.accessTokenHandle, tokenSet.accessTokenHandle);
  const snapshot = oauth.snapshot();
  assert.equal(snapshot.version, "zyra.mcp-oauth-runtime/v1");
  assert.equal(snapshot.providers.length, 1);
  assert.equal(snapshot.tokenSets.length, 1);
  assert.equal(snapshot.poisonRecords.length, 0);
  assert.equal(snapshot.digest.length, 64);
  const restored = new McpOAuthRuntime({
    vault,
    now,
    snapshot,
  });
  assert.equal(restored.get("provider-code")?.refreshTokenHandle, tokenSet.refreshTokenHandle);
  assert.equal(await restored.authorization("provider-code", 1_000), "Bearer access-secret-value");
  assert.equal(restored.snapshot().tokenSets[0]?.revision, 1);
  assert.equal(JSON.stringify(snapshot).includes("access-secret-value"), false);
  assert.equal(JSON.stringify(snapshot).includes("refresh-secret-value"), false);
});

test("MCP token vault enforces binding, revision, revocation, lease, and snapshot boundaries", async () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const vault = new McpTokenVaultPort({
    now,
    leaseDurationMs: 15_000,
  });
  const access = await vault.store({
    binding: {
      serverId: "vault-server",
      providerId: "vault-provider",
      subject: "user-17",
      audience: "mcp-runtime",
      scopes: ["mcp.read", "mcp.execute", "mcp.read"],
      clientId: "vault-client",
      kind: "access_token",
    },
    value: "access-value-17",
    expiresAt: "2026-07-18T08:00:00.000Z",
    metadata: { grant: "authorization_code" },
  });
  assert.equal(access.binding.serverId, "vault-server");
  assert.equal(access.binding.providerId, "vault-provider");
  assert.equal(access.binding.subject, "user-17");
  assert.equal(access.binding.audience, "mcp-runtime");
  assert.deepEqual(access.binding.scopes, ["mcp.execute", "mcp.read"]);
  assert.equal(access.binding.clientId, "vault-client");
  assert.equal(access.binding.kind, "access_token");
  assert.equal(access.revision, 1);
  assert.equal(access.revokedAt, null);
  assert.equal(access.expiresAt, "2026-07-18T08:00:00.000Z");
  assert.equal(access.metadata.grant, "authorization_code");
  assert.match(access.handle, /^mcp-credential-/);
  const firstLease = await vault.read(access.handle, {
    serverId: "vault-server",
    providerId: "vault-provider",
    subject: "user-17",
    audience: "mcp-runtime",
    kind: "access_token",
  });
  assert.ok(firstLease);
  assert.equal(firstLease?.handle, access.handle);
  assert.equal(firstLease?.value, "access-value-17");
  assert.equal(firstLease?.metadata.revision, 1);
  assert.equal(firstLease?.metadata.binding.kind, "access_token");
  assert.equal(Date.parse(firstLease!.expiresAt) > Date.parse(firstLease!.leasedAt), true);
  assert.equal(await vault.read(access.handle, {
    serverId: "other-server",
  }), null);
  assert.equal(await vault.read(access.handle, {
    providerId: "other-provider",
  }), null);
  assert.equal(await vault.read(access.handle, {
    subject: "other-user",
  }), null);
  assert.equal(await vault.read(access.handle, {
    kind: "refresh_token",
  }), null);
  const found = await vault.find({
    serverId: "vault-server",
    providerId: "vault-provider",
    scopes: ["mcp.read"],
  });
  assert.equal(found.length, 1);
  assert.equal(found[0]?.handle, access.handle);
  const replaced = await vault.store({
    binding: access.binding,
    value: "access-value-18",
    expectedRevision: 1,
    expiresAt: "2026-07-19T08:00:00.000Z",
    metadata: { grant: "refresh_token" },
  });
  assert.equal(replaced.handle, access.handle);
  assert.equal(replaced.revision, 2);
  assert.equal(replaced.metadata.grant, "refresh_token");
  assert.equal(replaced.expiresAt, "2026-07-19T08:00:00.000Z");
  const replacementLease = await vault.read(access.handle, {
    serverId: "vault-server",
    kind: "access_token",
  });
  assert.equal(replacementLease?.value, "access-value-18");
  assert.equal(replacementLease?.metadata.revision, 2);
  await assert.rejects(vault.store({
    binding: access.binding,
    value: "stale-write",
    expectedRevision: 1,
  }), /revision/i);
  await assert.rejects(vault.revoke(access.handle, 1), /revision/i);
  const revoked = await vault.revoke(access.handle, 2);
  assert.equal(revoked.revision, 3);
  assert.ok(revoked.revokedAt);
  assert.equal(await vault.read(access.handle), null);
  const revokedMatches = await vault.find({
    serverId: "vault-server",
  });
  assert.equal(revokedMatches.length, 1);
  assert.equal(revokedMatches[0]?.handle, access.handle);
  assert.ok(revokedMatches[0]?.revokedAt);
  const snapshot = await vault.snapshot();
  assert.equal(snapshot.version, "zyra.mcp-token-vault/v1");
  assert.equal(snapshot.metadata.length, 1);
  assert.equal(snapshot.metadata[0]?.handle, access.handle);
  assert.equal(snapshot.metadata[0]?.revision, 3);
  assert.equal(snapshot.valuesIncluded, false);
  assert.equal(snapshot.valueDigests[access.handle], revoked.valueDigest);
  assert.equal(JSON.stringify(snapshot).includes("access-value-18"), false);
  await assert.rejects(vault.delete(access.handle, 2), /revision/i);
  assert.equal(await vault.delete(access.handle, 3), true);
  assert.equal(await vault.metadata(access.handle), null);
  assert.equal(await vault.delete(access.handle), false);
  const empty = await vault.snapshot();
  assert.equal(empty.metadata.length, 0);
  assert.deepEqual(empty.valueDigests, {});
  assert.ok(empty.revision > snapshot.revision);
});

test("MCP config and policy resolve layered ownership before any connection attempt", () => {
  const store = new McpConfigStore();
  assert.equal(store.revision, 0);
  assert.equal(store.list().length, 0);
  const initial = store.merge(configLayer("project-main", 1, "policy-server"));
  assert.equal(initial.revision, 1);
  assert.deepEqual(initial.added, ["policy-server"]);
  assert.deepEqual(initial.updated, []);
  assert.deepEqual(initial.removed, []);
  assert.equal(initial.source, "project");
  assert.equal(initial.layerId, "project-main");
  assert.equal(initial.servers.length, 1);
  assert.equal(store.has("policy-server"), true);
  const config = store.require("policy-server");
  assert.equal(config.serverId, "policy-server");
  assert.equal(config.displayName, "MCP policy-server");
  assert.equal(config.enabled, true);
  assert.equal(config.source, "project");
  assert.equal(config.sourceRevision, 1);
  assert.equal(config.transport.kind, "streamable_http");
  assert.equal(config.retry.maximumAttempts, 3);
  assert.equal(config.timeouts.requestMs, 4_000);
  assert.equal(config.rateLimit.maximumConcurrent, 4);
  assert.equal(config.restartPolicy, "on_failure");
  assert.equal(config.networkPolicy, "allowlisted");
  assert.deepEqual(config.allowedOperations, ["tools/*", "resources/*", "prompts/*"]);
  assert.deepEqual(config.deniedOperations, ["sampling/*"]);
  assert.deepEqual(config.allowedRoots, ["G:/workspace"]);
  assert.equal(config.authProviderId, "provider-main");
  assert.deepEqual(config.protocolVersions, ["2025-06-18"]);
  assert.equal(config.autoInitialize, true);
  assert.equal(config.autoReconnect, true);
  assert.equal(config.allowSampling, false);
  assert.equal(config.allowElicitation, true);
  assert.equal(config.allowTasks, true);
  assert.equal(config.metadata.canonical_owner, "typescript");
  assert.equal(config.configDigest.length, 64);
  const rules: McpPolicyRule[] = [{
    ruleId: "deny-writes",
    effect: "deny",
    priority: 900,
    serverPattern: "policy-*",
    transportPattern: "*",
    operationPattern: "tools/call",
    capabilityPattern: "replace_*",
    resourcePattern: "*",
    workspacePattern: "G:/workspace*",
    sourcePattern: "project",
    interactiveOnly: false,
    autonomousOnly: false,
    expiresAt: null,
    reason: "write tool requires a different task plan",
    metadata: { security: "high" },
  }, {
    ruleId: "allow-read-tools",
    effect: "allow",
    priority: 500,
    serverPattern: "policy-*",
    transportPattern: "streamable_http",
    operationPattern: "tools/*",
    capabilityPattern: "inspect_*",
    resourcePattern: "*",
    workspacePattern: "G:/workspace*",
    sourcePattern: "project",
    interactiveOnly: false,
    autonomousOnly: false,
    expiresAt: null,
    reason: "read-only tools are allowed",
    metadata: { security: "low" },
  }, {
    ruleId: "ask-prompts",
    effect: "ask",
    priority: 300,
    serverPattern: "policy-*",
    transportPattern: "*",
    operationPattern: "prompts/get",
    capabilityPattern: "*",
    resourcePattern: "*",
    workspacePattern: "*",
    sourcePattern: "*",
    interactiveOnly: false,
    autonomousOnly: false,
    expiresAt: null,
    reason: "interactive prompt approval",
    metadata: {},
  }];
  const policy = new McpServerPolicy();
  const policySnapshot = policy.replace(rules, 0, {
    interactive: "ask",
    autonomous: "deny",
  });
  assert.equal(policySnapshot.revision, 1);
  assert.equal(policySnapshot.rules.length, 3);
  assert.equal(policySnapshot.rules[0]?.ruleId, "deny-writes");
  assert.equal(policySnapshot.defaultInteractiveEffect, "ask");
  assert.equal(policySnapshot.defaultAutonomousEffect, "deny");
  const baseContext = {
    serverId: "policy-server",
    transport: "streamable_http" as const,
    operation: "tools/call" as const,
    capabilityName: "inspect_state",
    resourceUri: "",
    workspaceRoot: "G:/workspace/project",
    source: "project",
    interactive: true,
    sealedAutonomous: false,
    networkReachable: true,
    authenticated: true,
    config,
    metadata: { task_id: "task-policy" },
  };
  const allowed = policy.evaluate(baseContext);
  assert.equal(allowed.effect, "allow");
  assert.equal(allowed.reasonCode, "matched_allow_rule");
  assert.deepEqual(allowed.matchedRuleIds, ["allow-read-tools"]);
  assert.equal(allowed.policyRevision, 1);
  assert.equal(allowed.policyDigest, policy.digest);
  assert.equal(allowed.requestDigest.length, 64);
  assert.equal(allowed.replanRequired, false);
  assert.equal(allowed.recoveryInput, null);
  assert.equal(allowed.metadata.authenticated, true);
  assert.equal(allowed.metadata.network_reachable, true);
  assert.equal(allowed.metadata.human_intervention_count, 0);
  assert.equal(policy.requireAllowed(baseContext).decisionId, allowed.decisionId);
  const denied = policy.evaluate({
    ...baseContext,
    capabilityName: "replace_label",
  });
  assert.equal(denied.effect, "deny");
  assert.equal(denied.reasonCode, "matched_deny_rule");
  assert.deepEqual(denied.matchedRuleIds, ["deny-writes"]);
  assert.equal(denied.replanRequired, true);
  assert.equal(denied.recoveryInput?.kind, "mcp_policy_denial");
  assert.equal(denied.recoveryInput?.retry_same_request, false);
  assert.equal(denied.recoveryInput?.replan_required, true);
  assert.equal(denied.recoveryInput?.e02_plans_or_routes, false);
  assert.throws(() => policy.requireAllowed({
    ...baseContext,
    capabilityName: "replace_label",
  }), (error: unknown) => error instanceof McpRuntimeError
    && error.failure.code === "matched_deny_rule"
    && error.failure.disposition === "replan");
  const promptAsk = policy.evaluate({
    ...baseContext,
    operation: "prompts/get",
    capabilityName: "explain_state",
  });
  assert.equal(promptAsk.effect, "ask");
  const sealed = policy.evaluate({
    ...baseContext,
    operation: "prompts/get",
    capabilityName: "explain_state",
    interactive: false,
    sealedAutonomous: true,
  });
  assert.equal(sealed.effect, "deny");
  assert.equal(sealed.reasonCode, "sealed_policy_denied_ask");
  assert.equal(sealed.replanRequired, true);
  const offline = policy.evaluate({
    ...baseContext,
    networkReachable: false,
  });
  assert.equal(offline.effect, "deny");
  assert.equal(offline.reasonCode, "network_policy_unreachable");
  const forbiddenByConfig = policy.evaluate({
    ...baseContext,
    operation: "sampling/createMessage",
    capabilityName: "sample",
  });
  assert.equal(forbiddenByConfig.effect, "deny");
  assert.equal(forbiddenByConfig.reasonCode, "operation_denied_by_server_config");
  assert.throws(() => policy.replace([], 0), /revision/i);
  const restoredStore = new McpConfigStore(store.snapshot());
  const restoredPolicy = new McpServerPolicy(policy.snapshot());
  assert.equal(restoredStore.require("policy-server").configDigest, config.configDigest);
  assert.equal(restoredPolicy.evaluate(baseContext).decisionId, allowed.decisionId);
  const disabledLayer = configLayer("project-main", 2, "policy-server", false);
  const disabledResult = store.merge(disabledLayer, 1);
  assert.deepEqual(disabledResult.updated, ["policy-server"]);
  const disabledConfig = store.require("policy-server");
  assert.equal(disabledConfig.enabled, false);
  const disabledDecision = policy.evaluate({
    ...baseContext,
    config: disabledConfig,
  });
  assert.equal(disabledDecision.effect, "deny");
  assert.equal(disabledDecision.reasonCode, "server_disabled");
});

test("MCP request journal fences effect receipts and exact committed replay across restore", () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const journal = new McpRequestJournal({
    now,
    maximumRecords: 32,
  });
  const input = journalInput("journal-main", true);
  const prepared = journal.prepare(input);
  assert.equal(prepared.status, "prepared");
  assert.equal(prepared.identity.runId, "run-journal-main");
  assert.equal(prepared.identity.taskId, "task-journal-main");
  assert.equal(prepared.identity.sessionId, "session-journal-main");
  assert.equal(prepared.identity.sessionRevision, 7);
  assert.equal(prepared.identity.workerRequestId, "worker-journal-main");
  assert.equal(prepared.identity.toolCallId, "call-journal-main");
  assert.equal(prepared.identity.serverId, "server-journal-main");
  assert.equal(prepared.identity.connectionId, "connection-journal-main");
  assert.equal(prepared.identity.connectionEpoch, 3);
  assert.equal(prepared.identity.requestId, "request-journal-main");
  assert.equal(prepared.identity.method, "tools/call");
  assert.equal(prepared.idempotent, true);
  assert.equal(prepared.idempotencyKey, "idempotency-journal-main");
  assert.equal(prepared.attempt, 1);
  assert.equal(prepared.sentAt, null);
  assert.equal(prepared.committedAt, null);
  assert.equal(prepared.response, null);
  assert.equal(prepared.failure, null);
  assert.equal(prepared.effectReceiptIds.length, 0);
  assert.match(prepared.journalId, /^mcp-request-/);
  assert.match(prepared.transitionId, /^mcp-request-transition-/);
  assert.equal(prepared.messageDigest.length, 64);
  assert.equal(prepared.argumentsDigest.length, 64);
  const duplicatePrepare = journal.prepare(input);
  assert.equal(duplicatePrepare.journalId, prepared.journalId);
  assert.equal(duplicatePrepare.transitionId, prepared.transitionId);
  assert.equal(duplicatePrepare.status, "prepared");
  const sent = journal.markSent(
    prepared.journalId,
    prepared.transitionId,
    3,
  );
  assert.equal(sent.status, "sent");
  assert.equal(sent.attempt, 1);
  assert.ok(sent.sentAt);
  assert.equal(sent.identity.connectionEpoch, 3);
  const firstReceipt = journal.recordEffect({
    journalId: prepared.journalId,
    transitionId: prepared.transitionId,
    effectKind: "remote_tool_result",
    effectKey: "server-journal-main:inspect_state:task",
    payload: {
      revision: 19,
      digest: "sha256:state-19",
      read_only: true,
    },
    reversible: true,
    metadata: {
      server_id: "server-journal-main",
      connection_epoch: 3,
    },
  });
  assert.equal(firstReceipt.journalId, prepared.journalId);
  assert.equal(firstReceipt.transitionId, prepared.transitionId);
  assert.equal(firstReceipt.effectKind, "remote_tool_result");
  assert.equal(firstReceipt.effectKey, "server-journal-main:inspect_state:task");
  assert.equal(firstReceipt.reversible, true);
  assert.equal((firstReceipt.payload as JsonObject).revision, 19);
  assert.equal((firstReceipt.payload as JsonObject).read_only, true);
  assert.equal(firstReceipt.effectDigest.length, 64);
  assert.match(firstReceipt.receiptId, /^mcp-effect-/);
  const duplicateReceipt = journal.recordEffect({
    journalId: prepared.journalId,
    transitionId: prepared.transitionId,
    effectKind: "remote_tool_result",
    effectKey: "server-journal-main:inspect_state:task",
    payload: {
      revision: 19,
      digest: "sha256:state-19",
      read_only: true,
    },
    reversible: true,
    metadata: {
      server_id: "server-journal-main",
      connection_epoch: 3,
    },
  });
  assert.equal(duplicateReceipt.receiptId, firstReceipt.receiptId);
  assert.equal(journal.effectsFor(prepared.journalId).length, 1);
  assert.equal(journal.get(prepared.journalId)?.status, "effect_observed");
  assert.deepEqual(journal.get(prepared.journalId)?.effectReceiptIds, [firstReceipt.receiptId]);
  const committed = journal.commit({
    journalId: prepared.journalId,
    transitionId: prepared.transitionId,
    status: "committed",
    response: {
      jsonrpc: "2.0",
      id: "request-journal-main",
      result: {
        content: [{ type: "text", text: "revision 19" }],
        structuredContent: {
          revision: 19,
          digest: "sha256:state-19",
        },
        isError: false,
      },
    },
    failure: null,
    metadata: {
      effect_receipt_id: firstReceipt.receiptId,
      canonical_owner: "typescript",
    },
  });
  assert.equal(committed.status, "committed");
  assert.ok(committed.committedAt);
  assert.equal(committed.failure, null);
  assert.equal((committed.response as { id?: unknown } | null)?.id, "request-journal-main");
  assert.equal(committed.metadata.canonical_owner, "typescript");
  const committedReplay = journal.prepare(input);
  assert.equal(committedReplay.journalId, prepared.journalId);
  assert.equal(committedReplay.status, "committed");
  assert.equal(committedReplay.attempt, 1);
  assert.equal((committedReplay.response as { id?: unknown } | null)?.id, "request-journal-main");
  assert.equal(journal.findByTransition(prepared.transitionId)?.journalId, prepared.journalId);
  assert.equal(journal.findByIdempotencyKey("idempotency-journal-main")?.journalId, prepared.journalId);
  const classification = journal.classifyRestore();
  assert.equal(classification.length, 0);
  const snapshot = journal.snapshot();
  assert.equal(snapshot.version, "zyra.mcp-request-journal/v1");
  assert.equal(snapshot.records.length, 1);
  assert.equal(snapshot.effects.length, 1);
  assert.equal(snapshot.digest.length, 64);
  const restored = new McpRequestJournal({
    now,
    maximumRecords: 32,
    snapshot,
  });
  assert.equal(restored.get(prepared.journalId)?.status, "committed");
  assert.equal(restored.effectsFor(prepared.journalId)[0]?.receiptId, firstReceipt.receiptId);
  assert.equal(restored.classifyRestore().length, 0);
  const restoredReplay = restored.prepare(input);
  assert.equal(restoredReplay.journalId, prepared.journalId);
  assert.equal((restoredReplay.response as { id?: unknown } | null)?.id, "request-journal-main");
  assert.equal(restoredReplay.attempt, 1);
  assert.throws(() => restored.markSent(
    prepared.journalId,
    prepared.transitionId,
    3,
  ), /committed|status/i);
  assert.throws(() => restored.recordEffect({
    journalId: prepared.journalId,
    transitionId: prepared.transitionId,
    effectKind: "remote_tool_result",
    effectKey: "different-effect",
    payload: { revision: 20 },
    reversible: true,
  }), /committed|status/i);
  assert.throws(() => restored.commit({
    journalId: prepared.journalId,
    transitionId: prepared.transitionId,
    status: "failed",
    response: null,
    failure: new McpRuntimeError({
      failureId: "failure-after-commit",
      category: "remote",
      code: "late_failure",
      message: "late failure must not replace a committed response",
      serverId: "server-journal-main",
      operation: "tools/call",
      requestId: "request-journal-main",
      retryable: false,
      disposition: "terminal",
    }).failure,
  }), /committed|status/i);
});

test("MCP protocol, schema, SSE, pagination, and retry utilities fail closed at boundaries", async () => {
  const codec = new McpProtocolCodec({
    maximumMessageBytes: 8_192,
    maximumContentItems: 64,
    maximumPageItems: 64,
    maximumTextBytes: 2_048,
    maximumBinaryBytes: 4_096,
    maximumSchemaBytes: 4_096,
    maximumMetadataBytes: 2_048,
  });
  const request = codec.request(17, "tools/call", {
    name: "inspect_state",
    arguments: { scope: "task" },
  });
  assert.equal(request.jsonrpc, "2.0");
  assert.equal(request.id, 17);
  assert.equal(request.method, "tools/call");
  assert.equal(request.params?.name, "inspect_state");
  const requestText = codec.encode(request);
  assert.equal(requestText.endsWith("\n"), false);
  assert.equal(codec.decodeText(requestText).jsonrpc, "2.0");
  assert.equal(codec.fingerprint(request).length, 64);
  const notification = codec.notification("notifications/progress", {
    progressToken: "progress-17",
    progress: 3,
    total: 10,
    message: "loading catalog",
  });
  assert.equal(notification.jsonrpc, "2.0");
  assert.equal(notification.method, "notifications/progress");
  assert.equal(notification.params?.progress, 3);
  const success = codec.success(17, {
    content: [{ type: "text", text: "ok" }],
    isError: false,
  });
  assert.equal(success.id, 17);
  assert.equal((success.result as JsonObject | undefined)?.isError, false);
  const failure = codec.error(18, -32602, "invalid params", {
    field: "scope",
  });
  assert.equal(failure.id, 18);
  assert.equal(failure.error?.code, -32602);
  assert.equal(failure.error?.message, "invalid params");
  assert.equal((failure.error?.data as JsonObject | undefined)?.field, "scope");
  assert.throws(() => codec.decodeText("not-json"), /JSON|message/i);
  assert.throws(() => codec.decode({
    jsonrpc: "1.0",
    id: 1,
    method: "ping",
  }), /jsonrpc|version/i);
  assert.throws(() => codec.decode({
    jsonrpc: "2.0",
    id: 1,
    method: "x".repeat(3_000),
  }), /string|bytes|method/i);
  const schema = new McpSchemaRuntime({
    maximumDepth: 16,
    maximumIssues: 32,
    coerceScalars: true,
    applyDefaults: true,
    removeAdditional: true,
  });
  const schemaDocument: JsonObject = {
    type: "object",
    properties: {
      enabled: { type: "boolean", default: true },
      count: { type: "integer", minimum: 1, maximum: 10 },
      mode: { type: "string", enum: ["fast", "safe"] },
      tags: {
        type: "array",
        items: { type: "string", minLength: 2 },
        minItems: 1,
        maxItems: 3,
        uniqueItems: true,
      },
      owner: {
        type: "object",
        properties: {
          email: { type: "string", format: "email" },
        },
        required: ["email"],
        additionalProperties: false,
      },
    },
    required: ["enabled", "count", "mode", "tags", "owner"],
    additionalProperties: false,
  };
  const valid = schema.validate(schemaDocument, {
    count: "4",
    mode: "safe",
    tags: ["alpha", "beta"],
    owner: { email: "owner@example.test", ignored: "remove" },
    extra: "remove",
  });
  assert.equal(valid.valid, true);
  const validObject = valid.value as JsonObject;
  assert.equal(validObject.enabled, true);
  assert.equal(validObject.count, 4);
  assert.equal(validObject.mode, "safe");
  assert.deepEqual(validObject.tags, ["alpha", "beta"]);
  assert.equal((validObject.owner as JsonObject).email, "owner@example.test");
  assert.equal((validObject.owner as JsonObject).ignored, undefined);
  assert.equal(validObject.extra, undefined);
  assert.ok(valid.defaultsApplied.includes("$.enabled"));
  assert.ok(valid.propertiesRemoved.includes("$.extra"));
  assert.ok(valid.propertiesRemoved.includes("$.owner.ignored"));
  assert.deepEqual(schema.require(schemaDocument, valid.value, "tool arguments"), valid.value);
  const invalid = schema.validate(schemaDocument, {
    enabled: "not-boolean",
    count: 14,
    mode: "unknown",
    tags: ["x", "x", "valid", "overflow"],
    owner: { email: "not-an-email" },
  });
  assert.equal(invalid.valid, false);
  assert.ok(invalid.issues.some((issue) => issue.keyword === "type"));
  assert.ok(invalid.issues.some((issue) => issue.keyword === "maximum"));
  assert.ok(invalid.issues.some((issue) => issue.keyword === "enum"));
  assert.ok(invalid.issues.some((issue) => issue.keyword === "minLength"));
  assert.ok(invalid.issues.some((issue) => issue.keyword === "maxItems"));
  assert.ok(invalid.issues.some((issue) => issue.keyword === "uniqueItems"));
  assert.ok(invalid.issues.some((issue) => issue.keyword === "format"));
  const parser = new McpSseParser({
    now: fixedNow,
    maximumEventBytes: 4_096,
    maximumBufferBytes: 8_192,
    maximumDataLines: 8,
  });
  assert.deepEqual(parser.push("id: evt-17\r\n"), []);
  assert.deepEqual(parser.push("event: progress\nretry: 1250\n"), []);
  assert.deepEqual(parser.push("data: {\"progress\":3,\n"), []);
  const parsed = parser.push("data: \"total\":10}\n\n");
  assert.equal(parsed.length, 1);
  assert.equal(parsed[0]?.eventId, "evt-17");
  assert.equal(parsed[0]?.event, "progress");
  assert.equal(parsed[0]?.retryMs, 1_250);
  assert.equal(parsed[0]?.data, "{\"progress\":3,\n\"total\":10}");
  const parserSnapshot = parser.snapshot();
  assert.equal(parserSnapshot.lastEventId, "evt-17");
  assert.equal(parserSnapshot.retryMs, 1_250);
  const resumed = new McpSseParser({ now: fixedNow }, parserSnapshot);
  const inherited = resumed.push("data: {\"resumed\":true}\n\n");
  assert.equal(inherited[0]?.eventId, "evt-17");
  assert.equal(inherited[0]?.retryMs, 1_250);
  resumed.reset({
    preserveLastEventId: false,
    preserveRetry: false,
  });
  const resetEvent = resumed.push("data: reset\n\n");
  assert.equal(resetEvent[0]?.eventId, null);
  assert.equal(resetEvent[0]?.retryMs, null);
  const pagination = new McpPaginationRuntime({
    maximumPages: 4,
    maximumItems: 8,
    rejectDuplicateCursors: true,
  });
  const pages = await pagination.collect(
    "resources/list",
    async (cursor, page) => {
      if (page === 1) {
        assert.equal(cursor, null);
        return {
          items: [{ uri: "memo://one" }, { uri: "memo://two" }],
          nextCursor: "cursor-2",
          metadata: { page },
        };
      }
      assert.equal(cursor, "cursor-2");
      assert.equal(page, 2);
      return {
        items: [{ uri: "memo://two" }, { uri: "memo://three" }],
        nextCursor: null,
        metadata: { page },
      };
    },
    {
      itemKey: (item) => String((item as JsonObject).uri),
      metadata: { server_id: "utility-server" },
    },
  );
  assert.equal(pages.pages, 2);
  assert.equal(pages.items.length, 3);
  assert.deepEqual(pages.items.map((item) => item.uri), ["memo://one", "memo://two", "memo://three"]);
  assert.deepEqual(pages.cursors, ["cursor-2"]);
  assert.equal(pages.duplicates, 1);
  assert.equal(pages.truncated, false);
  assert.equal(pages.itemDigests.length, 3);
  assert.equal(pages.digest.length, 64);
  assert.equal(pages.metadata.server_id, "utility-server");
  const retry = new McpRetryRuntime({
    now: fixedNow,
    maximumHistory: 8,
  });
  let attemptCount = 0;
  const retryResult = await retry.execute({
    serverId: "utility-server",
    requestId: "utility-request",
    operation: "resources/read",
    idempotent: true,
    idempotencyKey: "utility-key",
    connectionEpoch: 2,
    policy: {
      maximumAttempts: 3,
      initialDelayMs: 0,
      maximumDelayMs: 0,
      multiplier: 2,
      jitterRatio: 0,
      retryStatusCodes: [503],
      retryJsonRpcCodes: [],
    },
    metadata: { execution_id: "E02" },
  }, async (attempt) => {
    attemptCount += 1;
    if (attempt === 1) {
      throw new McpRuntimeError({
        failureId: "retryable-utility-failure",
        category: "transport",
        code: "service_unavailable",
        message: "temporary service unavailable",
        serverId: "utility-server",
        operation: "resources/read",
        requestId: "utility-request",
        statusCode: 503,
        retryable: true,
        disposition: "retry_same_connection",
      });
    }
    return { uri: "memo://three", text: "restored" };
  });
  assert.equal(attemptCount, 2);
  assert.equal(retryResult.retries, 1);
  assert.equal(retryResult.totalDelayMs, 0);
  assert.equal(retryResult.value.uri, "memo://three");
  assert.equal(retryResult.attempts.length, 2);
  assert.equal(retryResult.attempts[0]?.outcome, "failure");
  assert.equal(retryResult.attempts[0]?.retryScheduled, true);
  assert.equal(retryResult.attempts[1]?.outcome, "success");
  assert.equal(retry.history("utility-server").length, 2);
  await assert.rejects(retry.execute({
    serverId: "utility-server",
    requestId: "unsafe-request",
    operation: "tools/call",
    idempotent: false,
    idempotencyKey: null,
    connectionEpoch: 2,
    policy: {
      maximumAttempts: 2,
      initialDelayMs: 0,
      maximumDelayMs: 0,
      multiplier: 2,
      jitterRatio: 0,
      retryStatusCodes: [503],
      retryJsonRpcCodes: [],
    },
    metadata: {},
  }, async () => ({ ok: true })), /idempotency key/i);
});

test("MCP elicitation resumes only an exact continuation with schema-valid content", () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const runtime = new McpElicitationRuntime({
    now,
    defaultTtlMs: 30_000,
    maximumTtlMs: 60_000,
    allowUrlMode: false,
  });
  const changes: string[] = [];
  const unsubscribe = runtime.onChange((record) => {
    changes.push(`${record.elicitationId}:${record.status}`);
  });
  const identity = {
    runId: "run-elicit",
    taskId: "task-elicit",
    sessionId: "session-elicit",
    sessionRevision: 9,
    workerRequestId: "worker-elicit",
    toolCallId: "call-elicit",
    serverId: "server-elicit",
    connectionId: "connection-elicit",
    connectionEpoch: 4,
    requestId: "request-elicit",
  };
  const request = {
    mode: "form" as const,
    message: "Confirm the deployment target",
    requestedSchema: {
      type: "object" as const,
      properties: {
        environment: {
          type: "string" as const,
          title: "Environment",
          description: "Deployment environment",
          default: null,
          minLength: 3,
          maxLength: 16,
          format: null,
          enum: ["staging", "production"],
          enumNames: ["Staging", "Production"],
          minimum: null,
          maximum: null,
        },
        replicas: {
          type: "integer" as const,
          title: "Replicas",
          description: "Number of replicas",
          default: 1,
          minLength: null,
          maxLength: null,
          format: null,
          enum: [],
          enumNames: [],
          minimum: 1,
          maximum: 10,
        },
        acknowledged: {
          type: "boolean" as const,
          title: "Acknowledged",
          description: "Risk acknowledged",
          default: false,
          minLength: null,
          maxLength: null,
          format: null,
          enum: [true],
          enumNames: ["Acknowledged"],
          minimum: null,
          maximum: null,
        },
      },
      required: ["environment", "replicas", "acknowledged"],
    },
    meta: { canonical_owner: "typescript" },
  };
  const pending = runtime.request(identity, request, {
    ttlMs: 45_000,
    metadata: { execution_id: "E02" },
  });
  assert.equal(pending.identity.runId, "run-elicit");
  assert.equal(pending.identity.taskId, "task-elicit");
  assert.equal(pending.identity.sessionId, "session-elicit");
  assert.equal(pending.identity.sessionRevision, 9);
  assert.equal(pending.identity.workerRequestId, "worker-elicit");
  assert.equal(pending.identity.toolCallId, "call-elicit");
  assert.equal(pending.identity.serverId, "server-elicit");
  assert.equal(pending.identity.connectionId, "connection-elicit");
  assert.equal(pending.identity.connectionEpoch, 4);
  assert.equal(pending.identity.requestId, "request-elicit");
  assert.equal(pending.status, "pending");
  assert.equal(pending.response, null);
  assert.equal(pending.responseDigest, null);
  assert.equal(pending.rejectionCode, null);
  assert.equal(pending.resumedAt, null);
  assert.equal(pending.metadata.execution_id, "E02");
  assert.match(pending.elicitationId, /^mcp-elicitation-/);
  assert.match(pending.continuationId, /^mcp-elicitation-continuation-/);
  assert.equal(pending.requestDigest.length, 64);
  assert.equal(Date.parse(pending.expiresAt) - Date.parse(pending.createdAt), 45_000);
  assert.equal(runtime.pending().length, 1);
  assert.equal(runtime.pending("session-elicit").length, 1);
  assert.equal(runtime.pending("other-session").length, 0);
  assert.equal(runtime.get(pending.elicitationId)?.continuationId, pending.continuationId);
  assert.equal(changes.length, 1);
  assert.match(changes[0]!, /:pending$/);
  const accepted = runtime.resume({
    elicitationId: pending.elicitationId,
    continuationId: pending.continuationId,
    runId: identity.runId,
    taskId: identity.taskId,
    sessionId: identity.sessionId,
    sessionRevision: identity.sessionRevision,
    workerRequestId: identity.workerRequestId,
    toolCallId: identity.toolCallId,
    serverId: identity.serverId,
    connectionId: identity.connectionId,
    connectionEpoch: identity.connectionEpoch,
    requestId: identity.requestId,
    result: {
      action: "accept",
      content: {
        environment: "staging",
        replicas: 3,
        acknowledged: true,
      },
      meta: { responder: "operator-17" },
    },
  });
  assert.equal(accepted.status, "accepted");
  assert.equal(accepted.rejectionCode, null);
  assert.ok(accepted.resumedAt);
  assert.equal(accepted.response?.action, "accept");
  assert.equal(accepted.response?.content?.environment, "staging");
  assert.equal(accepted.response?.content?.replicas, 3);
  assert.equal(accepted.response?.content?.acknowledged, true);
  assert.equal(accepted.response?.meta?.responder, "operator-17");
  assert.equal(accepted.responseDigest?.length, 64);
  assert.equal(runtime.pending().length, 0);
  assert.equal(changes.length, 2);
  assert.match(changes[1]!, /:accepted$/);
  const exactReplay = runtime.resume({
    elicitationId: pending.elicitationId,
    continuationId: pending.continuationId,
    runId: identity.runId,
    taskId: identity.taskId,
    sessionId: identity.sessionId,
    sessionRevision: identity.sessionRevision,
    workerRequestId: identity.workerRequestId,
    toolCallId: identity.toolCallId,
    serverId: identity.serverId,
    connectionId: identity.connectionId,
    connectionEpoch: identity.connectionEpoch,
    requestId: identity.requestId,
    result: accepted.response!,
  });
  assert.equal(exactReplay.responseDigest, accepted.responseDigest);
  assert.equal(exactReplay.resumedAt, accepted.resumedAt);
  assert.equal(changes.length, 2);
  const snapshot = runtime.snapshot();
  assert.equal(snapshot.version, "zyra.mcp-elicitation-runtime/v1");
  assert.equal(snapshot.records.length, 1);
  assert.equal(snapshot.records[0]?.status, "accepted");
  assert.equal(snapshot.digest.length, 64);
  const restored = new McpElicitationRuntime({
    now,
    snapshot,
  });
  assert.equal(restored.get(pending.elicitationId)?.status, "accepted");
  assert.equal(restored.pending().length, 0);
  unsubscribe();
  const urlRuntime = new McpElicitationRuntime({
    now,
    allowUrlMode: false,
  });
  assert.throws(() => urlRuntime.request(identity, {
    mode: "url",
    message: "Open identity provider",
    url: "https://identity.example.test/continue",
    elicitationId: "remote-elicit",
    meta: {},
  }), /URL-mode|disabled/i);
});

test("MCP sampling applies model, context, token, cost, and exact replay custody", async () => {
  let tick = 0;
  const now = () => new Date(Date.parse(instant) + tick++ * 1_000);
  const providerRequests: JsonObject[] = [];
  const sampling = new McpSamplingRuntime({
    now,
    globalTokenBudget: 10_000,
    globalCostBudgetMicros: 50_000,
    provider: async (request) => {
      providerRequests.push({
        requestId: request.requestId,
        serverId: request.serverId,
        modelHints: request.modelHints,
        messages: request.messages as unknown as JsonObject[],
        systemPrompt: request.systemPrompt,
        maxTokens: request.maxTokens,
        temperature: request.temperature,
        stopSequences: request.stopSequences,
        metadata: request.metadata,
      } as unknown as JsonObject);
      return {
        role: "assistant",
        content: {
          type: "text",
          text: "The canonical revision is 21.",
          annotations: null,
          meta: { verified: true },
        },
        model: "zyra-test-model",
        stopReason: "end_turn",
        inputTokens: 120,
        outputTokens: 14,
        costMicros: 420,
        metadata: { provider_request_id: "provider-sample-1" },
      };
    },
  });
  const context = {
    serverId: "sampling-server",
    connectionId: "sampling-connection",
    connectionEpoch: 5,
    requestId: "sampling-request",
    sessionId: "sampling-session",
    taskId: "sampling-task",
    workspaceRoot: "G:/workspace",
    policyDigest: "sha256:sampling-policy",
    allowedModels: ["zyra-test-model", "zyra-backup-model"],
    maximumTokens: 512,
    maximumContextTokens: 2_048,
    allowTools: false,
    allowServerContext: false,
    metadata: { execution_id: "E02" },
  };
  const request = {
    messages: [{
      role: "user" as const,
      content: {
        type: "text" as const,
        text: "Explain revision 21.",
        annotations: null,
        meta: {},
      },
    }],
    modelPreferences: {
      hints: [{ name: "zyra-test-model" }],
      costPriority: 0.3,
      speedPriority: 0.5,
      intelligencePriority: 0.8,
    },
    systemPrompt: "Answer from the provided request only.",
    includeContext: "none" as const,
    temperature: 0.1,
    maxTokens: 128,
    stopSequences: ["END"],
    metadata: { source: "sampling-behavior" },
  };
  const result = await sampling.sample(context, request);
  assert.equal(result.role, "assistant");
  assert.equal(result.content.type, "text");
  assert.equal(result.content.text, "The canonical revision is 21.");
  assert.equal(result.content.meta?.verified, true);
  assert.equal(result.model, "zyra-test-model");
  assert.equal(result.stopReason, "end_turn");
  assert.match(String(result.meta?.sampling_id), /^mcp-sampling-/);
  assert.equal(result.meta?.input_tokens, 120);
  assert.equal(result.meta?.output_tokens, 14);
  assert.equal(result.meta?.cost_micros, 420);
  assert.equal(providerRequests.length, 1);
  assert.equal(providerRequests[0]?.requestId, "sampling-request");
  assert.equal(providerRequests[0]?.serverId, "sampling-server");
  assert.deepEqual(providerRequests[0]?.modelHints, ["zyra-test-model"]);
  assert.equal(providerRequests[0]?.systemPrompt, "Answer from the provided request only.");
  assert.equal(providerRequests[0]?.maxTokens, 128);
  assert.equal(providerRequests[0]?.temperature, 0.1);
  assert.deepEqual(providerRequests[0]?.stopSequences, ["END"]);
  const records = sampling.snapshot().records;
  assert.equal(records.length, 1);
  assert.equal(records[0]?.serverId, "sampling-server");
  assert.equal(records[0]?.connectionId, "sampling-connection");
  assert.equal(records[0]?.connectionEpoch, 5);
  assert.equal(records[0]?.requestId, "sampling-request");
  assert.equal(records[0]?.sessionId, "sampling-session");
  assert.equal(records[0]?.taskId, "sampling-task");
  assert.equal(records[0]?.status, "committed");
  assert.equal(records[0]?.selectedModel, "zyra-test-model");
  assert.equal(records[0]?.inputTokens, 120);
  assert.equal(records[0]?.outputTokens, 14);
  assert.equal(records[0]?.costMicros, 420);
  assert.equal(records[0]?.requestDigest.length, 64);
  assert.equal(records[0]?.resultDigest?.length, 64);
  assert.ok(records[0]?.completedAt);
  const exactReplay = await sampling.sample(context, request);
  assert.deepEqual(exactReplay, result);
  assert.equal(providerRequests.length, 1);
  assert.equal(sampling.get(records[0]!.samplingId)?.status, "committed");
  const snapshot = sampling.snapshot();
  assert.equal(snapshot.version, "zyra.mcp-sampling-runtime/v1");
  assert.equal(snapshot.tokenUsageByServer["sampling-server"], 134);
  assert.equal(snapshot.costMicrosByServer["sampling-server"], 420);
  assert.equal(snapshot.digest.length, 64);
  const restored = new McpSamplingRuntime({
    now,
    globalTokenBudget: 10_000,
    globalCostBudgetMicros: 50_000,
    snapshot,
    provider: async () => {
      throw new Error("restored committed sampling must not call provider");
    },
  });
  assert.deepEqual(await restored.sample(context, request), result);
  assert.equal(restored.get(records[0]!.samplingId)?.resultDigest, records[0]?.resultDigest);
  await assert.rejects(sampling.sample({
    ...context,
    requestId: "sampling-disallowed-model",
    allowedModels: ["different-model"],
  }, request), /model|allowed/i);
  await assert.rejects(sampling.sample({
    ...context,
    requestId: "sampling-token-limit",
    maximumTokens: 64,
  }, request), /token/i);
  await assert.rejects(sampling.sample({
    ...context,
    requestId: "sampling-context-denied",
    allowServerContext: false,
  }, {
    ...request,
    includeContext: "thisServer",
  }), /context/i);
});
