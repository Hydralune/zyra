import type { JsonObject } from "../contracts.ts";
import {
  cloneJson,
  deterministicMcpId,
  monotonicNow,
  sha256,
} from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import type {
  McpContent,
  McpCreateMessageRequest,
  McpCreateMessageResult,
} from "../core/protocol.ts";

export interface McpSamplingContext {
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  requestId: string;
  sessionId: string;
  taskId: string;
  workspaceRoot: string;
  policyDigest: string;
  allowedModels: string[];
  maximumTokens: number;
  maximumContextTokens: number;
  allowTools: boolean;
  allowServerContext: boolean;
  metadata: JsonObject;
}

export interface McpSamplingProviderRequest {
  requestId: string;
  serverId: string;
  modelHints: string[];
  messages: { role: "user" | "assistant"; content: McpContent }[];
  systemPrompt: string | null;
  maxTokens: number;
  temperature: number | null;
  stopSequences: string[];
  metadata: JsonObject;
}

export interface McpSamplingProviderResponse {
  role: "assistant" | "user";
  content: McpContent;
  model: string;
  stopReason: string | null;
  inputTokens: number;
  outputTokens: number;
  costMicros: number;
  metadata: JsonObject;
}

export type McpSamplingProvider = (
  request: McpSamplingProviderRequest,
  signal?: AbortSignal,
) => Promise<McpSamplingProviderResponse>;

export interface McpSamplingRecord {
  samplingId: string;
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  requestId: string;
  sessionId: string;
  taskId: string;
  requestDigest: string;
  status: "pending" | "committed" | "failed" | "cancelled";
  selectedModel: string | null;
  inputTokens: number;
  outputTokens: number;
  costMicros: number;
  resultDigest: string | null;
  createdAt: string;
  completedAt: string | null;
  metadata: JsonObject;
}

export interface McpSamplingSnapshot {
  version: "zyra.mcp-sampling-runtime/v1";
  revision: number;
  records: McpSamplingRecord[];
  tokenUsageByServer: Record<string, number>;
  costMicrosByServer: Record<string, number>;
  digest: string;
  capturedAt: string;
}

export class McpSamplingRuntime {
  private readonly provider: McpSamplingProvider;
  private readonly now: () => Date;
  private readonly records = new Map<string, McpSamplingRecord>();
  private readonly inFlight = new Map<string, Promise<McpCreateMessageResult>>();
  private readonly tokenUsageByServer = new Map<string, number>();
  private readonly costMicrosByServer = new Map<string, number>();
  private readonly globalTokenBudget: number;
  private readonly globalCostBudgetMicros: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: {
    provider: McpSamplingProvider;
    now?: () => Date;
    globalTokenBudget?: number;
    globalCostBudgetMicros?: number;
    snapshot?: McpSamplingSnapshot | null;
  }) {
    this.provider = options.provider;
    this.now = options.now ?? (() => new Date());
    this.globalTokenBudget = options.globalTokenBudget ?? 10_000_000;
    this.globalCostBudgetMicros = options.globalCostBudgetMicros ?? 100_000_000;
    if (options.snapshot) this.restore(options.snapshot);
  }

  async sample(
    context: McpSamplingContext,
    requestValue: McpCreateMessageRequest,
    signal?: AbortSignal,
  ): Promise<McpCreateMessageResult> {
    const request = cloneJson(requestValue);
    validateSampling(context, request);
    const requestDigest = sha256({ context, request });
    const samplingId = deterministicMcpId("mcp-sampling", {
      server_id: context.serverId,
      connection_id: context.connectionId,
      connection_epoch: context.connectionEpoch,
      request_id: context.requestId,
      session_id: context.sessionId,
      task_id: context.taskId,
      request_digest: requestDigest,
    }, 40);
    const committed = this.records.get(samplingId);
    if (committed?.status === "committed") {
      const cached = committed.metadata.result;
      if (cached && typeof cached === "object" && !Array.isArray(cached)) return cloneJson(cached as unknown as McpCreateMessageResult);
    }
    const existing = this.inFlight.get(samplingId);
    if (existing) return existing;
    const promise = this.performSample(samplingId, context, request, requestDigest, signal);
    this.inFlight.set(samplingId, promise);
    try {
      return await promise;
    } finally {
      this.inFlight.delete(samplingId);
    }
  }

  get(samplingId: string): McpSamplingRecord | null {
    const value = this.records.get(samplingId);
    return value ? cloneJson(value) : null;
  }

  snapshot(): McpSamplingSnapshot {
    const withoutDigest = {
      version: "zyra.mcp-sampling-runtime/v1" as const,
      revision: this.revision,
      records: [...this.records.values()].sort((left, right) => left.samplingId.localeCompare(right.samplingId)).map(cloneJson),
      tokenUsageByServer: Object.fromEntries([...this.tokenUsageByServer.entries()].sort(([left], [right]) => left.localeCompare(right))),
      costMicrosByServer: Object.fromEntries([...this.costMicrosByServer.entries()].sort(([left], [right]) => left.localeCompare(right))),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  restore(snapshot: McpSamplingSnapshot): void {
    if (snapshot.version !== "zyra.mcp-sampling-runtime/v1") throw samplingError("", "unsupported_sampling_snapshot", "unsupported sampling snapshot version");
    const { digest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== digest) throw samplingError("", "sampling_snapshot_digest_mismatch", "sampling snapshot digest mismatch");
    this.records.clear();
    this.tokenUsageByServer.clear();
    this.costMicrosByServer.clear();
    this.revision = snapshot.revision;
    for (const record of snapshot.records) {
      const restored = cloneJson(record);
      if (restored.status === "pending") {
        restored.status = "failed";
        restored.completedAt = snapshot.capturedAt;
        restored.metadata = { ...restored.metadata, restore_failure: "sampling request interrupted by restart" };
      }
      this.records.set(restored.samplingId, restored);
    }
    for (const [serverId, tokens] of Object.entries(snapshot.tokenUsageByServer)) this.tokenUsageByServer.set(serverId, tokens);
    for (const [serverId, cost] of Object.entries(snapshot.costMicrosByServer)) this.costMicrosByServer.set(serverId, cost);
  }

  private async performSample(
    samplingId: string,
    context: McpSamplingContext,
    request: McpCreateMessageRequest,
    requestDigest: string,
    signal?: AbortSignal,
  ): Promise<McpCreateMessageResult> {
    this.assertBudgets(context.serverId, request.maxTokens);
    const record: McpSamplingRecord = {
      samplingId,
      serverId: context.serverId,
      connectionId: context.connectionId,
      connectionEpoch: context.connectionEpoch,
      requestId: context.requestId,
      sessionId: context.sessionId,
      taskId: context.taskId,
      requestDigest,
      status: "pending",
      selectedModel: null,
      inputTokens: 0,
      outputTokens: 0,
      costMicros: 0,
      resultDigest: null,
      createdAt: this.timestamp(),
      completedAt: null,
      metadata: {
        policy_digest: context.policyDigest,
        workspace_root_digest: sha256(context.workspaceRoot),
      },
    };
    this.records.set(samplingId, record);
    this.revision += 1;
    try {
      const providerResponse = await this.provider({
        requestId: context.requestId,
        serverId: context.serverId,
        modelHints: request.modelPreferences?.hints.map((hint) => hint.name).filter((name): name is string => Boolean(name)) ?? [],
        messages: cloneJson(request.messages),
        systemPrompt: request.systemPrompt,
        maxTokens: Math.min(request.maxTokens, context.maximumTokens),
        temperature: request.temperature,
        stopSequences: [...request.stopSequences],
        metadata: {
          include_context: request.includeContext,
          request_metadata: request.metadata,
          context_metadata: context.metadata,
        },
      }, signal);
      if (context.allowedModels.length && !context.allowedModels.includes(providerResponse.model)) {
        throw samplingError(context.serverId, "sampling_model_denied", `sampling provider selected disallowed model ${providerResponse.model}`);
      }
      if (providerResponse.outputTokens > context.maximumTokens) {
        throw samplingError(context.serverId, "sampling_output_budget_exceeded", `sampling output ${providerResponse.outputTokens} exceeds ${context.maximumTokens}`);
      }
      const result: McpCreateMessageResult = {
        role: providerResponse.role,
        content: cloneJson(providerResponse.content),
        model: providerResponse.model,
        stopReason: providerResponse.stopReason,
        meta: {
          sampling_id: samplingId,
          input_tokens: providerResponse.inputTokens,
          output_tokens: providerResponse.outputTokens,
          cost_micros: providerResponse.costMicros,
        },
      };
      const tokens = providerResponse.inputTokens + providerResponse.outputTokens;
      this.tokenUsageByServer.set(context.serverId, (this.tokenUsageByServer.get(context.serverId) ?? 0) + tokens);
      this.costMicrosByServer.set(context.serverId, (this.costMicrosByServer.get(context.serverId) ?? 0) + providerResponse.costMicros);
      Object.assign(record, {
        status: "committed",
        selectedModel: providerResponse.model,
        inputTokens: providerResponse.inputTokens,
        outputTokens: providerResponse.outputTokens,
        costMicros: providerResponse.costMicros,
        resultDigest: sha256(result),
        completedAt: this.timestamp(),
        metadata: { ...record.metadata, provider_metadata: providerResponse.metadata, result: cloneJson(result) },
      });
      this.revision += 1;
      return result;
    } catch (error) {
      record.status = signal?.aborted ? "cancelled" : "failed";
      record.completedAt = this.timestamp();
      record.metadata = { ...record.metadata, failure: error instanceof Error ? error.message : String(error) };
      this.revision += 1;
      throw error;
    }
  }

  private assertBudgets(serverId: string, requested: number): void {
    const usedTokens = [...this.tokenUsageByServer.values()].reduce((total, value) => total + value, 0);
    if (usedTokens + requested > this.globalTokenBudget) throw samplingError(serverId, "global_sampling_token_budget_exceeded", "global sampling token budget exceeded");
    const usedCost = [...this.costMicrosByServer.values()].reduce((total, value) => total + value, 0);
    if (usedCost >= this.globalCostBudgetMicros) throw samplingError(serverId, "global_sampling_cost_budget_exceeded", "global sampling cost budget exceeded");
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function validateSampling(context: McpSamplingContext, request: McpCreateMessageRequest): void {
  if (!context.serverId || !context.connectionId || !context.requestId || !context.sessionId) throw samplingError(context.serverId, "invalid_sampling_context", "sampling context identity is incomplete");
  if (request.maxTokens <= 0 || request.maxTokens > context.maximumTokens) throw samplingError(context.serverId, "invalid_sampling_max_tokens", `sampling maxTokens ${request.maxTokens} exceeds ${context.maximumTokens}`);
  if (!context.allowServerContext && request.includeContext !== "none") throw samplingError(context.serverId, "sampling_context_denied", "server context inclusion is disabled");
}

function samplingError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-sampling", { server_id: serverId, code, message }),
    category: code.includes("budget") || code.includes("denied") ? "policy" : "capability",
    code,
    message,
    serverId,
    retryable: false,
    disposition: "replan",
  });
}
