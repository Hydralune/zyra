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
  SequenceIdFactory,
  fingerprintSecret,
  type ProviderDispatchRequest,
  type RouteRequest,
  type TransportProtocol,
} from "../src/index.ts";
import { ProviderControlPlaneRpcServer, RPC_PROTOCOL } from "../src/stdio-server.ts";
import {
  DEEPSEEK_API_KEY_ENV,
  DEEPSEEK_CREDENTIAL_ID,
  DEEPSEEK_PROVIDER_ID,
  DEEPSEEK_V4_FLASH_MODEL_ID,
  deepSeekV4FlashProfile,
  installDeepSeekV4FlashProfile,
} from "../src/profiles/deepseek.ts";
import {
  KIMI_API_KEY_ENV,
  KIMI_PLATFORM_CREDENTIAL_ID,
  KIMI_PLATFORM_PROVIDER_ID,
  KIMI_K27_CODE_MODEL_ID,
  installKimiK27CodeProfile,
  kimiK27CodeProfile,
} from "../src/profiles/kimi-platform.ts";
import {
  GLM_52_MODEL_ID,
  ZAI_API_KEY_ENV,
  ZHIPU_CREDENTIAL_ID,
  ZHIPU_PROVIDER_ID,
  glm52Profile,
  installGlm52Profile,
} from "../src/profiles/zhipu.ts";

interface CapturedRequest {
  readonly url: string;
  readonly method: string;
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
      const captured: CapturedRequest = {
        url: request.url ?? "",
        method: request.method ?? "",
        headers: request.headers,
        body: Buffer.concat(chunks).toString("utf8"),
      };
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

function makeControlPlane(
  t: TestContext,
  options: { readonly clock?: { now(): number }; readonly routeLeaseMilliseconds?: number } = {},
): { controlPlane: ProviderControlPlane; secrets: InMemorySecretResolver } {
  const directory = mkdtempSync(join(tmpdir(), "zyra-provider-control-"));
  const secrets = new InMemorySecretResolver();
  const controlPlane = new ProviderControlPlane({
    databasePath: join(directory, "provider.sqlite3"),
    secrets,
    ids: new SequenceIdFactory("test"),
    clock: options.clock,
    route: { leaseMilliseconds: options.routeLeaseMilliseconds ?? 60_000 },
  });
  t.after(() => {
    controlPlane.close();
    rmSync(directory, { recursive: true, force: true });
  });
  return { controlPlane, secrets };
}

test("DeepSeek V4 Flash profile binds an environment reference without persisting secret bytes", (t) => {
  const { controlPlane } = makeControlPlane(t);
  const secret = "deepseek-test-secret";
  const installed = installDeepSeekV4FlashProfile(controlPlane, {
    [DEEPSEEK_API_KEY_ENV]: secret,
  });
  const profile = deepSeekV4FlashProfile();

  assert.equal(profile.provider.baseUrl, "https://api.deepseek.com");
  assert.equal(profile.provider.protocol, "openai_chat");
  assert.deepEqual(profile.provider.allowedHosts, ["api.deepseek.com"]);
  assert.equal(profile.provider.metadata.routing_priority, 300);
  assert.equal(profile.model.modelId, "deepseek-v4-flash");
  assert.equal(profile.model.displayName, "DeepSeek V4 Flash 0731");
  assert.equal(profile.model.releasedAt, Date.UTC(2026, 6, 31));
  assert.equal(profile.model.metadata.model_version, "DeepSeek-V4-Flash-0731");
  assert.deepEqual(profile.model.pricing, [{
    inputPerMillion: 0.14,
    outputPerMillion: 0.28,
    cachedInputPerMillion: 0.0028,
    currency: "USD",
  }]);
  assert.equal(profile.model.endpointPath, "/chat/completions");
  assert.equal(profile.provider.requestDefaults.thinking, undefined);
  assert.deepEqual(profile.model.requestDefaults.thinking, { type: "disabled" });
  assert.equal(installed.credential.credentialId, DEEPSEEK_CREDENTIAL_ID);
  assert.equal(installed.credential.secretRef, `env://${DEEPSEEK_API_KEY_ENV}`);
  assert.notEqual(installed.credential.fingerprint, secret);
  assert.equal(JSON.stringify(controlPlane.catalog.snapshot()).includes(secret), false);
  assert.equal(JSON.stringify(controlPlane.credentials.list()).includes(secret), false);
});

test("DeepSeek V4 Flash profile fails closed when its environment secret is absent", (t) => {
  const { controlPlane } = makeControlPlane(t);
  assert.throws(
    () => installDeepSeekV4FlashProfile(controlPlane, {}),
    new RegExp(`${DEEPSEEK_API_KEY_ENV} is required`),
  );
  assert.deepEqual(controlPlane.catalog.providers(), []);
  assert.deepEqual(controlPlane.credentials.list(), []);
});

test("Kimi Open Platform K2.7 Code profile keeps thinking enabled and persists only an environment reference", (t) => {
  const { controlPlane } = makeControlPlane(t);
  const secret = "kimi-platform-test-secret";
  const installed = installKimiK27CodeProfile(controlPlane, {
    [KIMI_API_KEY_ENV]: secret,
  });
  const profile = kimiK27CodeProfile();

  assert.equal(profile.provider.baseUrl, "https://api.moonshot.cn/v1");
  assert.equal(profile.provider.protocol, "openai_chat");
  assert.deepEqual(profile.provider.allowedHosts, ["api.moonshot.cn"]);
  assert.equal(profile.provider.metadata.routing_priority, 100);
  assert.equal(profile.model.modelId, "kimi-k2.7-code");
  assert.equal(profile.model.contextWindow, 262_144);
  assert.equal(profile.model.endpointPath, "/chat/completions");
  assert.deepEqual(profile.model.requestDefaults.thinking, { type: "enabled" });
  assert.equal(installed.credential.credentialId, KIMI_PLATFORM_CREDENTIAL_ID);
  assert.equal(installed.credential.secretRef, `env://${KIMI_API_KEY_ENV}`);
  assert.notEqual(installed.credential.fingerprint, secret);
  assert.equal(JSON.stringify(controlPlane.catalog.snapshot()).includes(secret), false);
  assert.equal(JSON.stringify(controlPlane.credentials.list()).includes(secret), false);
});

test("Kimi Open Platform K2.7 Code profile fails closed when its environment secret is absent", (t) => {
  const { controlPlane } = makeControlPlane(t);
  assert.throws(
    () => installKimiK27CodeProfile(controlPlane, {}),
    new RegExp(`${KIMI_API_KEY_ENV} is required`),
  );
  assert.deepEqual(controlPlane.catalog.providers(), []);
  assert.deepEqual(controlPlane.credentials.list(), []);
});

test("Zhipu AI GLM-5.2 profile enables reasoning and persists only an environment reference", (t) => {
  const { controlPlane } = makeControlPlane(t);
  const secret = "zhipu-test-secret";
  const installed = installGlm52Profile(controlPlane, {
    [ZAI_API_KEY_ENV]: secret,
  });
  const profile = glm52Profile();

  assert.equal(profile.provider.baseUrl, "https://open.bigmodel.cn/api/paas/v4");
  assert.equal(profile.provider.protocol, "openai_chat");
  assert.deepEqual(profile.provider.allowedHosts, ["open.bigmodel.cn"]);
  assert.equal(profile.provider.metadata.routing_priority, 200);
  assert.equal(profile.model.modelId, GLM_52_MODEL_ID);
  assert.equal(profile.model.contextWindow, 1_000_000);
  assert.equal(profile.model.maximumOutputTokens, 131_072);
  assert.equal(profile.model.endpointPath, "/chat/completions");
  assert.deepEqual(profile.model.requestDefaults.thinking, { type: "enabled" });
  assert.equal(profile.model.requestDefaults.reasoning_effort, "max");
  assert.equal(installed.credential.credentialId, ZHIPU_CREDENTIAL_ID);
  assert.equal(installed.credential.secretRef, `env://${ZAI_API_KEY_ENV}`);
  assert.notEqual(installed.credential.fingerprint, secret);
  assert.equal(JSON.stringify(controlPlane.catalog.snapshot()).includes(secret), false);
  assert.equal(JSON.stringify(controlPlane.credentials.list()).includes(secret), false);
});

test("Zhipu AI GLM-5.2 profile fails closed when its environment secret is absent", (t) => {
  const { controlPlane } = makeControlPlane(t);
  assert.throws(
    () => installGlm52Profile(controlPlane, {}),
    new RegExp(`${ZAI_API_KEY_ENV} is required`),
  );
  assert.deepEqual(controlPlane.catalog.providers(), []);
  assert.deepEqual(controlPlane.credentials.list(), []);
});

test("default provider routing prefers DeepSeek, then GLM, with Kimi last", (t) => {
  const { controlPlane } = makeControlPlane(t);
  installGlm52Profile(controlPlane, { [ZAI_API_KEY_ENV]: "zhipu-secret" });
  installKimiK27CodeProfile(controlPlane, { [KIMI_API_KEY_ENV]: "kimi-secret" });
  installDeepSeekV4FlashProfile(controlPlane, {
    [DEEPSEEK_API_KEY_ENV]: "deepseek-secret",
  });

  const unconstrained: RouteRequest = {
    ...routeRequest(ZHIPU_PROVIDER_ID, GLM_52_MODEL_ID),
    preferredProviderId: null,
    preferredModelId: null,
    routeHint: null,
  };
  const first = controlPlane.acquireRoute(unconstrained);
  assert.equal(first.providerId, DEEPSEEK_PROVIDER_ID);
  assert.equal(first.modelId, DEEPSEEK_V4_FLASH_MODEL_ID);

  const withoutDeepSeek: RouteRequest = {
    ...unconstrained,
    turnId: "turn-without-deepseek",
    constraints: {
      ...unconstrained.constraints,
      providerIds: [KIMI_PLATFORM_PROVIDER_ID, ZHIPU_PROVIDER_ID],
      modelIds: [KIMI_K27_CODE_MODEL_ID, GLM_52_MODEL_ID],
    },
  };
  const second = controlPlane.acquireRoute(withoutDeepSeek);
  assert.equal(second.providerId, ZHIPU_PROVIDER_ID);
  assert.equal(second.modelId, GLM_52_MODEL_ID);
});

test("RPC server fails closed when the provider control-plane owner is disabled", async (t) => {
  const { controlPlane } = makeControlPlane(t);
  const server = new ProviderControlPlaneRpcServer(controlPlane);
  const previous = process.env.ZYRA_PROVIDER_CONTROL_PLANE_DISABLED;
  const restore = () => {
    if (previous === undefined) delete process.env.ZYRA_PROVIDER_CONTROL_PLANE_DISABLED;
    else process.env.ZYRA_PROVIDER_CONTROL_PLANE_DISABLED = previous;
  };
  process.env.ZYRA_PROVIDER_CONTROL_PLANE_DISABLED = "true";

  const response = await server.handle({
    protocol: RPC_PROTOCOL,
    requestId: "provider-owner-disconnect",
    operation: "health",
    payload: {},
  });
  restore();

  assert.equal(response.ok, false);
  assert.equal(response.error?.code, "provider_control_plane_disabled");
});

test("RPC installs configured live profiles in the fixed preference order without secret bytes", async (t) => {
  const { controlPlane } = makeControlPlane(t);
  const server = new ProviderControlPlaneRpcServer(controlPlane);
  const names = [DEEPSEEK_API_KEY_ENV, ZAI_API_KEY_ENV, KIMI_API_KEY_ENV] as const;
  const previous = new Map(names.map((name) => [name, process.env[name]]));
  const restore = () => {
    for (const name of names) {
      const value = previous.get(name);
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  };
  process.env[ZAI_API_KEY_ENV] = "configured-glm-secret";
  process.env[DEEPSEEK_API_KEY_ENV] = "configured-deepseek-secret";
  process.env[KIMI_API_KEY_ENV] = "configured-kimi-secret";

  const response = await server.handle({
    protocol: RPC_PROTOCOL,
    requestId: "install-configured-profiles",
    operation: "profiles.install_configured",
    payload: {},
  });
  restore();

  assert.equal(response.ok, true, JSON.stringify(response.error));
  const result = response.result as {
    installed: Array<Record<string, unknown>>;
    preferenceOrder: string[];
    secretBytesIncluded: boolean;
  };
  assert.deepEqual(
    result.installed.map((item) => `${item.providerId}/${item.modelId}`),
    [
      `${DEEPSEEK_PROVIDER_ID}/${DEEPSEEK_V4_FLASH_MODEL_ID}`,
      `${ZHIPU_PROVIDER_ID}/${GLM_52_MODEL_ID}`,
      `${KIMI_PLATFORM_PROVIDER_ID}/${KIMI_K27_CODE_MODEL_ID}`,
    ],
  );
  assert.deepEqual(result.preferenceOrder, [
    `${DEEPSEEK_PROVIDER_ID}/${DEEPSEEK_V4_FLASH_MODEL_ID}`,
    `${ZHIPU_PROVIDER_ID}/${GLM_52_MODEL_ID}`,
    `${KIMI_PLATFORM_PROVIDER_ID}/${KIMI_K27_CODE_MODEL_ID}`,
  ]);
  assert.equal(result.secretBytesIncluded, false);
  const serialized = JSON.stringify(response);
  assert.equal(serialized.includes("configured-glm-secret"), false);
  assert.equal(serialized.includes("configured-deepseek-secret"), false);
  assert.equal(serialized.includes("configured-kimi-secret"), false);
});

function installProvider(
  controlPlane: ProviderControlPlane,
  secrets: InMemorySecretResolver,
  input: {
    readonly providerId: string;
    readonly modelId: string;
    readonly baseUrl: string;
    readonly protocol: TransportProtocol;
    readonly endpointPath?: string;
    readonly secret?: string;
    readonly releasedAt?: number;
  },
): string {
  const integrationId = `${input.providerId}-integration`;
  const secretRef = `memory://${input.providerId}`;
  const secret = input.secret ?? `${input.providerId}-secret`;
  secrets.put(secretRef, { value: secret });
  controlPlane.upsertIntegration({
    integrationId,
    displayName: integrationId,
    kind: "bearer",
    envNames: [],
    headerName: null,
    authorizationScheme: "Bearer",
    supportsRefresh: false,
    metadata: {},
  });
  controlPlane.upsertProvider({
    providerId: input.providerId,
    displayName: input.providerId,
    integrationId,
    status: "active",
    baseUrl: input.baseUrl,
    protocol: input.protocol,
    defaultHeaders: { "x-zyra-provider": input.providerId },
    requestDefaults: {},
    allowedHosts: [new URL(input.baseUrl).hostname],
    tags: ["test"],
    metadata: {},
  });
  controlPlane.upsertModel({
    providerId: input.providerId,
    modelId: input.modelId,
    displayName: input.modelId,
    family: "test",
    status: "active",
    enabled: true,
    releasedAt: input.releasedAt ?? 1,
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
    endpointPath: input.endpointPath ?? null,
    protocol: null,
    requestDefaults: {},
    tags: [],
    metadata: {},
  });
  const credential = controlPlane.registerCredential({
    integrationId,
    providerId: input.providerId,
    accountId: `${input.providerId}-account`,
    secretRef,
    fingerprint: fingerprintSecret(secret),
  });
  return credential.credentialId;
}

function routeRequest(providerId: string, modelId: string, turnId = "turn-1"): RouteRequest {
  return {
    runId: "run-1",
    taskId: "task-1",
    nodeId: "node-1",
    sessionId: "session-1",
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
      minimumContextWindow: 1_000,
      maximumInputPricePerMillion: null,
      maximumOutputPricePerMillion: null,
      excludedCredentialIds: [],
      requiredScopes: [],
    },
    metadata: {},
  };
}

function dispatchRequest(routeId: string, turnId = "turn-1"): ProviderDispatchRequest {
  return {
    dispatchId: `dispatch-${turnId}`,
    routeId,
    runId: "run-1",
    taskId: "task-1",
    nodeId: "node-1",
    sessionId: "session-1",
    turnId,
    routeFallbackPolicy: "allow_route_change",
    messages: [{ role: "user", content: "Say hello" }],
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

test("catalog revisions pin immutable route fields and V1 stays read-only", async (t) => {
  const capture = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end("data: [DONE]\n\n");
  });
  t.after(() => capture.close());
  const { controlPlane, secrets } = makeControlPlane(t);
  installProvider(controlPlane, secrets, {
    providerId: "alpha",
    modelId: "alpha-model",
    baseUrl: capture.baseUrl,
    protocol: "openai_chat",
  });
  const route = controlPlane.acquireRoute(routeRequest("alpha", "alpha-model"));
  const pinnedRevision = route.catalogRevision;
  const provider = controlPlane.catalog.provider("alpha");
  controlPlane.upsertProvider({ ...provider, defaultHeaders: { "x-new-default": "next-turn" } });

  const restored = controlPlane.routes.require(route.routeId);
  assert.equal(restored.catalogRevision, pinnedRevision);
  assert.equal(restored.requestHeaders["x-new-default"], undefined);
  assert.equal(restored.checksum, route.checksum);
  const compat = controlPlane.catalog.compatibilityV1();
  assert.equal(compat.writable, false);
  assert.equal(compat.defaultModel, null);
  assert.equal(compat.providers[0]?.models["alpha-model"]?.context, 32_000);
});

test("expired pinned routes renew without changing provider or credential identity", (t) => {
  let now = 1_000_000;
  const { controlPlane, secrets } = makeControlPlane(t, {
    clock: { now: () => now },
    routeLeaseMilliseconds: 1_000,
  });
  installProvider(controlPlane, secrets, {
    providerId: "renewable",
    modelId: "renewable-model",
    baseUrl: "https://renewable.example.test",
    protocol: "openai_chat",
  });
  const original = controlPlane.acquireRoute(routeRequest("renewable", "renewable-model"));
  now += 1_001;

  assert.throws(
    () => controlPlane.routes.require(original.routeId),
    (error: unknown) => error instanceof ProviderControlPlaneError && error.kind === "route_expired",
  );
  assert.equal(controlPlane.routes.requirePersisted(original.routeId).checksum, original.checksum);

  const renewed = controlPlane.renewExpiredRoute(original.routeId);
  assert.notEqual(renewed.routeId, original.routeId);
  assert.equal(renewed.previousRouteId, original.routeId);
  assert.equal(renewed.providerId, original.providerId);
  assert.equal(renewed.modelId, original.modelId);
  assert.equal(renewed.credentialId, original.credentialId);
  assert.equal(renewed.credentialVersion, original.credentialVersion);
  assert.equal(renewed.credentialFingerprint, original.credentialFingerprint);
  assert.equal(renewed.expiresAt, now + 1_000);
  assert.equal(controlPlane.renewExpiredRoute(original.routeId).routeId, renewed.routeId);
  assert.equal(controlPlane.store.listRoutes("run-1", "task-1").length, 2);
});

test("credential rotation preserves an acquired route snapshot and changes only the next route", async (t) => {
  const capture = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n');
  });
  t.after(() => capture.close());
  const { controlPlane, secrets } = makeControlPlane(t);
  const credentialId = installProvider(controlPlane, secrets, {
    providerId: "rotating-provider",
    modelId: "rotating-model",
    baseUrl: capture.baseUrl,
    protocol: "openai_chat",
    secret: "credential-v1",
  });
  const acquiredBeforeRotation = controlPlane.acquireRoute(routeRequest("rotating-provider", "rotating-model"));
  const nextSecretRef = "memory://rotating-provider-v2";
  secrets.put(nextSecretRef, { value: "credential-v2" });
  const rotated = controlPlane.credentials.rotate(credentialId, acquiredBeforeRotation.credentialVersion, {
    secretRef: nextSecretRef,
    fingerprint: fingerprintSecret("credential-v2"),
  });

  await controlPlane.dispatch(dispatchRequest(acquiredBeforeRotation.routeId));
  const acquiredAfterRotation = controlPlane.acquireRoute(
    routeRequest("rotating-provider", "rotating-model", "turn-2"),
  );
  await controlPlane.dispatch(dispatchRequest(acquiredAfterRotation.routeId, "turn-2"));

  assert.equal(rotated.version, acquiredBeforeRotation.credentialVersion + 1);
  assert.equal(controlPlane.routes.require(acquiredBeforeRotation.routeId).credentialVersion, 1);
  assert.equal(acquiredAfterRotation.credentialVersion, 2);
  assert.equal(capture.requests.length, 2);
  assert.equal(capture.requests[0]?.headers.authorization, "Bearer credential-v1");
  assert.equal(capture.requests[1]?.headers.authorization, "Bearer credential-v2");
});

test("OpenAI-compatible dispatch captures real headers, body bytes, and SSE", async (t) => {
  const capture = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.write('data: {"id":"response-1","choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}\n\n');
    response.write('data: {"id":"response-1","choices":[{"delta":{},"finish_reason":null}]}\n\n');
    response.write('data: {"id":"response-1","choices":[{"delta":{"content":"hello "},"finish_reason":null}]}\n\n');
    response.write('data: {"choices":[{"delta":{"content":"world"},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n');
    response.end("data: [DONE]\n\n");
  });
  t.after(() => capture.close());
  const { controlPlane, secrets } = makeControlPlane(t);
  installProvider(controlPlane, secrets, {
    providerId: "openai-loopback",
    modelId: "chat-model",
    baseUrl: capture.baseUrl,
    protocol: "openai_chat",
    endpointPath: "/v1/chat/completions",
  });
  const route = controlPlane.acquireRoute(routeRequest("openai-loopback", "chat-model"));
  const result = await controlPlane.dispatch(dispatchRequest(route.routeId));

  assert.equal(result.text, "hello world");
  assert.equal(result.frames.filter((frame) => frame.kind === "response_start").length, 1);
  assert.equal(result.attempts.length, 1);
  assert.equal(result.attempts[0]?.outcome, "succeeded");
  assert.equal(capture.requests.length, 1);
  const request = capture.requests[0]!;
  assert.equal(request.url, "/v1/chat/completions");
  assert.equal(request.headers.authorization, "Bearer openai-loopback-secret");
  assert.equal(request.headers["x-zyra-provider"], "openai-loopback");
  const body = JSON.parse(request.body) as Record<string, unknown>;
  assert.equal(body.model, "chat-model");
  assert.equal(body.stream, true);
  assert.equal(body.max_tokens, 256);
  assert.equal("max_completion_tokens" in body, false);
  assert.ok(Buffer.byteLength(request.body) > 0);
  assert.equal(result.attempts[0]?.requestBytes, Buffer.byteLength(request.body));
});

test("Anthropic-compatible dispatch uses Messages wire and parses deltas", async (t) => {
  const capture = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.write('event: message_start\ndata: {"type":"message_start","message":{"id":"m","usage":{"input_tokens":2}}}\n\n');
    response.write('event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"anthropic-ok"}}\n\n');
    response.write('event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n');
    response.end('event: message_stop\ndata: {"type":"message_stop"}\n\n');
  });
  t.after(() => capture.close());
  const { controlPlane, secrets } = makeControlPlane(t);
  installProvider(controlPlane, secrets, {
    providerId: "anthropic-loopback",
    modelId: "claude-test",
    baseUrl: capture.baseUrl,
    protocol: "anthropic_messages",
    endpointPath: "/v1/messages",
  });
  const route = controlPlane.acquireRoute(routeRequest("anthropic-loopback", "claude-test"));
  const result = await controlPlane.dispatch(dispatchRequest(route.routeId));
  assert.equal(result.text, "anthropic-ok");
  assert.equal(result.stopReason, "end_turn");
  assert.equal(capture.requests[0]?.headers["anthropic-version"], "2023-06-01");
  const body = JSON.parse(capture.requests[0]!.body) as Record<string, unknown>;
  assert.equal(body.model, "claude-test");
  assert.equal(body.max_tokens, 256);
});

test("revoked pinned credential fails before transport with zero bytes", async (t) => {
  const capture = await captureServer((_request, response) => {
    response.writeHead(500);
    response.end("must not be reached");
  });
  t.after(() => capture.close());
  const { controlPlane, secrets } = makeControlPlane(t);
  const credentialId = installProvider(controlPlane, secrets, {
    providerId: "revoked-provider",
    modelId: "revoked-model",
    baseUrl: capture.baseUrl,
    protocol: "openai_chat",
  });
  const route = controlPlane.acquireRoute(routeRequest("revoked-provider", "revoked-model"));
  controlPlane.credentials.revoke(credentialId, route.credentialVersion);

  await assert.rejects(
    () => controlPlane.dispatch(dispatchRequest(route.routeId)),
    (error: unknown) => {
      assert.ok(error instanceof ProviderControlPlaneError);
      assert.equal(error.kind, "credential_revoked");
      assert.equal(error.bytesSent, 0);
      return true;
    },
  );
  assert.equal(capture.requests.length, 0);
  const attempts = controlPlane.store.listAttempts("dispatch-turn-1");
  assert.equal(attempts.length, 1);
  assert.equal(attempts[0]?.requestBytes, 0);
  assert.equal(attempts[0]?.responseBytes, 0);
});

test("provider unavailable creates a new route lease without backend concepts", async (t) => {
  const primary = await captureServer((_request, response) => {
    response.writeHead(503, { "content-type": "application/json", "retry-after": "0" });
    response.end('{"error":{"message":"temporarily unavailable"}}');
  });
  const fallback = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end('data: {"choices":[{"delta":{"content":"fallback-ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n');
  });
  t.after(async () => { await primary.close(); await fallback.close(); });
  const { controlPlane, secrets } = makeControlPlane(t);
  installProvider(controlPlane, secrets, {
    providerId: "primary",
    modelId: "primary-model",
    baseUrl: primary.baseUrl,
    protocol: "openai_chat",
    releasedAt: 20,
  });
  installProvider(controlPlane, secrets, {
    providerId: "fallback",
    modelId: "fallback-model",
    baseUrl: fallback.baseUrl,
    protocol: "openai_chat",
    releasedAt: 10,
  });
  const route = controlPlane.acquireRoute(routeRequest("primary", "primary-model"));
  const result = await controlPlane.dispatch(dispatchRequest(route.routeId));
  assert.equal(result.text, "fallback-ok");
  assert.equal(result.providerId, "fallback");
  assert.notEqual(result.routeId, route.routeId);
  assert.equal(result.attempts.length, 2);
  assert.equal(result.attempts[0]?.failureKind, "provider_unavailable");
  assert.equal(primary.requests.length, 1);
  assert.equal(fallback.requests.length, 1);
  const nextRoute = controlPlane.routes.require(result.routeId);
  assert.equal(nextRoute.previousRouteId, route.routeId);
  assert.equal("backendId" in nextRoute, false);
});

test("a pinned dispatch retries only its credential-bearing route", async (t) => {
  const primary = await captureServer((_request, response) => {
    response.writeHead(503, { "content-type": "application/json", "retry-after": "0" });
    response.end('{"error":{"message":"temporarily unavailable"}}');
  });
  const fallback = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end('data: {"choices":[{"delta":{"content":"must-not-run"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n');
  });
  t.after(async () => { await primary.close(); await fallback.close(); });
  const { controlPlane, secrets } = makeControlPlane(t);
  installProvider(controlPlane, secrets, {
    providerId: "pinned-primary",
    modelId: "pinned-model",
    baseUrl: primary.baseUrl,
    protocol: "openai_chat",
    releasedAt: 20,
  });
  installProvider(controlPlane, secrets, {
    providerId: "unrelayed-fallback",
    modelId: "unrelayed-model",
    baseUrl: fallback.baseUrl,
    protocol: "openai_chat",
    releasedAt: 10,
  });
  const route = controlPlane.acquireRoute(routeRequest("pinned-primary", "pinned-model"));
  const request = {
    ...dispatchRequest(route.routeId),
    routeFallbackPolicy: "pin_initial_route" as const,
  };

  await assert.rejects(
    () => controlPlane.dispatch(request),
    (error: unknown) => {
      assert.ok(error instanceof ProviderControlPlaneError);
      assert.equal(error.kind, "provider_unavailable");
      assert.equal(error.routeId, route.routeId);
      assert.equal(error.providerId, "pinned-primary");
      return true;
    },
  );
  assert.ok(primary.requests.length >= 1);
  assert.equal(fallback.requests.length, 0);
  const attempts = controlPlane.store.listAttempts(request.dispatchId);
  assert.ok(attempts.length >= 1);
  assert.equal(attempts.every((attempt) => attempt.routeId === route.routeId), true);
});

test("partial output followed by malformed stream is reconcile-only and never replayed", async (t) => {
  const primary = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.write('data: {"choices":[{"delta":{"content":"observed"},"finish_reason":null}]}\n\n');
    response.end("data: {malformed-json}\n\n");
  });
  const fallback = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end('data: {"choices":[{"delta":{"content":"must-not-run"},"finish_reason":"stop"}]}\n\n');
  });
  t.after(async () => { await primary.close(); await fallback.close(); });
  const { controlPlane, secrets } = makeControlPlane(t);
  installProvider(controlPlane, secrets, {
    providerId: "partial-primary",
    modelId: "partial-model",
    baseUrl: primary.baseUrl,
    protocol: "openai_chat",
    releasedAt: 20,
  });
  installProvider(controlPlane, secrets, {
    providerId: "partial-fallback",
    modelId: "fallback-model",
    baseUrl: fallback.baseUrl,
    protocol: "openai_chat",
    releasedAt: 10,
  });
  const route = controlPlane.acquireRoute(routeRequest("partial-primary", "partial-model"));
  await assert.rejects(
    () => controlPlane.dispatch(dispatchRequest(route.routeId)),
    (error: unknown) => {
      assert.ok(error instanceof ProviderControlPlaneError);
      assert.equal(error.outputObserved, true);
      assert.equal(error.recoveryIntent, "reconcile_partial_response");
      return true;
    },
  );
  assert.equal(primary.requests.length, 1);
  assert.equal(fallback.requests.length, 0);
  assert.equal(controlPlane.store.listAttempts("dispatch-turn-1").length, 1);
});

test("Anthropic thinking and fragmented tool JSON remain one normalized side-effect candidate", async (t) => {
  const capture = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.write('event: message_start\ndata: {"type":"message_start","message":{"id":"tool-message"}}\n\n');
    response.write('event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"inspect first"}}\n\n');
    response.write('event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"call-read","name":"read_file","input":{}}}\n\n');
    response.write('event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":"}}\n\n');
    response.write('event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"README.md\\"}"}}\n\n');
    response.write('event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":12}}\n\n');
    response.end('event: message_stop\ndata: {"type":"message_stop"}\n\n');
  });
  t.after(() => capture.close());
  const { controlPlane, secrets } = makeControlPlane(t);
  installProvider(controlPlane, secrets, {
    providerId: "anthropic-tools",
    modelId: "claude-tools",
    baseUrl: capture.baseUrl,
    protocol: "anthropic_messages",
    endpointPath: "/v1/messages",
  });
  const route = controlPlane.acquireRoute(routeRequest("anthropic-tools", "claude-tools"));
  const request = dispatchRequest(route.routeId);
  const result = await controlPlane.dispatch({
    ...request,
    tools: [{ name: "read_file", description: "Read a file", inputSchema: { type: "object" } }],
  });

  assert.equal(result.stopReason, "tool_use");
  assert.equal(result.frames.filter((frame) => frame.kind === "thinking_delta").length, 1);
  const toolFrames = result.frames.filter((frame) => frame.kind === "tool_call_delta");
  assert.equal(toolFrames.length, 3);
  assert.equal(toolFrames[0]?.toolCallId, "call-read");
  assert.equal(toolFrames[0]?.toolName, "read_file");
  assert.equal(toolFrames.slice(1).map((frame) => frame.jsonDelta).join(""), '{"path":"README.md"}');
  assert.equal(result.metadata.streamReplaySafe, false);
  assert.equal(result.attempts[0]?.metadata.streamToolCallCount, 1);
  assert.equal(capture.requests.length, 1);
});

test("incomplete tool JSON after observable call is reconcile-only and blocks model fallback", async (t) => {
  const primary = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.write('data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-partial","function":{"name":"write_file","arguments":"{\\"path\\":\\"a.txt\\""}}]},"finish_reason":"tool_calls"}]}\n\n');
    response.end("data: [DONE]\n\n");
  });
  const fallback = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end('data: {"choices":[{"delta":{"content":"must-not-replay"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n');
  });
  t.after(async () => { await primary.close(); await fallback.close(); });
  const { controlPlane, secrets } = makeControlPlane(t);
  installProvider(controlPlane, secrets, {
    providerId: "tool-primary",
    modelId: "tool-model",
    baseUrl: primary.baseUrl,
    protocol: "openai_chat",
    releasedAt: 20,
  });
  installProvider(controlPlane, secrets, {
    providerId: "tool-fallback",
    modelId: "fallback-model",
    baseUrl: fallback.baseUrl,
    protocol: "openai_chat",
    releasedAt: 10,
  });
  const route = controlPlane.acquireRoute(routeRequest("tool-primary", "tool-model"));
  await assert.rejects(
    () => controlPlane.dispatch({
      ...dispatchRequest(route.routeId),
      tools: [{ name: "write_file", description: "Write a file", inputSchema: { type: "object" } }],
    }),
    (error: unknown) => {
      assert.ok(error instanceof ProviderControlPlaneError);
      assert.equal(error.kind, "partial_response_observed");
      assert.equal(error.outputObserved, true);
      assert.equal(error.recoveryIntent, "reconcile_partial_response");
      return true;
    },
  );
  assert.equal(primary.requests.length, 1);
  assert.equal(fallback.requests.length, 0);
  const attempts = controlPlane.store.listAttempts("dispatch-turn-1");
  assert.equal(attempts.length, 1);
  assert.equal(attempts[0]?.metadata.streamReplaySafe, false);
  assert.equal(attempts[0]?.metadata.streamRecoveryAction, "reconcile_partial_response");
});

test("rate limit performs a second real request on a new provider route", async (t) => {
  const primary = await captureServer((_request, response) => {
    response.writeHead(429, { "content-type": "application/json", "retry-after": "0" });
    response.end('{"error":{"message":"rate limited","type":"rate_limit_error"}}');
  });
  const fallback = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end('data: {"choices":[{"delta":{"content":"rate-limit-recovered"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n');
  });
  t.after(async () => { await primary.close(); await fallback.close(); });
  const { controlPlane, secrets } = makeControlPlane(t);
  installProvider(controlPlane, secrets, {
    providerId: "rate-primary",
    modelId: "rate-model",
    baseUrl: primary.baseUrl,
    protocol: "openai_chat",
    releasedAt: 30,
  });
  installProvider(controlPlane, secrets, {
    providerId: "rate-fallback",
    modelId: "rate-fallback-model",
    baseUrl: fallback.baseUrl,
    protocol: "openai_chat",
    releasedAt: 20,
  });
  const original = controlPlane.acquireRoute(routeRequest("rate-primary", "rate-model"));
  const result = await controlPlane.dispatch(dispatchRequest(original.routeId));

  assert.equal(result.text, "rate-limit-recovered");
  assert.notEqual(result.routeId, original.routeId);
  assert.equal(result.providerId, "rate-fallback");
  assert.equal(result.attempts.length, 2);
  assert.equal(result.attempts[0]?.failureKind, "rate_limited");
  assert.equal(primary.requests.length, 1);
  assert.equal(fallback.requests.length, 1);
  assert.equal(controlPlane.routes.require(result.routeId).previousRouteId, original.routeId);
});

test("zero-output SSE stall changes provider route and performs one bounded recovery request", async (t) => {
  const stalled = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.flushHeaders();
    setTimeout(() => response.end(), 150);
  });
  const fallback = await captureServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end('data: {"choices":[{"delta":{"content":"stall-recovered"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n');
  });
  t.after(async () => { await stalled.close(); await fallback.close(); });
  const { controlPlane, secrets } = makeControlPlane(t);
  installProvider(controlPlane, secrets, {
    providerId: "stall-primary",
    modelId: "stall-model",
    baseUrl: stalled.baseUrl,
    protocol: "openai_chat",
    releasedAt: 30,
  });
  installProvider(controlPlane, secrets, {
    providerId: "stall-fallback",
    modelId: "stall-fallback-model",
    baseUrl: fallback.baseUrl,
    protocol: "openai_chat",
    releasedAt: 20,
  });
  const original = controlPlane.acquireRoute(routeRequest("stall-primary", "stall-model"));
  const result = await controlPlane.dispatch({
    ...dispatchRequest(original.routeId),
    timeoutMilliseconds: 500,
    chunkTimeoutMilliseconds: 25,
  });

  assert.equal(result.text, "stall-recovered");
  assert.notEqual(result.routeId, original.routeId);
  assert.equal(result.providerId, "stall-fallback");
  assert.equal(result.attempts.length, 2);
  assert.equal(result.attempts[0]?.failureKind, "stream_timeout");
  assert.equal(result.attempts[0]?.outputObserved, false);
  assert.equal(stalled.requests.length, 1);
  assert.equal(fallback.requests.length, 1);
});
