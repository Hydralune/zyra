import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer, type IncomingHttpHeaders, type Server } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { type TestContext } from "node:test";
import {
  InMemorySecretResolver,
  ProviderControlPlane,
  ProviderControlPlaneError,
  ProviderRouteHealthRuntime,
  SequenceIdFactory,
  encodeNormalizedProviderBody,
  fingerprintSecret,
  type ProviderDispatchRequest,
  type RouteRequest,
  type TransportProtocol,
} from "../src/index.ts";

interface CapturedRequest {
  readonly headers: IncomingHttpHeaders;
  readonly body: string;
}

interface CaptureServer {
  readonly server: Server;
  readonly baseUrl: string;
  readonly requests: CapturedRequest[];
  close(): Promise<void>;
}

async function captureServer(
  responder: (request: CapturedRequest, response: import("node:http").ServerResponse) => void,
): Promise<CaptureServer> {
  const requests: CapturedRequest[] = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      const captured = { headers: request.headers, body: Buffer.concat(chunks).toString("utf8") };
      requests.push(captured);
      responder(captured, response);
    });
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("capture server has no TCP address");
  return {
    server,
    baseUrl: `http://127.0.0.1:${address.port}`,
    requests,
    close: () => new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())),
  };
}

function controlPlane(
  t: TestContext,
): { readonly runtime: ProviderControlPlane; readonly secrets: InMemorySecretResolver; readonly directory: string } {
  const directory = mkdtempSync(join(tmpdir(), "zyra-provider-remediation-"));
  const secrets = new InMemorySecretResolver();
  const runtime = new ProviderControlPlane({
    databasePath: join(directory, "provider.sqlite3"),
    secrets,
    ids: new SequenceIdFactory("remediation"),
    route: { leaseMilliseconds: 60_000 },
  });
  t.after(() => {
    runtime.close();
    rmSync(directory, { recursive: true, force: true });
  });
  return { runtime, secrets, directory };
}

function install(
  runtime: ProviderControlPlane,
  secrets: InMemorySecretResolver,
  input: {
    readonly providerId: string;
    readonly modelId: string;
    readonly baseUrl: string;
    readonly protocol: TransportProtocol;
    readonly secret?: string;
    readonly supportsRefresh?: boolean;
    readonly priority?: number;
  },
): string {
  const integrationId = `${input.providerId}-integration`;
  const secretRef = `memory://${input.providerId}-primary`;
  const secret = input.secret ?? `${input.providerId}-secret`;
  secrets.put(secretRef, { value: secret });
  runtime.upsertIntegration({
    integrationId,
    displayName: integrationId,
    kind: "bearer",
    envNames: [],
    headerName: null,
    authorizationScheme: "Bearer",
    supportsRefresh: input.supportsRefresh ?? false,
    metadata: {},
  });
  runtime.upsertProvider({
    providerId: input.providerId,
    displayName: input.providerId,
    integrationId,
    status: "active",
    baseUrl: input.baseUrl,
    protocol: input.protocol,
    defaultHeaders: {},
    requestDefaults: {},
    allowedHosts: [new URL(input.baseUrl).hostname],
    tags: [],
    metadata: {},
  });
  runtime.upsertModel({
    providerId: input.providerId,
    modelId: input.modelId,
    displayName: input.modelId,
    family: "test",
    status: "active",
    enabled: true,
    releasedAt: 1,
    contextWindow: 32_000,
    maximumOutputTokens: 4_096,
    capabilities: {
      input: ["text"],
      output: ["text", "tool"],
      tools: true,
      streaming: true,
      reasoning: true,
      structuredOutput: true,
    },
    pricing: [],
    endpointPath: null,
    protocol: null,
    requestDefaults: {},
    tags: [],
    metadata: {},
  });
  return runtime.registerCredential({
    integrationId,
    providerId: input.providerId,
    accountId: "account-primary",
    secretRef,
    fingerprint: fingerprintSecret(secret),
    priority: input.priority ?? 10,
  }).credentialId;
}

function routeRequest(providerId: string, modelId: string, turnId = "turn-remediation"): RouteRequest {
  return {
    runId: "run-remediation",
    taskId: "task-remediation",
    nodeId: "node-remediation",
    sessionId: "session-remediation",
    turnId,
    purpose: "reason",
    preferredProviderId: providerId,
    preferredModelId: modelId,
    routeHint: `${providerId}/${modelId}`,
    constraints: {
      providerIds: [],
      modelIds: [],
      requiredInput: ["text"],
      requiredOutput: ["text"],
      requireTools: false,
      requireStreaming: true,
      minimumContextWindow: 1,
      maximumInputPricePerMillion: null,
      maximumOutputPricePerMillion: null,
      excludedCredentialIds: [],
      requiredScopes: [],
    },
    metadata: {},
  };
}

function dispatchRequest(routeId: string, turnId = "turn-remediation"): ProviderDispatchRequest {
  return {
    dispatchId: `dispatch-${turnId}`,
    routeId,
    runId: "run-remediation",
    taskId: "task-remediation",
    nodeId: "node-remediation",
    sessionId: "session-remediation",
    turnId,
    messages: [{ role: "user", content: "hello" }],
    tools: [],
    maximumOutputTokens: 256,
    temperature: null,
    stream: true,
    timeoutMilliseconds: 5_000,
    chunkTimeoutMilliseconds: 1_000,
    idempotencyKey: `idem-${turnId}`,
    extraBody: {},
    metadata: {},
  };
}

test("Responses and Anthropic codecs retain exact tool call/result identity", (t) => {
  const { runtime, secrets } = controlPlane(t);
  for (const [providerId, protocol] of [
    ["responses", "openai_responses"],
    ["anthropic", "anthropic_messages"],
  ] as const) {
    install(runtime, secrets, {
      providerId,
      modelId: `${providerId}-model`,
      baseUrl: "http://127.0.0.1:9",
      protocol,
    });
    const route = runtime.acquireRoute(routeRequest(providerId, `${providerId}-model`, `turn-${providerId}`));
    const request: ProviderDispatchRequest = {
      ...dispatchRequest(route.routeId, `turn-${providerId}`),
      messages: [
        { role: "user", content: "read it" },
        {
          role: "assistant",
          content: [{ type: "tool_call", id: "call-read-1", name: "read_file", arguments: { path: "README.md" } }],
        },
        { role: "tool", name: "read_file", toolCallId: "call-read-1", content: "contents" },
      ],
      tools: [{ name: "read_file", description: "Read file", inputSchema: { type: "object" } }],
    };
    const body = JSON.parse(encodeNormalizedProviderBody(route, request).json) as Record<string, unknown>;
    if (protocol === "openai_responses") {
      const input = body.input as Array<Record<string, unknown>>;
      assert.equal(input.find((item) => item.type === "function_call")?.call_id, "call-read-1");
      assert.equal(input.find((item) => item.type === "function_call_output")?.call_id, "call-read-1");
      assert.equal(input.some((item) => item.role === "user" && item.content === "contents"), false);
    } else {
      const messages = body.messages as Array<Record<string, unknown>>;
      const assistant = messages.find((message) => message.role === "assistant");
      const toolUse = (assistant?.content as Array<Record<string, unknown>>).find((part) => part.type === "tool_use");
      const toolResultMessage = messages.find((message) => message.role === "user" && Array.isArray(message.content));
      const toolResult = (toolResultMessage?.content as Array<Record<string, unknown>>).find((part) => part.type === "tool_result");
      assert.equal(toolUse?.id, "call-read-1");
      assert.equal(toolResult?.tool_use_id, "call-read-1");
    }
  }
});

test("non-streaming provider responses are normalized on the real transport path", async (t) => {
  const capture = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({
      object: "response",
      id: "resp-1",
      status: "completed",
      output: [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "normalized" }] }],
      usage: { input_tokens: 4, output_tokens: 2 },
    }));
  });
  t.after(() => capture.close());
  const { runtime, secrets } = controlPlane(t);
  install(runtime, secrets, {
    providerId: "nonstream",
    modelId: "responses-model",
    baseUrl: capture.baseUrl,
    protocol: "openai_responses",
  });
  const route = runtime.acquireRoute(routeRequest("nonstream", "responses-model"));
  const result = await runtime.dispatch(dispatchRequest(route.routeId));
  assert.equal(result.text, "normalized");
  assert.equal(result.stopReason, "completed");
  assert.equal(result.usage.output_tokens, 2);
  assert.deepEqual(result.frames.map((frame) => frame.kind), ["response_start", "text_delta", "usage", "response_end"]);
  assert.equal(capture.requests.length, 1);
});

test("401 rotates to a sibling credential and performs a second real request", async (t) => {
  const capture = await captureServer((request, response) => {
    if (request.headers.authorization === "Bearer expired-secret") {
      response.writeHead(401, { "content-type": "application/json" });
      response.end('{"error":{"message":"expired token","type":"authentication_error"}}');
      return;
    }
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end('data: {"choices":[{"delta":{"content":"rotated"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n');
  });
  t.after(() => capture.close());
  const { runtime, secrets } = controlPlane(t);
  const firstCredentialId = install(runtime, secrets, {
    providerId: "auth-rotation",
    modelId: "auth-model",
    baseUrl: capture.baseUrl,
    protocol: "openai_chat",
    secret: "expired-secret",
    priority: 10,
  });
  const secondRef = "memory://auth-rotation-secondary";
  secrets.put(secondRef, { value: "fresh-secret" });
  const second = runtime.registerCredential({
    integrationId: "auth-rotation-integration",
    providerId: "auth-rotation",
    accountId: "account-secondary",
    secretRef: secondRef,
    fingerprint: fingerprintSecret("fresh-secret"),
    priority: 0,
  });
  const route = runtime.acquireRoute(routeRequest("auth-rotation", "auth-model"));
  assert.equal(route.credentialId, firstCredentialId);
  const result = await runtime.dispatch(dispatchRequest(route.routeId));
  assert.equal(result.text, "rotated");
  assert.equal(result.attempts.length, 2);
  assert.equal(result.attempts[0]?.failureKind, "authentication_failed");
  assert.equal(runtime.routes.require(result.routeId).credentialId, second.credentialId);
  assert.deepEqual(capture.requests.map((request) => request.headers.authorization), [
    "Bearer expired-secret",
    "Bearer fresh-secret",
  ]);
});

test("dispatch idempotency caches one side effect and rejects digest drift", async (t) => {
  const capture = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end('data: {"choices":[{"delta":{"content":"once"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n');
  });
  t.after(() => capture.close());
  const { runtime, secrets } = controlPlane(t);
  install(runtime, secrets, {
    providerId: "idempotent",
    modelId: "idempotent-model",
    baseUrl: capture.baseUrl,
    protocol: "openai_chat",
  });
  const route = runtime.acquireRoute(routeRequest("idempotent", "idempotent-model"));
  const request = dispatchRequest(route.routeId);
  const first = await runtime.dispatch(request);
  const replay = await runtime.dispatch(request);
  assert.equal(first.text, "once");
  assert.deepEqual(replay, first);
  assert.equal(capture.requests.length, 1);
  await assert.rejects(
    () => runtime.dispatch({ ...request, messages: [{ role: "user", content: "changed" }] }),
    /idempotency|digest|bound|reused/i,
  );
  assert.equal(capture.requests.length, 1);
});

test("admission capacity is acquired before provider bytes are sent", async (t) => {
  const capture = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end("data: [DONE]\n\n");
  });
  t.after(() => capture.close());
  const { runtime, secrets } = controlPlane(t);
  install(runtime, secrets, {
    providerId: "capacity",
    modelId: "capacity-model",
    baseUrl: capture.baseUrl,
    protocol: "openai_chat",
  });
  const route = runtime.acquireRoute(routeRequest("capacity", "capacity-model"));
  const permits = Array.from({ length: 8 }, () => runtime.routeHealth.acquire(route));
  await assert.rejects(
    () => runtime.dispatch(dispatchRequest(route.routeId)),
    (error: unknown) => {
      assert.ok(error instanceof ProviderControlPlaneError);
      assert.equal(error.kind, "provider_unavailable");
      assert.equal(error.bytesSent, 0);
      return true;
    },
  );
  assert.equal(capture.requests.length, 0);
  assert.equal(runtime.store.listAttempts("dispatch-turn-remediation")[0]?.requestBytes, 0);
  for (const permit of permits) runtime.routeHealth.release(permit);
});

test("credential refresh is version fenced and only changes the next route", async (t) => {
  const { runtime, secrets } = controlPlane(t);
  const credentialId = install(runtime, secrets, {
    providerId: "refreshable",
    modelId: "refresh-model",
    baseUrl: "http://127.0.0.1:9",
    protocol: "openai_chat",
    secret: "refresh-v1",
    supportsRefresh: true,
  });
  const oldRoute = runtime.acquireRoute(routeRequest("refreshable", "refresh-model", "turn-old"));
  const newRef = "memory://refreshable-v2";
  secrets.put(newRef, { value: "refresh-v2" });
  const receipt = await runtime.credentialRefresh.execute({
    refreshId: "refresh-job-1",
    credentialId,
    expectedVersion: 1,
    reason: "manual",
    requestedBy: "test",
  }, {
    refresh: async () => ({
      secretRef: newRef,
      fingerprint: fingerprintSecret("refresh-v2"),
      expiresAt: Date.now() + 3_600_000,
      refreshAfter: Date.now() + 1_800_000,
    }),
  });
  assert.equal(receipt.state, "applied");
  assert.equal(receipt.resultingVersion, 2);
  assert.equal(runtime.routes.require(oldRoute.routeId).credentialVersion, 1);
  const nextRoute = runtime.acquireRoute(routeRequest("refreshable", "refresh-model", "turn-new"));
  assert.equal(nextRoute.credentialVersion, 2);
  assert.equal(nextRoute.credentialFingerprint, fingerprintSecret("refresh-v2"));
  await assert.rejects(
    () => runtime.credentialRefresh.execute({
      refreshId: "refresh-job-stale",
      credentialId,
      expectedVersion: 1,
      reason: "manual",
      requestedBy: "test",
    }, { refresh: async () => { throw new Error("must not execute"); } }),
    /version conflict/i,
  );
});

test("persisted cancellation blocks a dispatch before transport", async (t) => {
  const capture = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end("data: [DONE]\n\n");
  });
  t.after(() => capture.close());
  const { runtime, secrets } = controlPlane(t);
  install(runtime, secrets, {
    providerId: "cancelled",
    modelId: "cancelled-model",
    baseUrl: capture.baseUrl,
    protocol: "openai_chat",
  });
  const route = runtime.acquireRoute(routeRequest("cancelled", "cancelled-model"));
  const request = dispatchRequest(route.routeId);
  runtime.cancellations.request(request.dispatchId, "operator stop", "test");
  await assert.rejects(
    () => runtime.dispatch(request),
    (error: unknown) => {
      assert.ok(error instanceof ProviderControlPlaneError);
      assert.equal(error.kind, "request_aborted");
      return true;
    },
  );
  assert.equal(capture.requests.length, 0);
  assert.equal(runtime.cancellations.get(request.dispatchId)?.state, "acknowledged");
});

test("catalog discovery validates the whole document before one revision publish", (t) => {
  const { runtime, secrets } = controlPlane(t);
  install(runtime, secrets, {
    providerId: "catalog-source",
    modelId: "catalog-model",
    baseUrl: "http://127.0.0.1:9",
    protocol: "openai_chat",
  });
  const before = runtime.catalog.snapshot();
  assert.throws(() => runtime.catalogReconciler.reconcile({
    sourceId: "bad-discovery",
    sourceRevision: "1",
    providers: [...before.providers, ...before.providers],
    models: before.models,
    integrations: before.integrations,
    removeMissing: false,
    metadata: {},
  }), /duplicate provider identity/i);
  assert.equal(runtime.catalog.snapshot().revision, before.revision);
  assert.equal(runtime.catalogReconciler.runs("bad-discovery")[0]?.state, "failed");

  const provider = before.providers[0]!;
  const published = runtime.catalogReconciler.reconcile({
    sourceId: "valid-discovery",
    sourceRevision: "2",
    providers: [{ ...provider, tags: ["discovered"] }],
    models: before.models,
    integrations: before.integrations,
    removeMissing: false,
    metadata: { transport: "fixture-free" },
  }, { expectedRevision: before.revision });
  assert.equal(published.state, "published");
  assert.equal(published.catalogRevision, before.revision + 1);
  assert.deepEqual(runtime.catalog.provider("catalog-source").tags, ["discovered"]);
  assert.throws(() => runtime.catalogReconciler.reconcile({
    sourceId: "stale-discovery",
    sourceRevision: "3",
    providers: runtime.catalog.providers(),
    models: runtime.catalog.models(),
    integrations: runtime.catalog.integrations(),
    removeMissing: false,
    metadata: {},
  }, { expectedRevision: before.revision }), /revision conflict/i);
});

test("route circuit transitions through a single fenced half-open probe", (t) => {
  const { runtime, secrets } = controlPlane(t);
  install(runtime, secrets, {
    providerId: "health",
    modelId: "health-model",
    baseUrl: "http://127.0.0.1:9",
    protocol: "openai_chat",
  });
  const route = runtime.acquireRoute(routeRequest("health", "health-model"));
  let now = 1_000;
  const health = new ProviderRouteHealthRuntime(runtime.store, {
    clock: { now: () => now },
    ids: new SequenceIdFactory("health"),
    failureThreshold: 1,
    cooldownMilliseconds: 10,
    permitMilliseconds: 100,
    maximumInFlightPerProvider: 2,
  });
  const first = health.acquire(route);
  health.recordFailure(first, {
    latencyMilliseconds: 4,
    httpStatus: 503,
    failureKind: "provider_unavailable",
    retryAfterMilliseconds: null,
  });
  health.release(first);
  assert.equal(health.snapshot("health", "health-model").circuit, "open");
  assert.throws(() => health.acquire(route), /circuit is open/i);
  now += 11;
  const probe = health.acquire(route);
  assert.equal(health.snapshot("health", "health-model").circuit, "half_open");
  assert.throws(() => health.acquire(route), /half-open probe is already leased/i);
  health.recordSuccess(probe, 3, 200);
  health.release(probe);
  assert.equal(health.snapshot("health", "health-model").circuit, "closed");
});
