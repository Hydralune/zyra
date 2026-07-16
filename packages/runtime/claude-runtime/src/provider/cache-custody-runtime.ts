import { createHash, createHmac } from "node:crypto";

export type CacheTtl = "5m" | "1h";

export interface CacheControlMarker {
  type: "ephemeral";
  ttl?: CacheTtl;
}

export interface ProviderCachePolicyInput {
  sessionId: string;
  providerId: string;
  model: string;
  querySource: string;
  oneHourEnabled: boolean;
  oneHourModels?: readonly string[];
  oneHourSources?: readonly string[];
}

export interface ProviderCacheSample {
  sessionId: string;
  model: string;
  promptHash: string;
  cacheReadInputTokens: number;
  cacheCreationInputTokens?: number;
  observedAtMs: number;
  deletionObserved?: boolean;
  compactionObserved?: boolean;
}

export interface ProviderCacheBreak {
  broken: boolean;
  reason:
    | "none"
    | "excluded-model"
    | "initial-sample"
    | "model-change"
    | "prompt-change"
    | "observation-gap"
    | "cache-read-collapse";
  previousReadTokens: number;
  currentReadTokens: number;
  readRatio: number;
  suppressed: boolean;
}

export interface ToolMismatchEvidence {
  missingResultIds: string[];
  orphanResultIds: string[];
  duplicateUseIds: string[];
  duplicateResultIds: string[];
  toolUseIndexes: Record<string, number[]>;
  toolResultIndexes: Record<string, number[]>;
  preNormalizationCount: number;
  postNormalizationCount: number;
  valid: boolean;
}

export interface AnthropicClientDescriptor {
  family: "anthropic";
  clientKind: "zyra-executable-provider-client/v1";
  transport: "anthropic" | "bedrock" | "foundry" | "vertex";
  endpoint: string;
  model: string;
  auth: "api-key" | "oauth" | "aws" | "aws-bearer" | "azure-api-key" | "azure-ad" | "gcp";
  credentialFingerprint: string;
  headers: Record<string, string>;
  maxRetries: number;
  timeoutMs: number;
  region: string | null;
  projectId: string | null;
  skipAuth: boolean;
  credentialRefresh: "none" | "oauth" | "aws" | "azure-ad" | "gcp";
  source: string;
  proxyUrl: string | null;
  debugLogger: boolean;
  containerId: string | null;
  remoteSessionId: string | null;
  clientApp: string | null;
  sensitiveCustomHeaderNames: string[];
}

export interface AwsCredentialBinding {
  accessKeyId: string;
  secretAccessKey: string;
  sessionToken?: string;
}

export interface AnthropicClientCredentialProviders {
  aws?: () => Promise<AwsCredentialBinding | null>;
  azure?: () => Promise<string | null>;
  gcp?: () => Promise<string | null>;
  oauth?: () => Promise<string | null>;
}

export interface AnthropicClientTransportRequest {
  url: string;
  method: "POST";
  headers: Readonly<Record<string, string>>;
  body: string;
  timeoutMs: number;
  signal?: AbortSignal;
}

export interface AnthropicClientTransportResponse {
  status: number;
  headers: Readonly<Record<string, string>>;
  settle?: (success: boolean) => void;
}

export interface AnthropicClientExecutionSnapshot {
  attempts: number;
  credentialRefreshes: number;
  lastRetryDelayMs: number;
  lastAuthBinding: AnthropicClientDescriptor["auth"] | null;
}

export interface AnthropicExecutableClient extends AnthropicClientDescriptor {
  readonly descriptor: AnthropicClientDescriptor;
  execute<TResponse extends AnthropicClientTransportResponse>(
    request: AnthropicClientTransportRequest,
    transport: { execute(value: AnthropicClientTransportRequest): Promise<TResponse> },
  ): Promise<TResponse>;
  executionSnapshot(): AnthropicClientExecutionSnapshot;
}

export interface AnthropicClientFactoryInput {
  providerId: string;
  endpoint?: string;
  model: string;
  credential: string;
  credentialFingerprint?: string;
  oauth?: boolean;
  sessionId?: string;
  source?: string;
  maxRetries?: number;
  timeoutMs?: number;
  region?: string | null;
  projectId?: string | null;
  proxyUrl?: string | null;
  extraHeaders?: Readonly<Record<string, string>>;
  environment?: Readonly<Record<string, string | undefined>>;
  credentialProviders?: AnthropicClientCredentialProviders;
  now?: () => number;
  sleep?: (milliseconds: number) => Promise<void>;
}

export interface ProviderRequestCustodyEffect {
  applied: boolean;
  ttl: CacheTtl;
  breakpointCount: number;
  mismatch: ToolMismatchEvidence;
  client: AnthropicClientDescriptor | null;
  executableClient?: AnthropicExecutableClient | null;
  cacheBreak: ProviderCacheBreak | null;
  diffPath: string | null;
}

type MutableRecord = Record<string, unknown>;

const recordOf = (value: unknown): MutableRecord | null =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as MutableRecord
    : null;

const stringOf = (value: unknown, fallback = ""): string =>
  typeof value === "string" ? value : fallback;

const numberOf = (value: unknown, fallback = 0): number =>
  typeof value === "number" && Number.isFinite(value) ? value : fallback;

const booleanOf = (value: unknown, fallback = false): boolean =>
  typeof value === "boolean" ? value : fallback;

const integerOf = (value: unknown, fallback: number, minimum: number, maximum: number): number => {
  const parsed = typeof value === "number" ? value : Number.parseInt(String(value ?? ""), 10);
  if (!Number.isSafeInteger(parsed)) return fallback;
  return Math.max(minimum, Math.min(maximum, parsed));
};

const envTruthy = (value: string | undefined): boolean =>
  typeof value === "string" && /^(?:1|true|yes|on)$/i.test(value.trim());

const stringRecord = (value: unknown): Record<string, string> => {
  const record = recordOf(value);
  if (!record) return {};
  const output: Record<string, string> = {};
  for (const [rawName, rawValue] of Object.entries(record)) {
    if (typeof rawValue !== "string") continue;
    const name = rawName.trim().toLowerCase();
    const content = rawValue.trim();
    if (!name || !content || /[\r\n]/.test(name) || /[\r\n]/.test(content)) continue;
    if (name === "host" || name === "content-length") continue;
    output[name] = content;
  }
  return output;
};

const customHeadersFromEnvironment = (
  value: string | undefined,
): { safe: Record<string, string>; sensitiveNames: string[] } => {
  const safe: Record<string, string> = {};
  const sensitiveNames = new Set<string>();
  for (const line of value?.split(/\n|\r\n/) ?? []) {
    const separator = line.indexOf(":");
    if (separator < 1) continue;
    const name = line.slice(0, separator).trim().toLowerCase();
    const content = line.slice(separator + 1).trim();
    if (!name || !content || /[\r\n]/.test(name) || /[\r\n]/.test(content)) continue;
    if (name === "authorization" || name === "x-api-key" || /token|secret|credential/i.test(name)) {
      sensitiveNames.add(name);
      continue;
    }
    if (name !== "host" && name !== "content-length") safe[name] = content;
  }
  return { safe, sensitiveNames: [...sensitiveNames].sort() };
};

const trimEndpoint = (value: string): string => value.replace(/\/+$/, "");

const wildcardMatches = (value: string, pattern: string): boolean => {
  if (pattern === "*") return true;
  if (!pattern.includes("*")) return value === pattern;
  const escaped = pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replaceAll("\\*", ".*");
  return new RegExp(`^${escaped}$`).test(value);
};

const stableFingerprint = (value: string): string => {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
};

const sha256Hex = (value: string): string => createHash("sha256").update(value).digest("hex");

const hmac = (key: string | Uint8Array, value: string): Buffer =>
  createHmac("sha256", key).update(value).digest();

const awsEncode = (value: string): string =>
  encodeURIComponent(value).replace(/[!'()*]/g, (character) =>
    `%${character.charCodeAt(0).toString(16).toUpperCase()}`);

const retryableStatus = (status: number): boolean =>
  status === 408 || status === 409 || status === 425 || status === 429 || status >= 500;

const retryDelay = (
  attempt: number,
  headers: Readonly<Record<string, string>> = {},
): number => {
  const retryAfter = Object.entries(headers).find(([name]) => name.toLowerCase() === "retry-after")?.[1];
  const seconds = Number(retryAfter);
  if (Number.isFinite(seconds) && seconds >= 0) return Math.min(60_000, Math.floor(seconds * 1_000));
  return Math.min(30_000, 250 * (2 ** attempt));
};

const delay = (milliseconds: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

const signBedrockRequest = (
  request: AnthropicClientTransportRequest,
  credential: AwsCredentialBinding,
  region: string,
  nowMs: number,
): AnthropicClientTransportRequest => {
  const url = new URL(request.url);
  const instant = new Date(nowMs).toISOString().replace(/[:-]|\.\d{3}/g, "");
  const amzDate = instant.slice(0, 15) + "Z";
  const date = amzDate.slice(0, 8);
  const payloadHash = sha256Hex(request.body);
  const headers: Record<string, string> = {
    ...request.headers,
    host: url.host,
    "x-amz-content-sha256": payloadHash,
    "x-amz-date": amzDate,
  };
  if (credential.sessionToken) headers["x-amz-security-token"] = credential.sessionToken;
  const canonicalHeaderEntries = Object.entries(headers)
    .map(([name, value]) => [name.toLowerCase(), value.trim().replace(/\s+/g, " ")] as const)
    .sort(([left], [right]) => left.localeCompare(right));
  const signedHeaders = canonicalHeaderEntries.map(([name]) => name).join(";");
  const canonicalHeaders = canonicalHeaderEntries.map(([name, value]) => `${name}:${value}\n`).join("");
  const query = [...url.searchParams.entries()]
    .sort(([leftName, leftValue], [rightName, rightValue]) =>
      leftName.localeCompare(rightName) || leftValue.localeCompare(rightValue))
    .map(([name, value]) => `${awsEncode(name)}=${awsEncode(value)}`)
    .join("&");
  const canonicalRequest = [
    request.method,
    url.pathname.split("/").map(awsEncode).join("/") || "/",
    query,
    canonicalHeaders,
    signedHeaders,
    payloadHash,
  ].join("\n");
  const scope = `${date}/${region}/bedrock/aws4_request`;
  const stringToSign = `AWS4-HMAC-SHA256\n${amzDate}\n${scope}\n${sha256Hex(canonicalRequest)}`;
  const dateKey = hmac(`AWS4${credential.secretAccessKey}`, date);
  const regionKey = hmac(dateKey, region);
  const serviceKey = hmac(regionKey, "bedrock");
  const signingKey = hmac(serviceKey, "aws4_request");
  const signature = createHmac("sha256", signingKey).update(stringToSign).digest("hex");
  headers.authorization = [
    `AWS4-HMAC-SHA256 Credential=${credential.accessKeyId}/${scope}`,
    `SignedHeaders=${signedHeaders}`,
    `Signature=${signature}`,
  ].join(", ");
  return { ...request, headers };
};

const executableAnthropicClient = (
  descriptor: AnthropicClientDescriptor,
  credential: string,
  environment: Readonly<Record<string, string | undefined>>,
  providers: AnthropicClientCredentialProviders,
  now: () => number,
  sleep: (milliseconds: number) => Promise<void>,
): AnthropicExecutableClient => {
  let attempts = 0;
  let credentialRefreshes = 0;
  let lastRetryDelayMs = 0;
  let lastAuthBinding: AnthropicClientDescriptor["auth"] | null = null;

  const bind = async (request: AnthropicClientTransportRequest): Promise<AnthropicClientTransportRequest> => {
    const headers: Record<string, string> = { ...request.headers };
    delete headers.authorization;
    delete headers["x-api-key"];
    lastAuthBinding = descriptor.auth;
    if (descriptor.transport === "anthropic") {
      const refreshed = descriptor.auth === "oauth" ? await providers.oauth?.() : null;
      if (refreshed) credentialRefreshes += 1;
      const secret = refreshed || credential || environment.ANTHROPIC_AUTH_TOKEN || environment.ANTHROPIC_API_KEY || "";
      if (!secret) throw new Error("anthropic_client_credential_missing");
      if (descriptor.auth === "oauth" || environment.ANTHROPIC_AUTH_TOKEN) headers.authorization = `Bearer ${secret}`;
      else headers["x-api-key"] = secret;
      return { ...request, headers };
    }
    if (descriptor.transport === "bedrock") {
      const bearer = environment.AWS_BEARER_TOKEN_BEDROCK || (descriptor.auth === "aws-bearer" ? credential : "");
      if (bearer) return { ...request, headers: { ...headers, authorization: `Bearer ${bearer}` } };
      if (descriptor.skipAuth) return { ...request, headers };
      const refreshed = await providers.aws?.();
      if (refreshed) credentialRefreshes += 1;
      const aws = refreshed ?? (
        environment.AWS_ACCESS_KEY_ID && environment.AWS_SECRET_ACCESS_KEY
          ? {
            accessKeyId: environment.AWS_ACCESS_KEY_ID,
            secretAccessKey: environment.AWS_SECRET_ACCESS_KEY,
            sessionToken: environment.AWS_SESSION_TOKEN,
          }
          : null
      );
      if (!aws) throw new Error("bedrock_aws_credentials_missing");
      return signBedrockRequest({ ...request, headers }, aws, descriptor.region ?? "us-east-1", now());
    }
    if (descriptor.transport === "foundry") {
      const apiKey = environment.ANTHROPIC_FOUNDRY_API_KEY || (descriptor.auth === "azure-api-key" ? credential : "");
      if (apiKey) return { ...request, headers: { ...headers, "api-key": apiKey } };
      if (descriptor.skipAuth) return { ...request, headers };
      const token = await providers.azure?.() || environment.AZURE_ACCESS_TOKEN || credential;
      if (!token) throw new Error("foundry_azure_token_missing");
      credentialRefreshes += 1;
      return { ...request, headers: { ...headers, authorization: `Bearer ${token}` } };
    }
    if (descriptor.skipAuth) return { ...request, headers };
    const token = await providers.gcp?.() || environment.GOOGLE_OAUTH_ACCESS_TOKEN || credential;
    if (!token) throw new Error("vertex_google_token_missing");
    credentialRefreshes += 1;
    return {
      ...request,
      headers: {
        ...headers,
        authorization: `Bearer ${token}`,
        ...(descriptor.projectId ? { "x-goog-user-project": descriptor.projectId } : {}),
      },
    };
  };

  return {
    ...descriptor,
    descriptor: Object.freeze({ ...descriptor, headers: { ...descriptor.headers } }),
    async execute<TResponse extends AnthropicClientTransportResponse>(
      request: AnthropicClientTransportRequest,
      transport: { execute(value: AnthropicClientTransportRequest): Promise<TResponse> },
    ): Promise<TResponse> {
      let lastError: unknown;
      for (let attempt = 0; attempt <= descriptor.maxRetries; attempt += 1) {
        if (request.signal?.aborted) throw request.signal.reason ?? new Error("provider_request_aborted");
        attempts += 1;
        const bound = await bind(request);
        try {
          const response = await transport.execute(bound);
          if (!retryableStatus(response.status) || attempt === descriptor.maxRetries) return response;
          response.settle?.(false);
          lastRetryDelayMs = retryDelay(attempt, response.headers);
          await sleep(lastRetryDelayMs);
        } catch (error) {
          lastError = error;
          if (attempt === descriptor.maxRetries || request.signal?.aborted) throw error;
          lastRetryDelayMs = retryDelay(attempt);
          await sleep(lastRetryDelayMs);
        }
      }
      throw lastError ?? new Error("provider_client_retry_exhausted");
    },
    executionSnapshot(): AnthropicClientExecutionSnapshot {
      return { attempts, credentialRefreshes, lastRetryDelayMs, lastAuthBinding };
    },
  };
};

const cloneValue = <T>(value: T): T => {
  if (Array.isArray(value)) return value.map((item) => cloneValue(item)) as T;
  const record = recordOf(value);
  if (!record) return value;
  return Object.fromEntries(
    Object.entries(record).map(([key, item]) => [key, cloneValue(item)]),
  ) as T;
};

const contentBlocks = (message: unknown): MutableRecord[] => {
  const record = recordOf(message);
  if (!record) return [];
  const content = record.content;
  if (Array.isArray(content)) return content.map(recordOf).filter((item): item is MutableRecord => item !== null);
  const block = recordOf(content);
  return block ? [block] : [];
};

const toolUseId = (block: MutableRecord): string => {
  const type = stringOf(block.type);
  if (type === "tool_use") return stringOf(block.id);
  if (type === "tool_result") return stringOf(block.tool_use_id ?? block.toolUseId);
  return "";
};

export class ProviderCacheCustodyRuntime {
  private readonly oneHourSessions = new Set<string>();
  private readonly samples = new Map<string, ProviderCacheSample>();

  should1hCacheTTL(input: ProviderCachePolicyInput): boolean {
    if (this.oneHourSessions.has(input.sessionId)) return true;
    if (!input.oneHourEnabled) return false;
    if (input.providerId !== "bedrock") return false;
    const models = input.oneHourModels ?? ["claude-*"];
    const sources = input.oneHourSources ?? ["*"];
    const modelEligible = models.some((pattern) => wildcardMatches(input.model, pattern));
    const sourceEligible = sources.some((pattern) => wildcardMatches(input.querySource, pattern));
    if (modelEligible && sourceEligible) this.oneHourSessions.add(input.sessionId);
    return modelEligible && sourceEligible;
  }

  addCacheBreakpoints<T>(
    messages: readonly T[],
    options: {
      ttl: CacheTtl;
      skipCacheWrite?: boolean;
      pinnedToolResultIds?: readonly string[];
    },
  ): T[] {
    const cloned = cloneValue(messages) as unknown[];
    const pinned = new Set(options.pinnedToolResultIds ?? []);
    let selected: MutableRecord | null = null;
    for (const message of cloned) {
      for (const block of contentBlocks(message)) {
        delete block.cacheControl;
        delete block.cache_control;
        if (stringOf(block.type) === "tool_result" && pinned.has(toolUseId(block))) {
          block.cacheReference = true;
        }
      }
    }
    if (!options.skipCacheWrite) {
      for (let messageIndex = cloned.length - 1; messageIndex >= 0 && selected === null; messageIndex -= 1) {
        const message = recordOf(cloned[messageIndex]);
        if (!message || stringOf(message.role) !== "user") continue;
        const blocks = contentBlocks(message);
        for (let blockIndex = blocks.length - 1; blockIndex >= 0; blockIndex -= 1) {
          const block = blocks[blockIndex]!;
          if (stringOf(block.type) === "cache_reference") continue;
          selected = block;
          break;
        }
      }
    }
    if (selected) selected.cacheControl = { type: "ephemeral", ttl: options.ttl } satisfies CacheControlMarker;
    return cloned as T[];
  }

  getCacheBreakDiffPath(root: string, sessionId: string, observedAtMs: number): string {
    const safeSession = sessionId.replace(/[^a-zA-Z0-9._-]/g, "_").slice(0, 96) || "unknown";
    const safeRoot = root.replaceAll("\\", "/").replace(/\/$/, "");
    return `${safeRoot}/${safeSession}/${Math.max(0, Math.trunc(observedAtMs))}-cache-break.json`;
  }

  isExcludedModel(model: string, excluded: readonly string[] = []): boolean {
    return excluded.some((pattern) => wildcardMatches(model, pattern));
  }

  checkResponseForCacheBreak(
    current: ProviderCacheSample,
    options: {
      excludedModels?: readonly string[];
      maxGapMs?: number;
      minimumPreviousReadTokens?: number;
      collapseRatio?: number;
    } = {},
  ): ProviderCacheBreak {
    const previous = this.samples.get(current.sessionId);
    this.samples.set(current.sessionId, { ...current });
    const base = {
      previousReadTokens: previous?.cacheReadInputTokens ?? 0,
      currentReadTokens: current.cacheReadInputTokens,
      readRatio: previous && previous.cacheReadInputTokens > 0
        ? current.cacheReadInputTokens / previous.cacheReadInputTokens
        : 1,
      suppressed: Boolean(current.deletionObserved || current.compactionObserved),
    };
    if (this.isExcludedModel(current.model, options.excludedModels)) {
      return { ...base, broken: false, reason: "excluded-model" };
    }
    if (!previous) return { ...base, broken: false, reason: "initial-sample" };
    if (previous.model !== current.model) return { ...base, broken: true, reason: "model-change" };
    if (previous.promptHash !== current.promptHash && !base.suppressed) {
      return { ...base, broken: true, reason: "prompt-change" };
    }
    if (current.observedAtMs - previous.observedAtMs > (options.maxGapMs ?? 15 * 60_000)) {
      return { ...base, broken: true, reason: "observation-gap" };
    }
    const minimum = options.minimumPreviousReadTokens ?? 128;
    const ratio = options.collapseRatio ?? 0.25;
    if (!base.suppressed && previous.cacheReadInputTokens >= minimum && base.readRatio < ratio) {
      return { ...base, broken: true, reason: "cache-read-collapse" };
    }
    return { ...base, broken: false, reason: "none" };
  }

  logToolUseToolResultMismatch(
    preNormalization: readonly unknown[],
    postNormalization: readonly unknown[],
  ): ToolMismatchEvidence {
    const uses = new Map<string, number[]>();
    const results = new Map<string, number[]>();
    postNormalization.forEach((message, messageIndex) => {
      for (const block of contentBlocks(message)) {
        const type = stringOf(block.type);
        const id = toolUseId(block);
        if (!id) continue;
        const target = type === "tool_use" ? uses : type === "tool_result" ? results : null;
        if (!target) continue;
        const indexes = target.get(id) ?? [];
        indexes.push(messageIndex);
        target.set(id, indexes);
      }
    });
    const missingResultIds = [...uses.keys()].filter((id) => !results.has(id)).sort();
    const orphanResultIds = [...results.keys()].filter((id) => !uses.has(id)).sort();
    const duplicateUseIds = [...uses].filter(([, indexes]) => indexes.length > 1).map(([id]) => id).sort();
    const duplicateResultIds = [...results].filter(([, indexes]) => indexes.length > 1).map(([id]) => id).sort();
    return {
      missingResultIds,
      orphanResultIds,
      duplicateUseIds,
      duplicateResultIds,
      toolUseIndexes: Object.fromEntries(uses),
      toolResultIndexes: Object.fromEntries(results),
      preNormalizationCount: preNormalization.length,
      postNormalizationCount: postNormalization.length,
      valid: missingResultIds.length + orphanResultIds.length + duplicateUseIds.length + duplicateResultIds.length === 0,
    };
  }

  getAssistantMessageFromError(error: unknown): string {
    const record = recordOf(error);
    const status = numberOf(record?.status ?? record?.statusCode, 0);
    const code = stringOf(record?.code).toLowerCase();
    const message = stringOf(record?.message, String(error ?? "unknown provider error"));
    const normalized = `${code} ${message}`.toLowerCase();
    if (status === 401 || status === 403 || /auth|api.?key|credential|permission/.test(normalized)) {
      return "Provider authentication failed. Check the selected credential and provider permissions.";
    }
    if (status === 402 || /billing|credit|payment/.test(normalized)) {
      return "Provider billing is unavailable. Check credits, billing status, and account limits.";
    }
    if (status === 429 || /rate.?limit|too many requests|overloaded/.test(normalized)) {
      return "Provider rate limit reached. The runtime will apply bounded backoff before retrying.";
    }
    if (/timeout|timed out|etimedout|econnaborted|deadline|abort/.test(normalized)) {
      return "Provider request timed out. The runtime can retry with the preserved session state.";
    }
    if (/media|image|pdf|document|unsupported.*format/.test(normalized)) {
      return "Provider rejected an attachment. Check media type, size, and model capabilities.";
    }
    if (/organization|workspace|tenant/.test(normalized)) {
      return "Provider organization access failed. Check the configured organization or workspace.";
    }
    if (/refusal|safety|policy/.test(normalized)) {
      return "Provider declined the request under its safety policy. Revise the request before retrying.";
    }
    return `Provider request failed: ${message.slice(0, 320)}`;
  }

  getAnthropicClient(input: AnthropicClientFactoryInput): AnthropicExecutableClient | null {
    const environment = input.environment ?? process.env;
    const requestedProvider = input.providerId.trim().toLowerCase();
    const transport: AnthropicClientDescriptor["transport"] | null =
      envTruthy(environment.CLAUDE_CODE_USE_BEDROCK) || requestedProvider === "bedrock"
        ? "bedrock"
        : envTruthy(environment.CLAUDE_CODE_USE_FOUNDRY) || requestedProvider === "foundry"
          ? "foundry"
          : envTruthy(environment.CLAUDE_CODE_USE_VERTEX) || requestedProvider === "vertex"
            ? "vertex"
            : requestedProvider === "anthropic"
              ? "anthropic"
              : null;
    if (!transport) return null;

    const custom = customHeadersFromEnvironment(environment.ANTHROPIC_CUSTOM_HEADERS);
    const headers: Record<string, string> = {
      "x-app": "cli",
      "user-agent": "zyra-code-worker/1",
      "x-claude-code-session-id": input.sessionId?.trim() || "default",
      ...stringRecord(input.extraHeaders),
      ...custom.safe,
    };
    const containerId = environment.CLAUDE_CODE_CONTAINER_ID?.trim() || null;
    const remoteSessionId = environment.CLAUDE_CODE_REMOTE_SESSION_ID?.trim() || null;
    const clientApp = environment.CLAUDE_AGENT_SDK_CLIENT_APP?.trim() || null;
    if (containerId) headers["x-claude-remote-container-id"] = containerId;
    if (remoteSessionId) headers["x-claude-remote-session-id"] = remoteSessionId;
    if (clientApp) headers["x-client-app"] = clientApp;
    if (envTruthy(environment.CLAUDE_CODE_ADDITIONAL_PROTECTION)) {
      headers["x-anthropic-additional-protection"] = "true";
    }

    const maxRetries = integerOf(input.maxRetries, 3, 0, 20);
    const timeoutMs = integerOf(
      environment.API_TIMEOUT_MS ?? input.timeoutMs,
      input.timeoutMs ?? 600_000,
      1_000,
      3_600_000,
    );
    const debugLogger = envTruthy(environment.CLAUDE_CODE_DEBUG_TO_STDERR);
    const credentialFingerprint = input.credentialFingerprint?.trim()
      || stableFingerprint(input.credential || `${transport}:${input.model}`);
    let endpoint = input.endpoint?.trim() || "";
    let auth: AnthropicClientDescriptor["auth"];
    let region = input.region?.trim() || null;
    let projectId = input.projectId?.trim() || null;
    let skipAuth = false;
    let credentialRefresh: AnthropicClientDescriptor["credentialRefresh"] = "none";

    if (transport === "bedrock") {
      endpoint ||= "https://bedrock-runtime.amazonaws.com";
      const smallFastRegion = environment.ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION?.trim();
      const isSmallFastModel = /haiku|small[-_ ]?fast/i.test(input.model);
      region = (isSmallFastModel && smallFastRegion)
        ? smallFastRegion
        : region || environment.AWS_REGION?.trim() || environment.AWS_DEFAULT_REGION?.trim() || "us-east-1";
      skipAuth = envTruthy(environment.CLAUDE_CODE_SKIP_BEDROCK_AUTH)
        || Boolean(environment.AWS_BEARER_TOKEN_BEDROCK?.trim());
      auth = environment.AWS_BEARER_TOKEN_BEDROCK?.trim() ? "aws-bearer" : "aws";
      credentialRefresh = skipAuth ? "none" : "aws";
    } else if (transport === "foundry") {
      const resource = environment.ANTHROPIC_FOUNDRY_RESOURCE?.trim();
      endpoint ||= environment.ANTHROPIC_FOUNDRY_BASE_URL?.trim()
        || (resource ? `https://${resource}.services.ai.azure.com` : "https://services.ai.azure.com");
      skipAuth = envTruthy(environment.CLAUDE_CODE_SKIP_FOUNDRY_AUTH);
      auth = environment.ANTHROPIC_FOUNDRY_API_KEY?.trim() ? "azure-api-key" : "azure-ad";
      credentialRefresh = auth === "azure-ad" && !skipAuth ? "azure-ad" : "none";
    } else if (transport === "vertex") {
      endpoint ||= "https://aiplatform.googleapis.com";
      const modelRegionKey = /haiku/i.test(input.model)
        ? "VERTEX_REGION_CLAUDE_HAIKU_4_5"
        : /opus/i.test(input.model)
          ? "VERTEX_REGION_CLAUDE_OPUS_4_5"
          : "VERTEX_REGION_CLAUDE_SONNET_4_5";
      region = environment[modelRegionKey]?.trim()
        || region
        || environment.CLOUD_ML_REGION?.trim()
        || "us-east5";
      projectId = projectId
        || environment.GOOGLE_CLOUD_PROJECT?.trim()
        || environment.GCLOUD_PROJECT?.trim()
        || environment.ANTHROPIC_VERTEX_PROJECT_ID?.trim()
        || null;
      skipAuth = envTruthy(environment.CLAUDE_CODE_SKIP_VERTEX_AUTH);
      auth = "gcp";
      credentialRefresh = skipAuth ? "none" : "gcp";
    } else {
      const staging = environment.USER_TYPE === "ant" && envTruthy(environment.USE_STAGING_OAUTH);
      endpoint ||= staging
        ? environment.CLAUDE_CODE_STAGING_OAUTH_BASE_URL?.trim() || "https://api-staging.anthropic.com"
        : "https://api.anthropic.com";
      auth = input.oauth ? "oauth" : "api-key";
      credentialRefresh = input.oauth ? "oauth" : "none";
      headers["anthropic-version"] = "2023-06-01";
      if (input.oauth) headers.authorization = "Bearer [redacted]";
      else headers["x-api-key"] = "[redacted]";
    }

    const descriptor: AnthropicClientDescriptor = {
      family: "anthropic",
      clientKind: "zyra-executable-provider-client/v1",
      transport,
      endpoint: trimEndpoint(endpoint),
      model: input.model,
      auth,
      credentialFingerprint,
      headers,
      maxRetries,
      timeoutMs,
      region,
      projectId,
      skipAuth,
      credentialRefresh,
      source: input.source?.trim() || "runtime",
      proxyUrl: input.proxyUrl?.trim() || null,
      debugLogger,
      containerId,
      remoteSessionId,
      clientApp,
      sensitiveCustomHeaderNames: custom.sensitiveNames,
    };
    return executableAnthropicClient(
      descriptor,
      input.credential,
      environment,
      input.credentialProviders ?? {},
      input.now ?? Date.now,
      input.sleep ?? delay,
    );
  }

  rehydrateAnthropicClient(
    descriptor: AnthropicClientDescriptor,
    credential: string,
    environment: Readonly<Record<string, string | undefined>> = process.env,
  ): AnthropicExecutableClient {
    return executableAnthropicClient(descriptor, credential, environment, {}, Date.now, delay);
  }

  applyProviderRequestCustody(request: unknown): ProviderRequestCustodyEffect {
    const record = recordOf(request);
    const metadata = recordOf(record?.metadata);
    const providerId = stringOf(record?.providerId ?? record?.provider ?? metadata?.providerId, "compatible");
    const model = stringOf(record?.model ?? metadata?.model, "unknown");
    const sessionId = stringOf(record?.sessionId ?? metadata?.sessionId, "default");
    const querySource = stringOf(record?.querySource ?? metadata?.querySource, "runtime");
    const ttl: CacheTtl = this.should1hCacheTTL({
      sessionId,
      providerId,
      model,
      querySource,
      oneHourEnabled: booleanOf(record?.oneHourCacheEnabled ?? metadata?.oneHourCacheEnabled),
      oneHourModels: Array.isArray(record?.oneHourCacheModels) ? record.oneHourCacheModels.map(String) : undefined,
      oneHourSources: Array.isArray(record?.oneHourCacheSources) ? record.oneHourCacheSources.map(String) : undefined,
    }) ? "1h" : "5m";
    const messages = Array.isArray(record?.messages) ? record.messages : [];
    const normalized = this.addCacheBreakpoints(messages, {
      ttl,
      skipCacheWrite: booleanOf(record?.skipCacheWrite ?? metadata?.skipCacheWrite),
      pinnedToolResultIds: Array.isArray(record?.pinnedToolResultIds)
        ? record.pinnedToolResultIds.map(String)
        : [],
    });
    if (record && Array.isArray(record.messages)) record.messages = normalized;
    const mismatch = this.logToolUseToolResultMismatch(messages, normalized);
    const credential = stringOf(record?.credential ?? metadata?.credential);
    const executableClient = this.getAnthropicClient({
      providerId,
      endpoint: stringOf(record?.endpoint ?? metadata?.endpoint) || undefined,
      model,
      credential,
      credentialFingerprint: stringOf(record?.credentialFingerprint ?? metadata?.credentialFingerprint) || undefined,
      oauth: booleanOf(record?.oauth ?? metadata?.oauth),
      sessionId,
      source: querySource,
      maxRetries: numberOf(record?.maxRetries ?? metadata?.maxRetries, 3),
      timeoutMs: numberOf(record?.timeoutMs ?? metadata?.timeoutMs, 600_000),
      region: stringOf(record?.region ?? metadata?.region) || null,
      projectId: stringOf(record?.projectId ?? metadata?.projectId) || null,
      proxyUrl: stringOf(record?.proxyUrl ?? metadata?.proxyUrl) || null,
      extraHeaders: stringRecord(record?.extraHeaders ?? metadata?.extraHeaders),
    });
    const client = executableClient?.descriptor ?? null;
    const previousSample = recordOf(record?.previousCacheSample);
    const currentSample = recordOf(record?.cacheSample);
    const cacheBreak = currentSample
      ? this.checkResponseForCacheBreak({
        sessionId,
        model,
        promptHash: stringOf(currentSample.promptHash),
        cacheReadInputTokens: numberOf(currentSample.cacheReadInputTokens),
        cacheCreationInputTokens: numberOf(currentSample.cacheCreationInputTokens),
        observedAtMs: numberOf(currentSample.observedAtMs, Date.now()),
        deletionObserved: booleanOf(currentSample.deletionObserved),
        compactionObserved: booleanOf(currentSample.compactionObserved),
      })
      : previousSample
        ? this.checkResponseForCacheBreak({
          sessionId,
          model,
          promptHash: stringOf(previousSample.promptHash),
          cacheReadInputTokens: numberOf(previousSample.cacheReadInputTokens),
          observedAtMs: numberOf(previousSample.observedAtMs, Date.now()),
        })
        : null;
    const diffRoot = stringOf(record?.cacheBreakDiffRoot ?? metadata?.cacheBreakDiffRoot);
    const diffPath = diffRoot
      ? this.getCacheBreakDiffPath(diffRoot, sessionId, Date.now())
      : null;
    const effect: ProviderRequestCustodyEffect = {
      applied: Boolean(record),
      ttl,
      breakpointCount: normalized.flatMap(contentBlocks).filter((block) => block.cacheControl !== undefined).length,
      mismatch,
      client,
      executableClient,
      cacheBreak,
      diffPath,
    };
    if (record) record.sourceCustody = effect;
    return effect;
  }

  snapshot(): { oneHourSessions: string[]; samples: ProviderCacheSample[] } {
    return {
      oneHourSessions: [...this.oneHourSessions].sort(),
      samples: [...this.samples.values()].map((sample) => ({ ...sample })),
    };
  }

  restore(snapshot: { oneHourSessions: readonly string[]; samples: readonly ProviderCacheSample[] }): void {
    this.oneHourSessions.clear();
    for (const sessionId of snapshot.oneHourSessions) this.oneHourSessions.add(sessionId);
    this.samples.clear();
    for (const sample of snapshot.samples) this.samples.set(sample.sessionId, { ...sample });
  }
}

export const providerCacheCustodyRuntime = new ProviderCacheCustodyRuntime();
