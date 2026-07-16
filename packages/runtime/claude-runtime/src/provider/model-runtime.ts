import {
  ProviderCacheCustodyRuntime,
  type AnthropicExecutableClient,
  type ProviderRequestCustodyEffect,
} from "./cache-custody-runtime.js";
import { createHash, randomUUID } from "node:crypto";

import {
  asBoolean,
  asObject,
  asString,
  type JsonObject,
  type JsonValue,
} from "../contracts.ts";
import {
  assertCompatibleResponse,
  compatibleHeaders,
  compatibleRequestBody,
  compatibleRequestUrl,
  consumeCompatibleStream,
  decodeCompatibleResponse,
} from "./compatible-runtime.ts";

export const PROVIDER_MODEL_SNAPSHOT_VERSION = "zyra.provider-model/v1";
export const CLIENT_REQUEST_ID_HEADER = "x-client-request-id";
export const MAX_NON_STREAMING_TOKENS = 64_000;
export const MAX_MEDIA_PER_REQUEST = 20;

export type ProviderKind = "anthropic" | "bedrock" | "vertex" | "compatible" | "local";
export type ProviderAuthKind = "api_key" | "oauth" | "aws" | "gcp" | "none";
export type ModelCapability =
  | "text"
  | "vision"
  | "tools"
  | "thinking"
  | "prompt_cache"
  | "streaming"
  | "effort"
  | "structured_output";
export type StopReason =
  | "end_turn"
  | "max_tokens"
  | "stop_sequence"
  | "tool_use"
  | "refusal"
  | "pause_turn"
  | "unknown";
export type ProviderRequestState =
  | "prepared"
  | "dispatched"
  | "streaming"
  | "completed"
  | "failed"
  | "cancelled";

export interface ModelDescriptor {
  id: string;
  canonicalName: string;
  provider: ProviderKind;
  contextWindow: number;
  maxOutputTokens: number;
  inputPricePerMillion: number;
  outputPricePerMillion: number;
  cacheReadPricePerMillion: number;
  cacheWritePricePerMillion: number;
  capabilities: readonly ModelCapability[];
  aliases: readonly string[];
  deprecated: boolean;
  replacement: string | null;
}

export interface ProviderEndpoint {
  provider: ProviderKind;
  baseUrl: string;
  apiVersion: string;
  region: string | null;
  projectId: string | null;
  timeoutMs: number;
  connectTimeoutMs: number;
  maxConnections: number;
  keepAlive: boolean;
  proxyUrl: string | null;
  extraHeaders: Readonly<Record<string, string>>;
}

export interface ProviderCredential {
  kind: ProviderAuthKind;
  apiKey: string | null;
  accessToken: string | null;
  expiresAt: string | null;
  accountId: string | null;
  source: "config" | "environment" | "credential_chain" | "none";
  fingerprint: string;
}

export interface ProviderTextBlock {
  type: "text";
  text: string;
  cacheControl?: CacheControl;
}

export interface ProviderImageBlock {
  type: "image";
  mediaType: string;
  data: string;
  width?: number;
  height?: number;
  cacheControl?: CacheControl;
}

export interface ProviderDocumentBlock {
  type: "document";
  mediaType: string;
  data: string;
  title?: string;
  cacheControl?: CacheControl;
}

export interface ProviderToolUseBlock {
  type: "tool_use";
  id: string;
  name: string;
  input: JsonObject;
}

export interface ProviderToolResultBlock {
  type: "tool_result";
  toolUseId: string;
  content: string | JsonValue[];
  isError: boolean;
  cacheControl?: CacheControl;
}

export interface ProviderThinkingBlock {
  type: "thinking";
  thinking: string;
  signature: string | null;
}

export type ProviderContentBlock =
  | ProviderTextBlock
  | ProviderImageBlock
  | ProviderDocumentBlock
  | ProviderToolUseBlock
  | ProviderToolResultBlock
  | ProviderThinkingBlock;

export interface ProviderMessage {
  role: "user" | "assistant";
  content: ProviderContentBlock[];
}

export interface ProviderToolDefinition {
  name: string;
  description: string;
  inputSchema: JsonObject;
  strict: boolean;
  deferred: boolean;
  cacheControl?: CacheControl;
}

export interface CacheControl {
  type: "ephemeral";
  ttl: "5m" | "1h";
}

export interface ThinkingConfiguration {
  enabled: boolean;
  budgetTokens: number;
  effort: "low" | "medium" | "high" | "max" | null;
}

export interface ProviderRequestOptions {
  model: string;
  messages: readonly ProviderMessage[];
  system: readonly ProviderTextBlock[];
  tools: readonly ProviderToolDefinition[];
  maxTokens: number;
  temperature: number | null;
  topP: number | null;
  stopSequences: readonly string[];
  stream: boolean;
  thinking: ThinkingConfiguration;
  metadata: JsonObject;
  betaHeaders: readonly string[];
  querySource: string;
  sessionId: string;
  runId: string;
  taskId: string;
  timeoutMs?: number;
}

export interface PreparedProviderRequest {
  sourceCustody: ProviderRequestCustodyEffect;
  requestId: string;
  provider: ProviderKind;
  endpoint: ProviderEndpoint;
  credentialFingerprint: string;
  model: ModelDescriptor;
  headers: Readonly<Record<string, string>>;
  body: JsonObject;
  bodyDigest: string;
  messageCount: number;
  mediaCount: number;
  toolCount: number;
  createdAt: string;
  deadlineAt: string;
}

export interface ProviderUsage {
  inputTokens: number;
  outputTokens: number;
  cacheReadInputTokens: number;
  cacheCreationInputTokens: number;
  serverToolUseTokens: number;
}

export interface ProviderResponse {
  requestId: string;
  providerRequestId: string | null;
  headers: Readonly<Record<string, string>>;
  model: string;
  role: "assistant";
  content: ProviderContentBlock[];
  stopReason: StopReason;
  stopSequence: string | null;
  usage: ProviderUsage;
  metadata: JsonObject;
}

export interface ObservedProviderOutcome {
  ok: boolean;
  providerRequestId?: string | null;
  response?: JsonValue;
  usage?: Partial<ProviderUsage>;
  errorCode?: string | null;
}

export interface ProviderTransportRequest {
  url: string;
  method: "POST";
  headers: Readonly<Record<string, string>>;
  body: string;
  timeoutMs: number;
  signal?: AbortSignal;
}

export interface ProviderTransportResponse {
  status: number;
  headers: Readonly<Record<string, string>>;
  body?: JsonValue;
  stream?: AsyncIterable<string | Uint8Array>;
  settle?: (success: boolean) => void;
}

export interface ProviderTransport {
  execute(request: ProviderTransportRequest): Promise<ProviderTransportResponse>;
}

interface RequestRecord {
  request: PreparedProviderRequest;
  state: ProviderRequestState;
  providerRequestId: string | null;
  responseDigest: string | null;
  errorCode: string | null;
  dispatchedAt: string | null;
  completedAt: string | null;
  clientAttempts: number;
  clientCredentialRefreshes: number;
  clientLastRetryDelayMs: number;
  revision: number;
}

export interface ProviderModelSnapshot {
  version: typeof PROVIDER_MODEL_SNAPSHOT_VERSION;
  revision: number;
  activeModel: string;
  models: ModelDescriptor[];
  endpoint: ProviderEndpoint;
  credential: Omit<ProviderCredential, "apiKey" | "accessToken">;
  requests: RequestRecord[];
  usage: ProviderUsage;
  sourceCustody?: ReturnType<ProviderCacheCustodyRuntime["snapshot"]>;
  checksum: string;
}

interface StreamBlockState {
  index: number;
  type: ProviderContentBlock["type"];
  text: string;
  thinking: string;
  signature: string | null;
  toolId: string | null;
  toolName: string | null;
  toolInputJson: string;
}

interface StreamState {
  requestId: string;
  providerRequestId: string | null;
  model: string;
  stopReason: StopReason;
  stopSequence: string | null;
  usage: ProviderUsage;
  blocks: Map<number, StreamBlockState>;
  opened: boolean;
  closed: boolean;
  eventCount: number;
}

const EMPTY_USAGE: ProviderUsage = Object.freeze({
  inputTokens: 0,
  outputTokens: 0,
  cacheReadInputTokens: 0,
  cacheCreationInputTokens: 0,
  serverToolUseTokens: 0,
});

const DEFAULT_ENDPOINTS: Record<ProviderKind, ProviderEndpoint> = {
  anthropic: {
    provider: "anthropic",
    baseUrl: "https://api.anthropic.com",
    apiVersion: "2023-06-01",
    region: null,
    projectId: null,
    timeoutMs: 600_000,
    connectTimeoutMs: 30_000,
    maxConnections: 16,
    keepAlive: true,
    proxyUrl: null,
    extraHeaders: {},
  },
  bedrock: {
    provider: "bedrock",
    baseUrl: "https://bedrock-runtime.amazonaws.com",
    apiVersion: "2023-09-30",
    region: "us-east-1",
    projectId: null,
    timeoutMs: 600_000,
    connectTimeoutMs: 30_000,
    maxConnections: 16,
    keepAlive: true,
    proxyUrl: null,
    extraHeaders: {},
  },
  vertex: {
    provider: "vertex",
    baseUrl: "https://aiplatform.googleapis.com",
    apiVersion: "v1",
    region: "us-central1",
    projectId: null,
    timeoutMs: 600_000,
    connectTimeoutMs: 30_000,
    maxConnections: 16,
    keepAlive: true,
    proxyUrl: null,
    extraHeaders: {},
  },
  compatible: {
    provider: "compatible",
    baseUrl: "http://127.0.0.1:8080",
    apiVersion: "v1",
    region: null,
    projectId: null,
    timeoutMs: 600_000,
    connectTimeoutMs: 10_000,
    maxConnections: 8,
    keepAlive: true,
    proxyUrl: null,
    extraHeaders: {},
  },
  local: {
    provider: "local",
    baseUrl: "http://127.0.0.1:11434",
    apiVersion: "v1",
    region: null,
    projectId: null,
    timeoutMs: 600_000,
    connectTimeoutMs: 10_000,
    maxConnections: 4,
    keepAlive: true,
    proxyUrl: null,
    extraHeaders: {},
  },
};

const DEFAULT_MODELS: readonly ModelDescriptor[] = Object.freeze([
  Object.freeze({
    id: "claude-sonnet-4-5",
    canonicalName: "claude-sonnet-4-5",
    provider: "anthropic" as const,
    contextWindow: 200_000,
    maxOutputTokens: 64_000,
    inputPricePerMillion: 3,
    outputPricePerMillion: 15,
    cacheReadPricePerMillion: 0.3,
    cacheWritePricePerMillion: 3.75,
    capabilities: ["text", "vision", "tools", "thinking", "prompt_cache", "streaming", "effort", "structured_output"] as const,
    aliases: ["sonnet", "sonnet-4.5"] as const,
    deprecated: false,
    replacement: null,
  }),
  Object.freeze({
    id: "claude-opus-4-1",
    canonicalName: "claude-opus-4-1",
    provider: "anthropic" as const,
    contextWindow: 200_000,
    maxOutputTokens: 32_000,
    inputPricePerMillion: 15,
    outputPricePerMillion: 75,
    cacheReadPricePerMillion: 1.5,
    cacheWritePricePerMillion: 18.75,
    capabilities: ["text", "vision", "tools", "thinking", "prompt_cache", "streaming"] as const,
    aliases: ["opus", "opus-4.1"] as const,
    deprecated: false,
    replacement: null,
  }),
  Object.freeze({
    id: "claude-haiku-3-5",
    canonicalName: "claude-haiku-3-5",
    provider: "anthropic" as const,
    contextWindow: 200_000,
    maxOutputTokens: 8_192,
    inputPricePerMillion: 0.8,
    outputPricePerMillion: 4,
    cacheReadPricePerMillion: 0.08,
    cacheWritePricePerMillion: 1,
    capabilities: ["text", "vision", "tools", "prompt_cache", "streaming"] as const,
    aliases: ["haiku", "haiku-3.5"] as const,
    deprecated: false,
    replacement: null,
  }),
]);

export class ProviderConfigurationError extends Error {
  readonly code: string;
  readonly details: JsonObject;

  constructor(code: string, message: string, details: JsonObject = {}) {
    super(message);
    this.name = "ProviderConfigurationError";
    this.code = code;
    this.details = details;
  }
}

export class ProviderProtocolError extends Error {
  readonly code: string;
  readonly status: number | null;
  readonly retryable: boolean;
  readonly details: JsonObject;

  constructor(
    code: string,
    message: string,
    options: { status?: number; retryable?: boolean; details?: JsonObject } = {},
  ) {
    super(message);
    this.name = "ProviderProtocolError";
    this.code = code;
    this.status = options.status ?? null;
    this.retryable = options.retryable ?? false;
    this.details = options.details ?? {};
  }
}

export class ProviderModelRuntime {
  private readonly sourceCustody = new ProviderCacheCustodyRuntime();
  private readonly models = new Map<string, ModelDescriptor>();
  private readonly aliases = new Map<string, string>();
  private readonly requests = new Map<string, RequestRecord>();
  private readonly executableClients = new Map<string, AnthropicExecutableClient>();
  private endpoint: ProviderEndpoint;
  private credential: ProviderCredential;
  private activeModel: string;
  private usage: ProviderUsage = { ...EMPTY_USAGE };
  private revision = 0;

  constructor(options: {
    model?: string;
    provider?: ProviderKind;
    endpoint?: Partial<ProviderEndpoint>;
    credential?: Partial<ProviderCredential>;
    models?: readonly ModelDescriptor[];
  } = {}) {
    const descriptors = options.models ?? DEFAULT_MODELS;
    for (const descriptor of descriptors) this.registerModel(descriptor);
    const provider = options.provider ?? this.resolveModel(options.model ?? DEFAULT_MODELS[0].id).provider;
    this.endpoint = normalizeEndpoint(provider, options.endpoint);
    this.credential = normalizeCredential(provider, options.credential);
    this.activeModel = this.resolveModel(options.model ?? DEFAULT_MODELS[0].id).id;
  }

  client_module(value: JsonObject = {}): JsonObject {
    const provider = providerKind(asString(value.provider, this.endpoint.provider));
    if (provider !== this.endpoint.provider || value.base_url !== undefined) {
      this.endpoint = normalizeEndpoint(provider, {
        ...this.endpoint,
        baseUrl: asString(value.base_url, this.endpoint.baseUrl),
        region: nullableString(value.region, this.endpoint.region),
        projectId: nullableString(value.project_id, this.endpoint.projectId),
        proxyUrl: nullableString(value.proxy_url, this.endpoint.proxyUrl),
        keepAlive: asBoolean(value.keep_alive, this.endpoint.keepAlive),
      });
      this.revision += 1;
    }
    return {
      provider: this.endpoint.provider,
      base_url: this.endpoint.baseUrl,
      api_version: this.endpoint.apiVersion,
      credential_kind: this.credential.kind,
      credential_fingerprint: this.credential.fingerprint,
      revision: this.revision,
    };
  }

  claude_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "select_model") {
      const model = this.selectModel(asString(value.model));
      return modelToJson(model);
    }
    if (action === "prepare") {
      const options = providerOptionsFromJson(asObject(value.options));
      const prepared = this.prepare(options);
      return preparedToJson(prepared);
    }
    if (action === "snapshot") return this.snapshot() as unknown as JsonObject;
    if (action === "usage") return usageToJson(this.usage);
    return {
      active_model: this.activeModel,
      provider: this.endpoint.provider,
      request_count: this.requests.size,
      revision: this.revision,
    };
  }

  registerModel(value: ModelDescriptor): void {
    const descriptor = validateModel(value);
    if (this.models.has(descriptor.id)) {
      throw new ProviderConfigurationError("duplicate_model", `model already registered: ${descriptor.id}`);
    }
    this.models.set(descriptor.id, descriptor);
    this.aliases.set(descriptor.id.toLowerCase(), descriptor.id);
    this.aliases.set(descriptor.canonicalName.toLowerCase(), descriptor.id);
    for (const alias of descriptor.aliases) {
      const key = alias.trim().toLowerCase();
      const existing = this.aliases.get(key);
      if (existing && existing !== descriptor.id) {
        throw new ProviderConfigurationError("duplicate_model_alias", `model alias is ambiguous: ${alias}`);
      }
      this.aliases.set(key, descriptor.id);
    }
  }

  resolveModel(name: string): ModelDescriptor {
    const key = name.trim().toLowerCase();
    const id = this.aliases.get(key) ?? name;
    const descriptor = this.models.get(id);
    if (!descriptor) {
      throw new ProviderConfigurationError("unknown_model", `unknown provider model: ${name}`, {
        requested_model: name,
        available_models: [...this.models.keys()],
      });
    }
    if (descriptor.deprecated && descriptor.replacement) {
      return this.resolveModel(descriptor.replacement);
    }
    return descriptor;
  }

  selectModel(name: string): ModelDescriptor {
    const descriptor = this.resolveModel(name);
    if (descriptor.provider !== this.endpoint.provider && this.endpoint.provider !== "compatible") {
      this.endpoint = normalizeEndpoint(descriptor.provider, {});
      this.credential = normalizeCredential(descriptor.provider, {});
    }
    this.activeModel = descriptor.id;
    this.revision += 1;
    return descriptor;
  }

  configureEndpoint(value: Partial<ProviderEndpoint>): ProviderEndpoint {
    const provider = value.provider ?? this.endpoint.provider;
    const providerChanged = provider !== this.endpoint.provider;
    this.endpoint = normalizeEndpoint(provider, providerChanged ? value : {
      ...this.endpoint,
      ...value,
    });
    this.revision += 1;
    return structuredClone(this.endpoint);
  }

  configureCredential(value: Partial<ProviderCredential>): ProviderCredential {
    this.credential = normalizeCredential(this.endpoint.provider, {
      ...this.credential,
      ...value,
    });
    this.revision += 1;
    return structuredClone(this.credential);
  }

  buildSystemPromptBlocks(
    blocks: readonly ProviderTextBlock[],
    ttl: CacheControl["ttl"] = "5m",
  ): ProviderTextBlock[] {
    const normalized = normalizeSystemBlocks(blocks);
    if (normalized.length === 0) return [];
    return normalized.map((block, index) => ({
      ...block,
      cacheControl: index === normalized.length - 1 ? { type: "ephemeral", ttl } : block.cacheControl,
    }));
  }

  getMaxOutputTokensForModel(model: ModelDescriptor, requested: number): number {
    const environmentLimit = Number(process.env.ZYRA_MAX_OUTPUT_TOKENS ?? "");
    const configuredLimit = Number.isSafeInteger(environmentLimit) && environmentLimit > 0
      ? environmentLimit
      : model.maxOutputTokens;
    return clampInteger(requested, 1, Math.min(model.maxOutputTokens, configuredLimit));
  }

  adjustParamsForNonStreaming(
    maxTokens: number,
    thinking: ThinkingConfiguration,
    stream: boolean,
  ): { maxTokens: number; thinking: ThinkingConfiguration } {
    if (stream) return { maxTokens, thinking: structuredClone(thinking) };
    const cappedTokens = Math.min(maxTokens, MAX_NON_STREAMING_TOKENS);
    const cappedThinking = thinking.enabled
      ? { ...thinking, budgetTokens: Math.min(thinking.budgetTokens, Math.max(1, cappedTokens - 1)) }
      : structuredClone(thinking);
    return { maxTokens: cappedTokens, thinking: cappedThinking };
  }

  prepare(options: ProviderRequestOptions): PreparedProviderRequest {
    const custodyEnvelope: Record<string, unknown> = {
      ...options,
      providerId: asString(options.metadata.providerId, this.endpoint.provider),
      endpoint: this.endpoint.baseUrl,
      model: options.model || this.activeModel,
      sessionId: options.sessionId,
      querySource: options.querySource,
      messages: structuredClone(options.messages),
      metadata: structuredClone(options.metadata),
      credential: this.credential.apiKey ?? this.credential.accessToken ?? "",
      credentialFingerprint: this.credential.fingerprint,
      oauth: this.credential.kind === "oauth",
      maxRetries: clampInteger(Number(options.metadata.providerMaxRetries ?? 3), 0, 20),
      timeoutMs: options.timeoutMs ?? this.endpoint.timeoutMs,
      region: this.endpoint.region,
      projectId: this.endpoint.projectId,
      proxyUrl: this.endpoint.proxyUrl,
      extraHeaders: this.endpoint.extraHeaders,
      oneHourCacheEnabled: options.metadata.oneHourCacheEnabled === true,
    };
    const sourceCustody = this.sourceCustody.applyProviderRequestCustody(custodyEnvelope);
    const executableClient = sourceCustody.executableClient ?? null;
    delete sourceCustody.executableClient;
    const effectiveOptions: ProviderRequestOptions = {
      ...options,
      messages: Array.isArray(custodyEnvelope.messages)
        ? custodyEnvelope.messages as ProviderMessage[]
        : options.messages,
    };
    const model = this.resolveModel(options.model || this.activeModel);
    assertCapability(model, "text");
    if (options.stream) assertCapability(model, "streaming");
    if (options.tools.length > 0) assertCapability(model, "tools");
    if (options.thinking.enabled) assertCapability(model, "thinking");
    const messages = normalizeMessages(effectiveOptions.messages);
    enforceToolPairs(messages);
    const mediaCount = countMedia(messages);
    if (mediaCount > MAX_MEDIA_PER_REQUEST) {
      throw new ProviderConfigurationError(
        "too_many_media_items",
        `request contains ${mediaCount} media items; maximum is ${MAX_MEDIA_PER_REQUEST}`,
        { media_count: mediaCount, maximum: MAX_MEDIA_PER_REQUEST },
      );
    }
    const requestedMaxTokens = this.getMaxOutputTokensForModel(model, options.maxTokens);
    const requestedThinking = normalizeThinking(options.thinking, requestedMaxTokens, model);
    const adjusted = this.adjustParamsForNonStreaming(requestedMaxTokens, requestedThinking, options.stream);
    const maxTokens = adjusted.maxTokens;
    const thinking = normalizeThinking(adjusted.thinking, maxTokens, model);
    const requestId = randomUUID();
    const client = sourceCustody.client;
    const clientProvider: ProviderKind = client?.transport === "foundry"
      ? "anthropic"
      : client?.transport ?? this.endpoint.provider;
    const effectiveEndpoint = client
      ? normalizeEndpoint(clientProvider, {
        ...this.endpoint,
        provider: clientProvider,
        baseUrl: client.endpoint,
        region: client.region,
        projectId: client.projectId,
        proxyUrl: client.proxyUrl,
      })
      : structuredClone(this.endpoint);
    const timeoutMs = clampInteger(
      options.timeoutMs ?? client?.timeoutMs ?? effectiveEndpoint.timeoutMs,
      effectiveEndpoint.connectTimeoutMs,
      3_600_000,
    );
    const system = this.buildSystemPromptBlocks(options.system, sourceCustody.ttl);
    const tools = normalizeTools(options.tools);
    const body = this.buildBody({
      ...effectiveOptions,
      messages,
      system,
      tools,
      model: model.id,
      maxTokens,
      thinking,
    }, effectiveEndpoint);
    const headers = this.buildHeaders(requestId, options.betaHeaders, effectiveEndpoint, client?.headers ?? {});
    const now = Date.now();
    const prepared: PreparedProviderRequest = {
      sourceCustody,
      requestId,
      provider: effectiveEndpoint.provider,
      endpoint: structuredClone(effectiveEndpoint),
      credentialFingerprint: this.credential.fingerprint,
      model,
      headers,
      body,
      bodyDigest: digest(body),
      messageCount: messages.length,
      mediaCount,
      toolCount: tools.length,
      createdAt: new Date(now).toISOString(),
      deadlineAt: new Date(now + timeoutMs).toISOString(),
    };
    if (executableClient) this.executableClients.set(requestId, executableClient);
    this.requests.set(requestId, {
      request: prepared,
      state: "prepared",
      providerRequestId: null,
      responseDigest: null,
      errorCode: null,
      dispatchedAt: null,
      completedAt: null,
      clientAttempts: 0,
      clientCredentialRefreshes: 0,
      clientLastRetryDelayMs: 0,
      revision: 1,
    });
    this.revision += 1;
    return structuredClone(prepared);
  }

  async queryWithModel(
    prepared: PreparedProviderRequest,
    transport: ProviderTransport,
    signal?: AbortSignal,
  ): Promise<ProviderResponse> {
    return this.execute(prepared, transport, signal);
  }

  async queryHaiku(
    prepared: PreparedProviderRequest,
    transport: ProviderTransport,
    signal?: AbortSignal,
  ): Promise<ProviderResponse> {
    if (!prepared.model.canonicalName.toLowerCase().includes("haiku")) {
      throw new ProviderConfigurationError("haiku_model_required", "queryHaiku requires a Haiku model");
    }
    if (prepared.toolCount > 0 || prepared.body.stream === true || prepared.body.thinking !== undefined) {
      throw new ProviderConfigurationError(
        "haiku_query_contract",
        "queryHaiku requires a non-streaming request without tools or extended thinking",
      );
    }
    return this.queryWithModel(prepared, transport, signal);
  }

  async execute(
    prepared: PreparedProviderRequest,
    transport: ProviderTransport,
    signal?: AbortSignal,
  ): Promise<ProviderResponse> {
    const record = this.requireRecord(prepared.requestId);
    this.transition(record, "prepared", "dispatched");
    record.dispatchedAt = new Date().toISOString();
    const request: ProviderTransportRequest = {
      url: providerRequestUrl(prepared),
      method: "POST",
      headers: prepared.headers,
      body: canonicalJson(prepared.body),
      timeoutMs: Math.max(1, Date.parse(prepared.deadlineAt) - Date.now()),
      signal,
    };
    let response: ProviderTransportResponse;
    const executableClient = this.executableClients.get(prepared.requestId)
      ?? (prepared.sourceCustody.client
        ? this.sourceCustody.rehydrateAnthropicClient(
          prepared.sourceCustody.client,
          this.credential.apiKey ?? this.credential.accessToken ?? "",
        )
        : null);
    if (executableClient && !this.executableClients.has(prepared.requestId)) {
      this.executableClients.set(prepared.requestId, executableClient);
    }
    const recordClientState = (): void => {
      if (!executableClient) return;
      const state = executableClient.executionSnapshot();
      record.clientAttempts = state.attempts;
      record.clientCredentialRefreshes = state.credentialRefreshes;
      record.clientLastRetryDelayMs = state.lastRetryDelayMs;
    };
    try {
      response = executableClient
        ? await executableClient.execute(request, transport)
        : await transport.execute(request);
      recordClientState();
    } catch (error) {
      recordClientState();
      record.state = signal?.aborted ? "cancelled" : "failed";
      record.errorCode = signal?.aborted ? "request_cancelled" : "transport_error";
      record.completedAt = new Date().toISOString();
      record.revision += 1;
      this.revision += 1;
      throw error;
    }
    record.providerRequestId = header(response.headers, "request-id")
      ?? header(response.headers, "x-request-id")
      ?? null;
    if (response.status < 200 || response.status >= 300) {
      record.state = "failed";
      record.errorCode = `http_${response.status}`;
      record.completedAt = new Date().toISOString();
      record.revision += 1;
      this.revision += 1;
      throw providerHttpError(response);
    }
    let parsed: ProviderResponse;
    let responseSettled = false;
    const settleResponse = (success: boolean): void => {
      if (responseSettled) return;
      responseSettled = true;
      response.settle?.(success);
    };
    try {
      if (prepared.body.stream === true) {
        if (!response.stream) {
          throw new ProviderProtocolError("missing_stream", "streaming response did not include a stream");
        }
        record.state = "streaming";
        record.revision += 1;
        if (prepared.provider === "compatible" || prepared.provider === "local") {
          const compatible = await consumeCompatibleStream(response.stream);
          assertCompatibleResponse(compatible);
          parsed = parseProviderResponse(prepared.requestId, compatible.normalized);
        } else {
          parsed = await consumeProviderStream(prepared.requestId, response.stream);
        }
      } else if (prepared.provider === "compatible" || prepared.provider === "local") {
        const compatible = decodeCompatibleResponse(response.body);
        assertCompatibleResponse(compatible);
        parsed = parseProviderResponse(prepared.requestId, compatible.normalized);
      } else {
        parsed = parseProviderResponse(prepared.requestId, response.body);
      }
      settleResponse(true);
    } catch (error) {
      settleResponse(false);
      record.state = signal?.aborted ? "cancelled" : "failed";
      record.errorCode = signal?.aborted ? "request_cancelled" : "provider_protocol_error";
      record.completedAt = new Date().toISOString();
      record.revision += 1;
      this.revision += 1;
      throw error;
    }
    if (!parsed.providerRequestId) parsed.providerRequestId = record.providerRequestId;
    parsed.headers = { ...response.headers };
    record.state = "completed";
    record.responseDigest = digest(responseToJson(parsed));
    record.completedAt = new Date().toISOString();
    record.revision += 1;
    this.usage = addUsage(this.usage, parsed.usage);
    this.revision += 1;
    return parsed;
  }

  observeOutcome(requestId: string, outcome: ObservedProviderOutcome): JsonObject {
    const record = this.requireRecord(requestId);
    const responseDigest = digest(outcome.response ?? null);
    const terminal = record.state === "completed" || record.state === "failed" || record.state === "cancelled";
    if (terminal) {
      const expectedState = outcome.ok ? "completed" : "failed";
      if (record.state !== expectedState) {
        throw new ProviderProtocolError(
          "observed_outcome_conflict",
          `provider request ${requestId} is ${record.state}, not ${expectedState}`,
        );
      }
      return requestRecordToJson(record);
    }
    if (record.state !== "prepared" && record.state !== "dispatched" && record.state !== "streaming") {
      throw new ProviderProtocolError(
        "observed_outcome_state",
        `provider request ${requestId} cannot settle from ${record.state}`,
      );
    }
    record.providerRequestId = outcome.providerRequestId ?? record.providerRequestId;
    record.completedAt = new Date().toISOString();
    record.revision += 1;
    if (outcome.ok) {
      record.state = "completed";
      record.responseDigest = responseDigest;
      record.errorCode = null;
      this.usage = addUsage(this.usage, normalizeUsage(outcome.usage ?? {}));
    } else {
      record.state = "failed";
      record.responseDigest = null;
      record.errorCode = outcome.errorCode?.trim() || "observed_provider_failure";
    }
    this.revision += 1;
    return requestRecordToJson(record);
  }

  markCancelled(requestId: string): void {
    const record = this.requireRecord(requestId);
    if (record.state === "completed" || record.state === "failed") return;
    record.state = "cancelled";
    record.errorCode = "request_cancelled";
    record.completedAt = new Date().toISOString();
    record.revision += 1;
    this.revision += 1;
  }

  requestState(requestId: string): JsonObject {
    const record = this.requireRecord(requestId);
    return requestRecordToJson(record);
  }

  snapshot(): ProviderModelSnapshot {
    const unsigned: Omit<ProviderModelSnapshot, "checksum"> = {
      version: PROVIDER_MODEL_SNAPSHOT_VERSION,
      revision: this.revision,
      activeModel: this.activeModel,
      models: [...this.models.values()].map((value) => structuredClone(value)),
      endpoint: structuredClone(this.endpoint),
      credential: {
        kind: this.credential.kind,
        expiresAt: this.credential.expiresAt,
        accountId: this.credential.accountId,
        source: this.credential.source,
        fingerprint: this.credential.fingerprint,
      },
      requests: [...this.requests.values()].map((value) => structuredClone(value)),
      usage: { ...this.usage },
      sourceCustody: this.sourceCustody.snapshot(),
    };
    return {
      ...unsigned,
      checksum: digest(unsigned),
    };
  }

  restore(snapshot: ProviderModelSnapshot, secret: Partial<ProviderCredential> = {}): void {
    if (snapshot.version !== PROVIDER_MODEL_SNAPSHOT_VERSION) {
      throw new ProviderConfigurationError("snapshot_version", "unsupported provider model snapshot");
    }
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) {
      throw new ProviderConfigurationError("snapshot_checksum", "provider model snapshot checksum mismatch");
    }
    this.models.clear();
    this.aliases.clear();
    for (const descriptor of snapshot.models) this.registerModel(descriptor);
    this.endpoint = normalizeEndpoint(snapshot.endpoint.provider, snapshot.endpoint);
    this.credential = normalizeCredential(snapshot.endpoint.provider, {
      ...snapshot.credential,
      ...secret,
    });
    this.activeModel = this.resolveModel(snapshot.activeModel).id;
    this.requests.clear();
    this.executableClients.clear();
    for (const record of snapshot.requests) {
      this.requests.set(record.request.requestId, structuredClone(record));
    }
    this.usage = normalizeUsage(snapshot.usage);
    this.sourceCustody.restore(snapshot.sourceCustody ?? { oneHourSessions: [], samples: [] });
    this.revision = snapshot.revision;
  }

  private buildBody(options: ProviderRequestOptions, endpoint = this.endpoint): JsonObject {
    if (endpoint.provider === "compatible" || endpoint.provider === "local") {
      const systemMessages: JsonObject[] = options.system.map((block) => ({
        role: "system",
        content: block.text,
      }));
      return compatibleRequestBody({
        baseUrl: endpoint.baseUrl,
        model: options.model,
        messages: [...systemMessages, ...options.messages.map(messageToJson)],
        tools: options.tools.map(toolToJson),
        maximumTokens: options.maxTokens,
        temperature: options.temperature,
        stream: options.stream,
        metadata: {
          ...options.metadata,
          user_id: digest({ session_id: options.sessionId }).slice(0, 32),
          zyra_run_id: options.runId,
          zyra_task_id: options.taskId,
          query_source: options.querySource,
        },
      });
    }
    const body: JsonObject = {
      model: options.model,
      messages: options.messages.map(messageToJson),
      max_tokens: options.maxTokens,
      stream: options.stream,
      metadata: {
        ...options.metadata,
        user_id: digest({ session_id: options.sessionId }).slice(0, 32),
        zyra_run_id: options.runId,
        zyra_task_id: options.taskId,
        query_source: options.querySource,
      },
    };
    if (options.system.length > 0) body.system = options.system.map(contentBlockToJson);
    if (options.tools.length > 0) body.tools = options.tools.map(toolToJson);
    if (options.temperature !== null) body.temperature = boundedFloat(options.temperature, 0, 1);
    if (options.topP !== null) body.top_p = boundedFloat(options.topP, 0, 1);
    if (options.stopSequences.length > 0) body.stop_sequences = [...options.stopSequences];
    if (options.thinking.enabled) {
      body.thinking = {
        type: "enabled",
        budget_tokens: options.thinking.budgetTokens,
      };
      delete body.temperature;
      delete body.top_p;
    }
    if (options.thinking.effort) body.output_config = { effort: options.thinking.effort };
    return body;
  }

  private buildHeaders(
    requestId: string,
    betas: readonly string[],
    endpoint = this.endpoint,
    clientHeaders: Readonly<Record<string, string>> = {},
  ): Readonly<Record<string, string>> {
    if (endpoint.provider === "compatible" || endpoint.provider === "local") {
      const secret = this.credential.apiKey ?? this.credential.accessToken;
      return Object.freeze(compatibleHeaders(secret, {
        [CLIENT_REQUEST_ID_HEADER]: requestId,
        "user-agent": "zyra-code-worker/1",
        ...endpoint.extraHeaders,
        ...clientHeaders,
      }));
    }
    const headers: Record<string, string> = {
      "content-type": "application/json",
      "accept": "application/json",
      "anthropic-version": endpoint.apiVersion,
      [CLIENT_REQUEST_ID_HEADER]: requestId,
      "user-agent": "zyra-code-worker/1",
      ...endpoint.extraHeaders,
      ...clientHeaders,
    };
    const betaHeader = [...new Set(betas.map((value) => value.trim()).filter(Boolean))].sort().join(",");
    if (betaHeader) headers["anthropic-beta"] = betaHeader;
    if (endpoint.provider === "anthropic" && this.credential.kind === "api_key" && this.credential.apiKey) {
      headers["x-api-key"] = this.credential.apiKey;
    }
    if (endpoint.provider === "anthropic" && this.credential.kind === "oauth" && this.credential.accessToken) {
      headers.authorization = `Bearer ${this.credential.accessToken}`;
    }
    return Object.freeze(headers);
  }

  private requireRecord(requestId: string): RequestRecord {
    const record = this.requests.get(requestId);
    if (!record) throw new ProviderConfigurationError("unknown_request", `unknown request: ${requestId}`);
    return record;
  }

  private transition(
    record: RequestRecord,
    expected: ProviderRequestState,
    next: ProviderRequestState,
  ): void {
    if (record.state !== expected) {
      throw new ProviderProtocolError(
        "invalid_request_transition",
        `request ${record.request.requestId} is ${record.state}; expected ${expected}`,
      );
    }
    record.state = next;
    record.revision += 1;
    this.revision += 1;
  }
}

function validateModel(value: ModelDescriptor): ModelDescriptor {
  if (!value.id.trim()) throw new ProviderConfigurationError("model_id", "model id is required");
  if (!value.canonicalName.trim()) {
    throw new ProviderConfigurationError("model_name", "canonical model name is required");
  }
  if (!Number.isSafeInteger(value.contextWindow) || value.contextWindow < 1024) {
    throw new ProviderConfigurationError("context_window", "model context window is invalid");
  }
  if (!Number.isSafeInteger(value.maxOutputTokens) || value.maxOutputTokens < 1) {
    throw new ProviderConfigurationError("output_tokens", "model output token limit is invalid");
  }
  const capabilities = [...new Set(value.capabilities)];
  if (!capabilities.includes("text")) {
    throw new ProviderConfigurationError("model_capability", "model must support text");
  }
  return Object.freeze({
    ...structuredClone(value),
    id: value.id.trim(),
    canonicalName: value.canonicalName.trim(),
    aliases: Object.freeze([...new Set(value.aliases.map((item) => item.trim()).filter(Boolean))]),
    capabilities: Object.freeze(capabilities),
  });
}

function normalizeEndpoint(
  provider: ProviderKind,
  value: Partial<ProviderEndpoint> | undefined,
): ProviderEndpoint {
  const defaults = DEFAULT_ENDPOINTS[provider];
  const baseUrl = (value?.baseUrl ?? defaults.baseUrl).trim().replace(/\/+$/, "");
  let parsed: URL;
  try {
    parsed = new URL(baseUrl);
  } catch {
    throw new ProviderConfigurationError("endpoint_url", `invalid provider base URL: ${baseUrl}`);
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    throw new ProviderConfigurationError("endpoint_protocol", "provider endpoint must use HTTP or HTTPS");
  }
  return {
    provider,
    baseUrl,
    apiVersion: (value?.apiVersion ?? defaults.apiVersion).trim(),
    region: nullableString(value?.region, defaults.region),
    projectId: nullableString(value?.projectId, defaults.projectId),
    timeoutMs: clampInteger(value?.timeoutMs ?? defaults.timeoutMs, 1_000, 3_600_000),
    connectTimeoutMs: clampInteger(value?.connectTimeoutMs ?? defaults.connectTimeoutMs, 100, 300_000),
    maxConnections: clampInteger(value?.maxConnections ?? defaults.maxConnections, 1, 256),
    keepAlive: value?.keepAlive ?? defaults.keepAlive,
    proxyUrl: nullableString(value?.proxyUrl, defaults.proxyUrl),
    extraHeaders: normalizeHeaders(value?.extraHeaders ?? defaults.extraHeaders),
  };
}

function normalizeCredential(
  provider: ProviderKind,
  value: Partial<ProviderCredential> | undefined,
): ProviderCredential {
  const defaultKind: ProviderAuthKind = provider === "anthropic"
    ? "api_key"
    : provider === "bedrock"
    ? "aws"
    : provider === "vertex"
    ? "gcp"
    : "none";
  const kind = authKind(value?.kind ?? defaultKind);
  const apiKey = nullableString(value?.apiKey, null);
  const accessToken = nullableString(value?.accessToken, null);
  const expiresAt = nullableString(value?.expiresAt, null);
  const accountId = nullableString(value?.accountId, null);
  const source = value?.source ?? "none";
  const secret = apiKey ?? accessToken ?? `${kind}:${source}:${accountId ?? "anonymous"}`;
  return {
    kind,
    apiKey,
    accessToken,
    expiresAt,
    accountId,
    source,
    fingerprint: value?.fingerprint ?? digest(secret).slice(0, 32),
  };
}

function normalizeHeaders(value: Readonly<Record<string, string>>): Readonly<Record<string, string>> {
  const headers: Record<string, string> = {};
  for (const [rawName, rawValue] of Object.entries(value)) {
    const name = rawName.trim().toLowerCase();
    const headerValue = rawValue.trim();
    if (!name || !headerValue) continue;
    if (name === "host" || name === "content-length") continue;
    if (/[\r\n]/.test(name) || /[\r\n]/.test(headerValue)) {
      throw new ProviderConfigurationError("header_injection", "provider header contains a newline");
    }
    headers[name] = headerValue;
  }
  return Object.freeze(headers);
}

function normalizeMessages(values: readonly ProviderMessage[]): ProviderMessage[] {
  const result: ProviderMessage[] = [];
  for (const value of values) {
    if (value.role !== "user" && value.role !== "assistant") {
      throw new ProviderConfigurationError("message_role", `unsupported provider role: ${String(value.role)}`);
    }
    const content = value.content.map(normalizeContentBlock);
    if (content.length === 0) continue;
    const previous = result.at(-1);
    if (previous?.role === value.role) previous.content.push(...content);
    else result.push({ role: value.role, content });
  }
  if (result.length === 0) {
    throw new ProviderConfigurationError("empty_messages", "provider request must contain a message");
  }
  if (result[0].role !== "user") {
    result.unshift({ role: "user", content: [{ type: "text", text: "Continue the task." }] });
  }
  return result;
}

function normalizeContentBlock(value: ProviderContentBlock): ProviderContentBlock {
  if (value.type === "text") {
    return {
      type: "text",
      text: value.text,
      ...(value.cacheControl ? { cacheControl: normalizeCacheControl(value.cacheControl) } : {}),
    };
  }
  if (value.type === "image") {
    if (!value.mediaType.startsWith("image/")) {
      throw new ProviderConfigurationError("image_media_type", "image block has invalid media type");
    }
    return {
      ...structuredClone(value),
      data: value.data.replace(/^data:[^;]+;base64,/, ""),
      ...(value.cacheControl ? { cacheControl: normalizeCacheControl(value.cacheControl) } : {}),
    };
  }
  if (value.type === "document") {
    return {
      ...structuredClone(value),
      data: value.data.replace(/^data:[^;]+;base64,/, ""),
      ...(value.cacheControl ? { cacheControl: normalizeCacheControl(value.cacheControl) } : {}),
    };
  }
  if (value.type === "tool_use") {
    if (!value.id || !value.name) {
      throw new ProviderConfigurationError("tool_use_identity", "tool use block requires id and name");
    }
    return { ...structuredClone(value), input: asObject(value.input) };
  }
  if (value.type === "tool_result") {
    if (!value.toolUseId) {
      throw new ProviderConfigurationError("tool_result_identity", "tool result requires tool use id");
    }
    return {
      ...structuredClone(value),
      ...(value.cacheControl ? { cacheControl: normalizeCacheControl(value.cacheControl) } : {}),
    };
  }
  if (value.type === "thinking") {
    return {
      type: "thinking",
      thinking: value.thinking,
      signature: value.signature,
    };
  }
  throw new ProviderConfigurationError("content_block", "unsupported provider content block");
}

function normalizeSystemBlocks(values: readonly ProviderTextBlock[]): ProviderTextBlock[] {
  const result: ProviderTextBlock[] = [];
  for (const value of values) {
    const text = value.text.trim();
    if (!text) continue;
    const previous = result.at(-1);
    if (previous && !previous.cacheControl && !value.cacheControl) previous.text += `\n\n${text}`;
    else result.push({ type: "text", text, ...(value.cacheControl ? { cacheControl: normalizeCacheControl(value.cacheControl) } : {}) });
  }
  return result;
}

function normalizeTools(values: readonly ProviderToolDefinition[]): ProviderToolDefinition[] {
  const names = new Set<string>();
  const tools: ProviderToolDefinition[] = [];
  for (const value of values) {
    const name = value.name.trim();
    if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(name)) {
      throw new ProviderConfigurationError("tool_name", `invalid provider tool name: ${name}`);
    }
    if (names.has(name)) throw new ProviderConfigurationError("duplicate_tool", `duplicate tool: ${name}`);
    names.add(name);
    const schema = normalizeSchema(value.inputSchema);
    tools.push({
      name,
      description: value.description.trim().slice(0, 8_000),
      inputSchema: schema,
      strict: value.strict,
      deferred: value.deferred,
      ...(value.cacheControl ? { cacheControl: normalizeCacheControl(value.cacheControl) } : {}),
    });
  }
  return tools;
}

function normalizeSchema(value: JsonObject): JsonObject {
  const schema = structuredClone(value);
  if (schema.type === undefined) schema.type = "object";
  if (schema.type !== "object") {
    throw new ProviderConfigurationError("tool_schema", "provider tool input schema must be an object");
  }
  if (schema.properties === undefined) schema.properties = {};
  if (schema.additionalProperties === undefined) schema.additionalProperties = false;
  return sortJson(schema) as JsonObject;
}

function enforceToolPairs(messages: readonly ProviderMessage[]): void {
  const pending = new Map<string, string>();
  const settled = new Set<string>();
  for (const message of messages) {
    for (const block of message.content) {
      if (block.type === "tool_use") {
        if (pending.has(block.id) || settled.has(block.id)) {
          throw new ProviderConfigurationError("duplicate_tool_use", `duplicate tool use id: ${block.id}`);
        }
        pending.set(block.id, block.name);
      }
      if (block.type === "tool_result") {
        if (!pending.has(block.toolUseId)) {
          throw new ProviderConfigurationError(
            "orphan_tool_result",
            `tool result has no preceding tool use: ${block.toolUseId}`,
          );
        }
        pending.delete(block.toolUseId);
        settled.add(block.toolUseId);
      }
    }
  }
  if (pending.size > 0) {
    throw new ProviderConfigurationError("missing_tool_result", "provider messages contain unsettled tool uses", {
      pending_tool_use_ids: [...pending.keys()],
    });
  }
}

function normalizeThinking(
  value: ThinkingConfiguration,
  maxTokens: number,
  model: ModelDescriptor,
): ThinkingConfiguration {
  if (!value.enabled) return { enabled: false, budgetTokens: 0, effort: value.effort };
  assertCapability(model, "thinking");
  const ceiling = Math.max(1_024, maxTokens - 1_024);
  return {
    enabled: true,
    budgetTokens: clampInteger(value.budgetTokens, 1_024, ceiling),
    effort: value.effort,
  };
}

function assertCapability(model: ModelDescriptor, capability: ModelCapability): void {
  if (!model.capabilities.includes(capability)) {
    throw new ProviderConfigurationError(
      "unsupported_model_capability",
      `model ${model.id} does not support ${capability}`,
      { model: model.id, capability },
    );
  }
}

function countMedia(messages: readonly ProviderMessage[]): number {
  let total = 0;
  for (const message of messages) {
    for (const block of message.content) {
      if (block.type === "image" || block.type === "document") total += 1;
    }
  }
  return total;
}

function providerRequestUrl(value: PreparedProviderRequest): string {
  const base = value.endpoint.baseUrl;
  if (value.provider === "compatible" || value.provider === "local") return compatibleRequestUrl(base);
  if (value.provider === "anthropic") return `${base}/v1/messages`;
  if (value.provider === "bedrock") {
    const model = encodeURIComponent(value.model.id);
    return `${base}/model/${model}/invoke-with-response-stream`;
  }
  if (value.provider === "vertex") {
    const project = encodeURIComponent(value.endpoint.projectId ?? "missing-project");
    const region = encodeURIComponent(value.endpoint.region ?? "us-central1");
    const model = encodeURIComponent(value.model.id);
    return `${base}/${value.endpoint.apiVersion}/projects/${project}/locations/${region}/publishers/anthropic/models/${model}:streamRawPredict`;
  }
  return `${base}/v1/messages`;
}

function providerHttpError(value: ProviderTransportResponse): ProviderProtocolError {
  const body = asObject(value.body);
  const nested = asObject(body.error);
  const message = asString(nested.message, asString(body.message, `provider returned HTTP ${value.status}`));
  const code = asString(nested.type, asString(nested.code, `http_${value.status}`));
  return new ProviderProtocolError(code, message, {
    status: value.status,
    retryable: value.status === 408 || value.status === 409 || value.status === 429 || value.status >= 500,
    details: {
      response: redactJson(body),
      retry_after: header(value.headers, "retry-after") ?? null,
      request_id: header(value.headers, "request-id") ?? header(value.headers, "x-request-id") ?? null,
    },
  });
}

export function parseProviderResponse(requestId: string, value: JsonValue | undefined): ProviderResponse {
  const body = asObject(value);
  const rawContent = Array.isArray(body.content) ? body.content : [];
  const content = rawContent.map((item) => parseResponseBlock(asObject(item)));
  return {
    requestId,
    providerRequestId: nullableString(body.id, null),
    headers: {},
    model: asString(body.model, "unknown"),
    role: "assistant",
    content,
    stopReason: stopReason(body.stop_reason),
    stopSequence: nullableString(body.stop_sequence, null),
    usage: normalizeUsage(asObject(body.usage)),
    metadata: {
      response_type: asString(body.type, "message"),
      content_digest: digest(content.map(contentBlockToJson)),
    },
  };
}

function parseResponseBlock(value: JsonObject): ProviderContentBlock {
  const type = asString(value.type);
  if (type === "text") return { type, text: asString(value.text) };
  if (type === "thinking" || type === "redacted_thinking") {
    return {
      type: "thinking",
      thinking: asString(value.thinking, asString(value.data)),
      signature: nullableString(value.signature, null),
    };
  }
  if (type === "tool_use") {
    return {
      type,
      id: asString(value.id),
      name: asString(value.name),
      input: asObject(value.input),
    };
  }
  throw new ProviderProtocolError("response_block", `unsupported response content block: ${type}`);
}

export async function consumeProviderStream(
  requestId: string,
  source: AsyncIterable<string | Uint8Array>,
): Promise<ProviderResponse> {
  const state: StreamState = {
    requestId,
    providerRequestId: null,
    model: "unknown",
    stopReason: "unknown",
    stopSequence: null,
    usage: { ...EMPTY_USAGE },
    blocks: new Map(),
    opened: false,
    closed: false,
    eventCount: 0,
  };
  let buffer = "";
  const decoder = new TextDecoder();
  for await (const chunk of source) {
    buffer += typeof chunk === "string" ? chunk : decoder.decode(chunk, { stream: true });
    const frames = splitSseFrames(buffer);
    buffer = frames.remainder;
    for (const frame of frames.frames) applyStreamFrame(state, frame);
  }
  buffer += decoder.decode();
  if (buffer.trim()) {
    const frames = splitSseFrames(`${buffer}\n\n`);
    for (const frame of frames.frames) applyStreamFrame(state, frame);
  }
  if (!state.opened) throw new ProviderProtocolError("stream_start", "provider stream has no message_start");
  if (!state.closed) throw new ProviderProtocolError("stream_end", "provider stream ended before message_stop");
  const content = [...state.blocks.values()]
    .sort((left, right) => left.index - right.index)
    .map(streamBlockToContent);
  return {
    requestId,
    providerRequestId: state.providerRequestId,
    headers: {},
    model: state.model,
    role: "assistant",
    content,
    stopReason: state.stopReason,
    stopSequence: state.stopSequence,
    usage: state.usage,
    metadata: {
      stream_event_count: state.eventCount,
      content_digest: digest(content.map(contentBlockToJson)),
    },
  };
}

function splitSseFrames(value: string): { frames: string[]; remainder: string } {
  const normalized = value.replaceAll("\r\n", "\n");
  const parts = normalized.split("\n\n");
  const remainder = parts.pop() ?? "";
  return { frames: parts.filter((item) => item.trim()), remainder };
}

function applyStreamFrame(state: StreamState, frame: string): void {
  let eventName = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  const raw = dataLines.join("\n");
  if (!raw || raw === "[DONE]") {
    if (raw === "[DONE]") state.closed = true;
    return;
  }
  let payload: JsonObject;
  try {
    payload = asObject(JSON.parse(raw));
  } catch {
    throw new ProviderProtocolError("stream_json", `invalid provider stream frame: ${eventName}`);
  }
  const type = asString(payload.type, eventName);
  state.eventCount += 1;
  if (type === "message_start") {
    if (state.opened) throw new ProviderProtocolError("stream_duplicate_start", "duplicate message_start");
    const message = asObject(payload.message);
    state.opened = true;
    state.providerRequestId = nullableString(message.id, null);
    state.model = asString(message.model, "unknown");
    state.usage = addUsage(state.usage, normalizeUsage(asObject(message.usage)));
    return;
  }
  if (!state.opened) throw new ProviderProtocolError("stream_order", `${type} arrived before message_start`);
  if (type === "content_block_start") {
    const index = integer(payload.index, -1);
    if (index < 0 || state.blocks.has(index)) {
      throw new ProviderProtocolError("stream_block_index", `invalid stream block index: ${index}`);
    }
    const block = asObject(payload.content_block);
    const blockType = asString(block.type) as ProviderContentBlock["type"];
    state.blocks.set(index, {
      index,
      type: blockType,
      text: asString(block.text),
      thinking: asString(block.thinking),
      signature: nullableString(block.signature, null),
      toolId: nullableString(block.id, null),
      toolName: nullableString(block.name, null),
      toolInputJson: block.input ? canonicalJson(block.input) : "",
    });
    return;
  }
  if (type === "content_block_delta") {
    const index = integer(payload.index, -1);
    const block = state.blocks.get(index);
    if (!block) throw new ProviderProtocolError("stream_missing_block", `delta for missing block: ${index}`);
    const delta = asObject(payload.delta);
    const deltaType = asString(delta.type);
    if (deltaType === "text_delta") block.text += asString(delta.text);
    else if (deltaType === "thinking_delta") block.thinking += asString(delta.thinking);
    else if (deltaType === "signature_delta") block.signature = `${block.signature ?? ""}${asString(delta.signature)}`;
    else if (deltaType === "input_json_delta") block.toolInputJson += asString(delta.partial_json);
    else throw new ProviderProtocolError("stream_delta", `unsupported content delta: ${deltaType}`);
    return;
  }
  if (type === "content_block_stop") {
    const index = integer(payload.index, -1);
    if (!state.blocks.has(index)) {
      throw new ProviderProtocolError("stream_missing_block", `stop for missing block: ${index}`);
    }
    return;
  }
  if (type === "message_delta") {
    const delta = asObject(payload.delta);
    state.stopReason = stopReason(delta.stop_reason);
    state.stopSequence = nullableString(delta.stop_sequence, null);
    state.usage = addUsage(state.usage, normalizeUsage(asObject(payload.usage)));
    return;
  }
  if (type === "message_stop") {
    state.closed = true;
    return;
  }
  if (type === "ping") return;
  if (type === "error") {
    const error = asObject(payload.error);
    throw new ProviderProtocolError(
      asString(error.type, "stream_error"),
      asString(error.message, "provider stream error"),
      { retryable: true, details: redactJson(error) },
    );
  }
}

function streamBlockToContent(value: StreamBlockState): ProviderContentBlock {
  if (value.type === "text") return { type: "text", text: value.text };
  if (value.type === "thinking") {
    return { type: "thinking", thinking: value.thinking, signature: value.signature };
  }
  if (value.type === "tool_use") {
    let input: JsonObject = {};
    try {
      input = asObject(JSON.parse(value.toolInputJson || "{}"));
    } catch {
      throw new ProviderProtocolError("tool_input_json", `invalid streamed tool input for ${value.toolName}`);
    }
    return {
      type: "tool_use",
      id: value.toolId ?? randomUUID(),
      name: value.toolName ?? "unknown",
      input,
    };
  }
  throw new ProviderProtocolError("stream_block", `unsupported streamed block: ${value.type}`);
}

function normalizeUsage(value: Partial<ProviderUsage> | JsonObject): ProviderUsage {
  const record = value as Record<string, unknown>;
  return {
    inputTokens: nonnegative(record.inputTokens ?? record.input_tokens),
    outputTokens: nonnegative(record.outputTokens ?? record.output_tokens),
    cacheReadInputTokens: nonnegative(record.cacheReadInputTokens ?? record.cache_read_input_tokens),
    cacheCreationInputTokens: nonnegative(
      record.cacheCreationInputTokens ?? record.cache_creation_input_tokens,
    ),
    serverToolUseTokens: nonnegative(record.serverToolUseTokens ?? record.server_tool_use_tokens),
  };
}

export function addUsage(left: ProviderUsage, right: ProviderUsage): ProviderUsage {
  return {
    inputTokens: left.inputTokens + right.inputTokens,
    outputTokens: left.outputTokens + right.outputTokens,
    cacheReadInputTokens: left.cacheReadInputTokens + right.cacheReadInputTokens,
    cacheCreationInputTokens: left.cacheCreationInputTokens + right.cacheCreationInputTokens,
    serverToolUseTokens: left.serverToolUseTokens + right.serverToolUseTokens,
  };
}

function contentBlockToJson(value: ProviderContentBlock): JsonObject {
  if (value.type === "text") {
    return { type: "text", text: value.text, ...(value.cacheControl ? { cache_control: cacheControlToJson(value.cacheControl) } : {}) };
  }
  if (value.type === "image" || value.type === "document") {
    return {
      type: value.type,
      source: { type: "base64", media_type: value.mediaType, data: value.data },
      ...(value.type === "document" && value.title ? { title: value.title } : {}),
      ...(value.cacheControl ? { cache_control: cacheControlToJson(value.cacheControl) } : {}),
    };
  }
  if (value.type === "tool_use") {
    return { type: "tool_use", id: value.id, name: value.name, input: value.input };
  }
  if (value.type === "tool_result") {
    return {
      type: "tool_result",
      tool_use_id: value.toolUseId,
      content: value.content,
      is_error: value.isError,
      ...(value.cacheControl ? { cache_control: cacheControlToJson(value.cacheControl) } : {}),
    };
  }
  return {
    type: "thinking",
    thinking: value.thinking,
    signature: value.signature,
  };
}

function messageToJson(value: ProviderMessage): JsonObject {
  return { role: value.role, content: value.content.map(contentBlockToJson) };
}

function toolToJson(value: ProviderToolDefinition): JsonObject {
  return {
    name: value.name,
    description: value.description,
    input_schema: value.inputSchema,
    strict: value.strict,
    defer_loading: value.deferred,
    ...(value.cacheControl ? { cache_control: cacheControlToJson(value.cacheControl) } : {}),
  };
}

function cacheControlToJson(value: CacheControl): JsonObject {
  return { type: value.type, ttl: value.ttl };
}

function responseToJson(value: ProviderResponse): JsonObject {
  return {
    request_id: value.requestId,
    provider_request_id: value.providerRequestId,
    model: value.model,
    role: value.role,
    content: value.content.map(contentBlockToJson),
    stop_reason: value.stopReason,
    stop_sequence: value.stopSequence,
    usage: usageToJson(value.usage),
    metadata: value.metadata,
  };
}

function preparedToJson(value: PreparedProviderRequest): JsonObject {
  return {
    request_id: value.requestId,
    provider: value.provider,
    endpoint: value.endpoint.baseUrl,
    credential_fingerprint: value.credentialFingerprint,
    model: value.model.id,
    headers: redactHeaders(value.headers),
    body: value.body,
    body_digest: value.bodyDigest,
    message_count: value.messageCount,
    media_count: value.mediaCount,
    tool_count: value.toolCount,
    created_at: value.createdAt,
    deadline_at: value.deadlineAt,
  };
}

function requestRecordToJson(value: RequestRecord): JsonObject {
  return {
    request_id: value.request.requestId,
    state: value.state,
    provider_request_id: value.providerRequestId,
    response_digest: value.responseDigest,
    error_code: value.errorCode,
    dispatched_at: value.dispatchedAt,
    completed_at: value.completedAt,
    client_attempts: value.clientAttempts,
    client_credential_refreshes: value.clientCredentialRefreshes,
    client_last_retry_delay_ms: value.clientLastRetryDelayMs,
    revision: value.revision,
  };
}

function modelToJson(value: ModelDescriptor): JsonObject {
  return {
    id: value.id,
    canonical_name: value.canonicalName,
    provider: value.provider,
    context_window: value.contextWindow,
    max_output_tokens: value.maxOutputTokens,
    capabilities: [...value.capabilities],
    deprecated: value.deprecated,
    replacement: value.replacement,
  };
}

function usageToJson(value: ProviderUsage): JsonObject {
  return {
    input_tokens: value.inputTokens,
    output_tokens: value.outputTokens,
    cache_read_input_tokens: value.cacheReadInputTokens,
    cache_creation_input_tokens: value.cacheCreationInputTokens,
    server_tool_use_tokens: value.serverToolUseTokens,
  };
}

function providerOptionsFromJson(value: JsonObject): ProviderRequestOptions {
  const rawMessages = Array.isArray(value.messages) ? value.messages : [];
  const rawSystem = Array.isArray(value.system) ? value.system : [];
  const rawTools = Array.isArray(value.tools) ? value.tools : [];
  const thinking = asObject(value.thinking);
  return {
    model: asString(value.model, "claude-sonnet-4-5"),
    messages: rawMessages.map(messageFromJson),
    system: rawSystem.map((item) => ({ type: "text", text: asString(asObject(item).text) })),
    tools: rawTools.map(toolFromJson),
    maxTokens: integer(value.max_tokens, 8_192),
    temperature: nullableNumber(value.temperature),
    topP: nullableNumber(value.top_p),
    stopSequences: Array.isArray(value.stop_sequences)
      ? value.stop_sequences.map((item) => String(item))
      : [],
    stream: asBoolean(value.stream, true),
    thinking: {
      enabled: asBoolean(thinking.enabled, false),
      budgetTokens: integer(thinking.budget_tokens, 0),
      effort: effort(thinking.effort),
    },
    metadata: asObject(value.metadata),
    betaHeaders: Array.isArray(value.beta_headers) ? value.beta_headers.map(String) : [],
    querySource: asString(value.query_source, "runtime"),
    sessionId: asString(value.session_id, "session-unknown"),
    runId: asString(value.run_id, "run-unknown"),
    taskId: asString(value.task_id, "task-unknown"),
    timeoutMs: integer(value.timeout_ms, 600_000),
  };
}

function messageFromJson(value: JsonValue): ProviderMessage {
  const record = asObject(value);
  const role = asString(record.role) === "assistant" ? "assistant" : "user";
  const raw = Array.isArray(record.content) ? record.content : [{ type: "text", text: asString(record.content) }];
  return { role, content: raw.map((item) => blockFromJson(asObject(item))) };
}

function blockFromJson(value: JsonObject): ProviderContentBlock {
  const type = asString(value.type, "text");
  if (type === "tool_use") {
    return { type, id: asString(value.id), name: asString(value.name), input: asObject(value.input) };
  }
  if (type === "tool_result") {
    return {
      type,
      toolUseId: asString(value.tool_use_id),
      content: Array.isArray(value.content) ? value.content : asString(value.content),
      isError: asBoolean(value.is_error, false),
    };
  }
  if (type === "thinking") {
    return { type, thinking: asString(value.thinking), signature: nullableString(value.signature, null) };
  }
  if (type === "image") {
    const source = asObject(value.source);
    return {
      type,
      mediaType: asString(source.media_type, "image/png"),
      data: asString(source.data),
      width: integer(value.width, 0) || undefined,
      height: integer(value.height, 0) || undefined,
    };
  }
  if (type === "document") {
    const source = asObject(value.source);
    return {
      type,
      mediaType: asString(source.media_type, "application/pdf"),
      data: asString(source.data),
      title: nullableString(value.title, null) ?? undefined,
    };
  }
  return { type: "text", text: asString(value.text) };
}

function toolFromJson(value: JsonValue): ProviderToolDefinition {
  const record = asObject(value);
  return {
    name: asString(record.name),
    description: asString(record.description),
    inputSchema: asObject(record.input_schema),
    strict: asBoolean(record.strict, true),
    deferred: asBoolean(record.defer_loading, false),
  };
}

function normalizeCacheControl(value: CacheControl): CacheControl {
  return { type: "ephemeral", ttl: value.ttl === "1h" ? "1h" : "5m" };
}

function redactHeaders(value: Readonly<Record<string, string>>): JsonObject {
  const result: JsonObject = {};
  for (const [name, headerValue] of Object.entries(value)) {
    result[name] = /authorization|api-key|token|cookie/i.test(name) ? "[redacted]" : headerValue;
  }
  return result;
}

function redactJson(value: JsonObject): JsonObject {
  const result: JsonObject = {};
  for (const [key, item] of Object.entries(value)) {
    if (/api.?key|authorization|access.?token|secret|password/i.test(key)) result[key] = "[redacted]";
    else if (Array.isArray(item)) result[key] = item.map((child) => child && typeof child === "object" ? redactJson(asObject(child)) : child);
    else if (item && typeof item === "object") result[key] = redactJson(asObject(item));
    else result[key] = item;
  }
  return result;
}

function header(value: Readonly<Record<string, string>>, name: string): string | null {
  const target = name.toLowerCase();
  for (const [key, item] of Object.entries(value)) {
    if (key.toLowerCase() === target) return item;
  }
  return null;
}

function stopReason(value: unknown): StopReason {
  const reason = String(value ?? "unknown");
  if (reason === "end_turn") return reason;
  if (reason === "max_tokens") return reason;
  if (reason === "stop_sequence") return reason;
  if (reason === "tool_use") return reason;
  if (reason === "refusal") return reason;
  if (reason === "pause_turn") return reason;
  return "unknown";
}

function providerKind(value: string): ProviderKind {
  if (value === "anthropic" || value === "bedrock" || value === "vertex" || value === "compatible" || value === "local") return value;
  throw new ProviderConfigurationError("provider_kind", `unsupported provider: ${value}`);
}

function authKind(value: string): ProviderAuthKind {
  if (value === "api_key" || value === "oauth" || value === "aws" || value === "gcp" || value === "none") return value;
  throw new ProviderConfigurationError("auth_kind", `unsupported provider auth kind: ${value}`);
}

function effort(value: unknown): ThinkingConfiguration["effort"] {
  const normalized = String(value ?? "");
  if (normalized === "low" || normalized === "medium" || normalized === "high" || normalized === "max") return normalized;
  return null;
}

function nullableString(value: unknown, fallback: string | null): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function integer(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.floor(value) : fallback;
}

function nonnegative(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
}

function clampInteger(value: number, minimum: number, maximum: number): number {
  if (!Number.isFinite(value)) return minimum;
  return Math.max(minimum, Math.min(maximum, Math.floor(value)));
}

function boundedFloat(value: number, minimum: number, maximum: number): number {
  if (!Number.isFinite(value)) return minimum;
  return Math.max(minimum, Math.min(maximum, value));
}

function sortJson(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map(sortJson);
  if (value && typeof value === "object") {
    const result: JsonObject = {};
    for (const key of Object.keys(value).sort()) result[key] = sortJson(value[key]);
    return result;
  }
  return value;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function digest(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}
