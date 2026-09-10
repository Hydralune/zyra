import { createHash } from "node:crypto";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  DEEPSEEK_PROVIDER_ID,
  DEEPSEEK_FLASH_MODEL_ID,
  GLM_52_MODEL_ID,
  KIMI_K27_CODE_MODEL_ID,
  KIMI_PLATFORM_PROVIDER_ID,
  ProviderControlPlane,
  ZHIPU_PROVIDER_ID,
  installDeepSeekFlashProfile,
  installGlm52Profile,
  installKimiK27CodeProfile,
} from "../packages/runtime/provider-control-plane/src/index.ts";
import type {
  CredentialRecord,
  ProviderDispatchResult,
  RouteRequest,
} from "../packages/runtime/provider-control-plane/src/contracts.ts";
import type {
  JsonRecord,
} from "../packages/runtime/provider-control-plane/src/canonical.ts";

interface FormalCase {
  readonly case_id: string;
  readonly domain: string;
  readonly repetition: number;
  readonly source_run_id: string;
  readonly owner_run_id: string;
  readonly task_id: string;
  readonly source_archive_digest: string;
  readonly source_outcome_digest: string;
}

interface ProviderEvidenceInput {
  readonly schema: "zyra.m3-current-provider-input/v1";
  readonly campaign_id: string;
  readonly implementation_commit: string;
  readonly cases: readonly FormalCase[];
}

interface ProviderSpec {
  readonly providerId: string;
  readonly modelId: string;
  readonly credential: CredentialRecord;
  readonly maximumOutputTokens: number;
  readonly temperature: number | null;
  readonly extraBody: JsonRecord;
  readonly forceToolChoice: boolean;
}

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const argumentsMap = parseArguments(process.argv.slice(2));
const inputPath = resolve(requiredArgument(argumentsMap, "input"));
const outputPath = resolve(requiredArgument(argumentsMap, "output"));
const input = validateInput(JSON.parse(readFileSync(inputPath, "utf8")));
const temporaryRoot = join(projectRoot, ".tmp");
mkdirSync(temporaryRoot, { recursive: true });
const stateRoot = mkdtempSync(join(temporaryRoot, "m3-provider-evidence-"));
const controlPlane = new ProviderControlPlane({
  databasePath: join(stateRoot, "provider.sqlite3"),
  route: {
    leaseMilliseconds: 180_000,
    defaultRetryPolicy: {
      maximumAttempts: 1,
      baseDelayMilliseconds: 500,
      maximumDelayMilliseconds: 2_000,
      retryStatuses: [429, 500, 502, 503, 504],
      rotateCredentialOnAuthenticationFailure: false,
      rotateRouteOnProviderUnavailable: false,
    },
  },
});

try {
  const deepseek = installDeepSeekFlashProfile(controlPlane);
  const kimi = installKimiK27CodeProfile(controlPlane);
  const glm = installGlm52Profile(controlPlane);
  const providers = new Map<string, ProviderSpec>([
    [ZHIPU_PROVIDER_ID, {
      providerId: ZHIPU_PROVIDER_ID,
      modelId: GLM_52_MODEL_ID,
      credential: glm.credential,
      maximumOutputTokens: 256,
      temperature: null,
      extraBody: {},
      forceToolChoice: true,
    }],
    [DEEPSEEK_PROVIDER_ID, {
      providerId: DEEPSEEK_PROVIDER_ID,
      modelId: DEEPSEEK_FLASH_MODEL_ID,
      credential: deepseek.credential,
      maximumOutputTokens: 96,
      temperature: null,
      extraBody: {},
      forceToolChoice: true,
    }],
    [KIMI_PLATFORM_PROVIDER_ID, {
      providerId: KIMI_PLATFORM_PROVIDER_ID,
      modelId: KIMI_K27_CODE_MODEL_ID,
      credential: kimi.credential,
      maximumOutputTokens: 192,
      temperature: null,
      extraBody: {},
      forceToolChoice: false,
    }],
  ]);
  const calls: JsonRecord[] = [];
  for (const formalCase of input.cases) {
    const secondary = formalCase.repetition % 2 === 0
      ? KIMI_PLATFORM_PROVIDER_ID
      : DEEPSEEK_PROVIDER_ID;
    for (const providerId of [ZHIPU_PROVIDER_ID, secondary]) {
      const provider = providers.get(providerId);
      if (provider === undefined) throw new Error(`provider is not installed: ${providerId}`);
      calls.push(await dispatchFormalBinding(input, formalCase, provider));
      process.stdout.write(
        `provider-evidence case=${formalCase.case_id} provider=${provider.providerId} status=passed\n`,
      );
    }
  }
  const providerIds = [...new Set(calls.map((item) => String(item.provider_id)))].sort();
  const modelIds = [...new Set(calls.map((item) => String(item.model_id)))].sort();
  const usageByProvider: Record<string, JsonRecord> = {};
  for (const providerId of providerIds) {
    const selected = calls.filter((item) => item.provider_id === providerId);
    usageByProvider[providerId] = {
      request_count: selected.length,
      prompt_tokens: sumUsage(selected, "prompt_tokens"),
      completion_tokens: sumUsage(selected, "completion_tokens"),
      total_tokens: sumUsage(selected, "total_tokens"),
    };
  }
  const output: Record<string, unknown> = {
    schema: "zyra.m3-current-provider-dispatch/v1",
    status: "passed",
    campaign_id: input.campaign_id,
    implementation_commit: input.implementation_commit,
    case_count: input.cases.length,
    provider_request_count: calls.length,
    current_provider_ids: providerIds,
    current_model_ids: modelIds,
    current_provider_count: providerIds.length,
    current_model_count: modelIds.length,
    live: true,
    simulated: false,
    external_model_request_made: true,
    authenticated_provider_runtime_invoked: true,
    same_run_as_formal_cases: true,
    credential_material_persisted: false,
    usage_by_provider: usageByProvider,
    calls,
    completed_at: new Date().toISOString(),
  };
  output.receipt_digest = digest(output);
  atomicJson(outputPath, output);
  process.stdout.write(
    `provider-evidence-complete cases=${input.cases.length} requests=${calls.length} providers=${providerIds.length}\n`,
  );
} finally {
  controlPlane.close();
}

async function dispatchFormalBinding(
  input: ProviderEvidenceInput,
  formalCase: FormalCase,
  provider: ProviderSpec,
): Promise<JsonRecord> {
  const bindingToken = createHash("sha256")
    .update([
      input.campaign_id,
      formalCase.case_id,
      formalCase.source_run_id,
      formalCase.source_archive_digest,
      formalCase.source_outcome_digest,
      provider.providerId,
      provider.modelId,
    ].join("\n"))
    .digest("hex")
    .slice(0, 24);
  const requestId = `m3-current:${formalCase.case_id}:${provider.providerId}`;
  const routeRequest: RouteRequest = {
    runId: formalCase.source_run_id,
    taskId: formalCase.task_id,
    nodeId: `cloud-${provider.providerId}`,
    sessionId: `m3-current-${formalCase.case_id.replaceAll(":", "-")}`,
    turnId: `provider-${provider.providerId}`,
    purpose: "verify",
    preferredProviderId: provider.providerId,
    preferredModelId: provider.modelId,
    routeHint: `${provider.providerId}/${provider.modelId}`,
    constraints: {
      providerIds: [provider.providerId],
      modelIds: [provider.modelId],
      requiredInput: ["text"],
      requiredOutput: ["tool"],
      requireTools: true,
      requireStreaming: true,
      minimumContextWindow: 1_000,
      maximumInputPricePerMillion: null,
      maximumOutputPricePerMillion: null,
      excludedCredentialIds: [],
      requiredScopes: ["chat.completions"],
    },
    metadata: {
      evidence_class: "m3-current-formal-case-provider",
      campaign_id: input.campaign_id,
      case_id: formalCase.case_id,
      source_archive_digest: formalCase.source_archive_digest,
      source_outcome_digest: formalCase.source_outcome_digest,
      secret_material_present: false,
    },
  };
  const route = controlPlane.acquireRoute(routeRequest);
  const result = await controlPlane.dispatch({
    dispatchId: `m3-${slug(formalCase.case_id)}-${provider.providerId}-${Date.now().toString(36)}`,
    routeId: route.routeId,
    runId: route.runId,
    taskId: route.taskId,
    nodeId: route.nodeId,
    sessionId: route.sessionId,
    turnId: route.turnId,
    messages: [{
      role: "user",
      content: [
        "This is a sealed compatibility check for a completed formal case.",
        "Call bind_formal_case exactly once and do not answer with text.",
        `binding_token=${bindingToken}`,
      ].join("\n"),
    }],
    tools: [{
      name: "bind_formal_case",
      description: "Bind this authenticated model response to the exact sealed formal case.",
      inputSchema: {
        type: "object",
        properties: {
          binding_token: {
            type: "string",
            description: "The exact binding_token supplied by the user.",
          },
        },
        required: ["binding_token"],
        additionalProperties: false,
      },
    }],
    maximumOutputTokens: provider.maximumOutputTokens,
    temperature: provider.temperature,
    stream: true,
    timeoutMilliseconds: 120_000,
    chunkTimeoutMilliseconds: 60_000,
    idempotencyKey: requestId,
    extraBody: {
      ...provider.extraBody,
      ...(provider.forceToolChoice
        ? {
            tool_choice: {
              type: "function",
              function: { name: "bind_formal_case" },
            },
          }
        : {}),
    },
    metadata: {
      purpose: "m3-current-formal-case-binding",
      case_id: formalCase.case_id,
      source_archive_digest: formalCase.source_archive_digest,
      source_outcome_digest: formalCase.source_outcome_digest,
    },
  });
  const toolCalls = reconstructToolCalls(result);
  if (toolCalls.length !== 1 || toolCalls[0]?.name !== "bind_formal_case") {
    throw new Error(
      `provider ${provider.providerId} did not call bind_formal_case exactly once`,
    );
  }
  const selectedCall = toolCalls[0];
  if (selectedCall.arguments.binding_token !== bindingToken) {
    throw new Error(`provider ${provider.providerId} returned the wrong binding token`);
  }
  const attempt = result.attempts.at(-1);
  if (
    attempt === undefined
    || attempt.httpStatus === null
    || attempt.httpStatus < 200
    || attempt.httpStatus >= 300
    || attempt.responseDigest === null
  ) {
    throw new Error(`provider ${provider.providerId} has no successful HTTP attempt`);
  }
  const requestDigest = stripDigestPrefix(attempt.requestDigest);
  const responseDigest = stripDigestPrefix(attempt.responseDigest);
  const toolResultDigest = digest({
    tool_call_id: selectedCall.id,
    tool_name: selectedCall.name,
    accepted: true,
    binding_token: bindingToken,
    source_archive_digest: formalCase.source_archive_digest,
    source_outcome_digest: formalCase.source_outcome_digest,
  });
  return {
    schema: "zyra.m3-current-provider-observation/v1",
    observation_id: `${formalCase.case_id}:${provider.providerId}`,
    case_id: formalCase.case_id,
    domain: formalCase.domain,
    repetition: formalCase.repetition,
    run_id: formalCase.source_run_id,
    owner_run_id: formalCase.owner_run_id,
    task_id: formalCase.task_id,
    source_archive_digest: formalCase.source_archive_digest,
    source_outcome_digest: formalCase.source_outcome_digest,
    provider_id: result.providerId,
    model_id: result.modelId,
    protocol: result.protocol,
    live: true,
    fresh: true,
    simulated: false,
    authenticated: true,
    external_model_request: true,
    current_dispatch: true,
    credential_ref: provider.credential.secretRef,
    credential_fingerprint: provider.credential.fingerprint,
    credential_material_persisted: false,
    endpoint: route.baseUrl,
    endpoint_host: new URL(route.baseUrl).hostname,
    endpoint_path: route.endpointPath,
    route_id: route.routeId,
    request_id: requestId,
    dispatch_id: result.dispatchId,
    attempt_id: attempt.attemptId,
    request_digest: requestDigest,
    response_digest: responseDigest,
    stream_evidence_digest: stripDigestPrefix(
      String(result.metadata.streamEvidenceDigest ?? ""),
    ),
    http_status: attempt.httpStatus,
    attempt_count: result.attempts.length,
    stop_reason: result.stopReason,
    tool_call_ids: [selectedCall.id],
    tool_result_ids: [selectedCall.id],
    tool_name: selectedCall.name,
    binding_token_digest: digest(bindingToken),
    tool_result_digest: toolResultDigest,
    usage: result.usage,
    latency_ms: Math.max(0, (attempt.completedAt ?? result.completedAt) - attempt.startedAt),
    started_at: new Date(attempt.startedAt).toISOString(),
    completed_at: new Date(attempt.completedAt ?? result.completedAt).toISOString(),
  };
}

function reconstructToolCalls(
  result: ProviderDispatchResult,
): Array<{ id: string; name: string; arguments: Record<string, unknown> }> {
  const accumulators = new Map<string, { name: string; json: string }>();
  let activeId = "";
  for (const frame of result.frames) {
    if (frame.kind !== "tool_call_delta") continue;
    if (frame.toolCallId) activeId = frame.toolCallId;
    if (!activeId) continue;
    const current = accumulators.get(activeId) ?? { name: "", json: "" };
    if (frame.toolName) current.name = frame.toolName;
    if (frame.jsonDelta) current.json += frame.jsonDelta;
    accumulators.set(activeId, current);
  }
  return [...accumulators.entries()].map(([id, value]) => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(value.json);
    } catch (error) {
      throw new Error(`provider tool arguments are not valid JSON for ${id}: ${String(error)}`);
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error(`provider tool arguments are not an object for ${id}`);
    }
    return {
      id,
      name: value.name,
      arguments: parsed as Record<string, unknown>,
    };
  });
}

function validateInput(value: unknown): ProviderEvidenceInput {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("provider evidence input must be an object");
  }
  const input = value as Record<string, unknown>;
  if (input.schema !== "zyra.m3-current-provider-input/v1") {
    throw new TypeError("provider evidence input schema is invalid");
  }
  if (
    typeof input.campaign_id !== "string"
    || typeof input.implementation_commit !== "string"
    || !/^[0-9a-f]{40}$/.test(input.implementation_commit)
    || !Array.isArray(input.cases)
    || input.cases.length < 1
  ) {
    throw new TypeError("provider evidence input identity is invalid");
  }
  const seen = new Set<string>();
  const cases = input.cases.map((raw) => {
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
      throw new TypeError("formal case must be an object");
    }
    const item = raw as Record<string, unknown>;
    for (const key of [
      "case_id",
      "domain",
      "source_run_id",
      "owner_run_id",
      "task_id",
      "source_archive_digest",
      "source_outcome_digest",
    ]) {
      if (typeof item[key] !== "string" || item[key] === "") {
        throw new TypeError(`formal case field is invalid: ${key}`);
      }
    }
    if (
      !Number.isInteger(item.repetition)
      || Number(item.repetition) < 1
      || !/^[0-9a-f]{64}$/.test(String(item.source_archive_digest))
      || !/^[0-9a-f]{64}$/.test(String(item.source_outcome_digest))
      || seen.has(String(item.case_id))
    ) {
      throw new TypeError("formal case repetition, digest, or identity is invalid");
    }
    seen.add(String(item.case_id));
    return item as unknown as FormalCase;
  });
  return {
    schema: "zyra.m3-current-provider-input/v1",
    campaign_id: input.campaign_id,
    implementation_commit: input.implementation_commit,
    cases,
  };
}

function parseArguments(values: readonly string[]): Map<string, string> {
  const output = new Map<string, string>();
  for (let index = 0; index < values.length; index += 2) {
    const key = values[index];
    const value = values[index + 1];
    if (!key?.startsWith("--") || value === undefined) {
      throw new TypeError("arguments must be --name value pairs");
    }
    output.set(key.slice(2), value);
  }
  return output;
}

function requiredArgument(values: ReadonlyMap<string, string>, key: string): string {
  const value = values.get(key);
  if (!value) throw new TypeError(`--${key} is required`);
  return value;
}

function sumUsage(calls: readonly JsonRecord[], key: string): number {
  return calls.reduce((total, call) => {
    const usage = call.usage;
    if (usage === null || typeof usage !== "object" || Array.isArray(usage)) return total;
    const value = (usage as JsonRecord)[key];
    return total + (typeof value === "number" && Number.isFinite(value) ? value : 0);
  }, 0);
}

function stripDigestPrefix(value: string): string {
  const normalized = value.startsWith("sha256:") ? value.slice(7) : value;
  if (!/^[0-9a-f]{64}$/.test(normalized)) {
    throw new TypeError("expected a SHA-256 digest");
  }
  return normalized;
}

function digest(value: unknown): string {
  return createHash("sha256").update(canonicalJson(value)).digest("hex");
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

function canonicalize(value: unknown): unknown {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError("canonical JSON rejects non-finite numbers");
    return Object.is(value, -0) ? 0 : value;
  }
  if (Array.isArray(value)) return value.map((item) => canonicalize(item));
  if (typeof value === "object") {
    const output: Record<string, unknown> = {};
    for (const key of Object.keys(value).sort()) {
      const item = (value as Record<string, unknown>)[key];
      if (item !== undefined) output[key] = canonicalize(item);
    }
    return output;
  }
  throw new TypeError(`unsupported canonical JSON value: ${typeof value}`);
}

function atomicJson(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  const temporary = `${path}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  renameSync(temporary, path);
}

function slug(value: string): string {
  return value.replaceAll(/[^A-Za-z0-9._-]/g, "-");
}
