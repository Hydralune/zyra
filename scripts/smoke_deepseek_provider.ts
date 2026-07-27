import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  DEEPSEEK_PROVIDER_ID,
  DEEPSEEK_V4_PRO_MODEL_ID,
  ProviderControlPlane,
  installDeepSeekV4ProProfile,
} from "../packages/runtime/provider-control-plane/src/index.ts";
import type {
  ProviderDispatchRequest,
  RouteRequest,
} from "../packages/runtime/provider-control-plane/src/contracts.ts";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const temporaryRoot = join(projectRoot, ".tmp");
mkdirSync(temporaryRoot, { recursive: true });
const stateRoot = mkdtempSync(join(temporaryRoot, "deepseek-smoke-"));
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
  const installed = installDeepSeekV4ProProfile(controlPlane);
  const routeRequest: RouteRequest = {
    runId: `deepseek-smoke-${stamp}`,
    taskId: "provider-connectivity",
    nodeId: "cloud-provider",
    sessionId: `deepseek-session-${stamp}`,
    turnId: "turn-1",
    purpose: "verify",
    preferredProviderId: DEEPSEEK_PROVIDER_ID,
    preferredModelId: DEEPSEEK_V4_PRO_MODEL_ID,
    routeHint: `${DEEPSEEK_PROVIDER_ID}/${DEEPSEEK_V4_PRO_MODEL_ID}`,
    constraints: {
      providerIds: [DEEPSEEK_PROVIDER_ID],
      modelIds: [DEEPSEEK_V4_PRO_MODEL_ID],
      requiredInput: ["text"],
      requiredOutput: ["text"],
      requireTools: false,
      requireStreaming: true,
      minimumContextWindow: 1_000,
      maximumInputPricePerMillion: 1,
      maximumOutputPricePerMillion: 1,
      excludedCredentialIds: [],
      requiredScopes: ["chat.completions"],
    },
    metadata: {
      evidence_class: "real-provider-smoke",
      secret_material_present: false,
    },
  };
  const route = controlPlane.acquireRoute(routeRequest);
  const dispatchRequest: ProviderDispatchRequest = {
    dispatchId: `deepseek-dispatch-${stamp}`,
    routeId: route.routeId,
    runId: route.runId,
    taskId: route.taskId,
    nodeId: route.nodeId,
    sessionId: route.sessionId,
    turnId: route.turnId,
    messages: [{
      role: "user",
      content: "Reply with exactly ZYRA_DEEPSEEK_OK and nothing else.",
    }],
    tools: [],
    maximumOutputTokens: 32,
    temperature: 0,
    stream: true,
    timeoutMilliseconds: 90_000,
    chunkTimeoutMilliseconds: 45_000,
    idempotencyKey: `deepseek-smoke-${stamp}`,
    extraBody: {
      thinking: { type: "disabled" },
    },
    metadata: {
      purpose: "low-cost-connectivity-smoke",
    },
  };
  const result = await controlPlane.dispatch(dispatchRequest);
  const normalizedText = result.text.trim();
  if (normalizedText !== "ZYRA_DEEPSEEK_OK") {
    throw new Error("DeepSeek smoke response did not match the deterministic marker");
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
