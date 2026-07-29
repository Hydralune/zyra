import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  KIMI_K27_CODE_MODEL_ID,
  KIMI_PLATFORM_PROVIDER_ID,
  ProviderControlPlane,
  installKimiK27CodeProfile,
} from "../packages/runtime/provider-control-plane/src/index.ts";
import type {
  ProviderDispatchRequest,
  RouteRequest,
} from "../packages/runtime/provider-control-plane/src/contracts.ts";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const temporaryRoot = join(projectRoot, ".tmp");
mkdirSync(temporaryRoot, { recursive: true });
const stateRoot = mkdtempSync(join(temporaryRoot, "kimi-platform-smoke-"));
const databasePath = join(stateRoot, "provider.sqlite3");
const stamp = Date.now().toString(36);

const controlPlane = new ProviderControlPlane({
  databasePath,
  route: {
    leaseMilliseconds: 120_000,
    defaultRetryPolicy: {
      maximumAttempts: 2,
      baseDelayMilliseconds: 250,
      maximumDelayMilliseconds: 1_000,
      retryStatuses: [429, 500, 502, 503, 504],
      rotateCredentialOnAuthenticationFailure: false,
      rotateRouteOnProviderUnavailable: false,
    },
  },
});

try {
  const installed = installKimiK27CodeProfile(controlPlane);
  const routeRequest: RouteRequest = {
    runId: `kimi-platform-smoke-${stamp}`,
    taskId: "provider-connectivity",
    nodeId: "cloud-provider",
    sessionId: `kimi-platform-session-${stamp}`,
    turnId: "turn-1",
    purpose: "verify",
    preferredProviderId: KIMI_PLATFORM_PROVIDER_ID,
    preferredModelId: KIMI_K27_CODE_MODEL_ID,
    routeHint: `${KIMI_PLATFORM_PROVIDER_ID}/${KIMI_K27_CODE_MODEL_ID}`,
    constraints: {
      providerIds: [KIMI_PLATFORM_PROVIDER_ID],
      modelIds: [KIMI_K27_CODE_MODEL_ID],
      requiredInput: ["text"],
      requiredOutput: ["text"],
      requireTools: false,
      requireStreaming: true,
      minimumContextWindow: 1_000,
      maximumInputPricePerMillion: null,
      maximumOutputPricePerMillion: null,
      excludedCredentialIds: [],
      requiredScopes: ["chat.completions"],
    },
    metadata: {
      evidence_class: "real-provider-smoke",
      secret_material_present: false,
      expected_model_version: "Kimi K2.7 Code",
    },
  };
  const route = controlPlane.acquireRoute(routeRequest);
  const dispatchRequest: ProviderDispatchRequest = {
    dispatchId: `kimi-platform-dispatch-${stamp}`,
    routeId: route.routeId,
    runId: route.runId,
    taskId: route.taskId,
    nodeId: route.nodeId,
    sessionId: route.sessionId,
    turnId: route.turnId,
    messages: [{
      role: "user",
      content: "Reply with exactly ZYRA_KIMI_OK and nothing else.",
    }],
    tools: [],
    maximumOutputTokens: 64,
    temperature: null,
    stream: true,
    timeoutMilliseconds: 120_000,
    chunkTimeoutMilliseconds: 60_000,
    idempotencyKey: `kimi-platform-smoke-${stamp}`,
    extraBody: {
      thinking: { type: "enabled" },
    },
    metadata: {
      purpose: "kimi-platform-connectivity-smoke",
    },
  };
  const result = await controlPlane.dispatch(dispatchRequest);
  const normalizedText = result.text.trim();
  if (normalizedText !== "ZYRA_KIMI_OK") {
    throw new Error("Kimi Code smoke response did not match the deterministic marker");
  }
  const attempt = result.attempts.at(-1);
  const receipt = {
    schema: "zyra.provider-live-smoke/v1",
    status: "passed",
    live: true,
    simulated: false,
    provider_id: result.providerId,
    model_id: result.modelId,
    protocol: result.protocol,
    endpoint_host: new URL(route.baseUrl).hostname,
    endpoint_path: route.endpointPath,
    credential_ref: installed.credential.secretRef,
    credential_fingerprint: installed.credential.fingerprint,
    credential_material_persisted: false,
    thinking_enabled: true,
    route_id: route.routeId,
    dispatch_id: result.dispatchId,
    http_status: attempt?.httpStatus ?? null,
    attempt_count: result.attempts.length,
    output_sha256: createHash("sha256").update(normalizedText).digest("hex"),
    usage: result.usage,
    stop_reason: result.stopReason,
    state_root: stateRoot,
  };
  process.stdout.write(`${JSON.stringify(receipt, null, 2)}\n`);
} finally {
  controlPlane.close();
}
