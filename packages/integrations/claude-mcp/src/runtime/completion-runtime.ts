import type { JsonObject, JsonValue } from "../contracts.ts";
import type { McpTransportAdapter } from "../connection/contracts.ts";
import { canonicalJson, cloneJson, deterministicMcpId, monotonicNow, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import { McpProtocolCodec } from "../core/protocol.ts";

export interface McpCompletionRequest {
  ref: { type: "ref/prompt" | "ref/resource"; name?: string; uri?: string };
  argument: { name: string; value: string };
  context?: { arguments?: Record<string, string> };
}

export interface McpCompletionResult {
  values: string[];
  total: number | null;
  hasMore: boolean;
}

export interface McpCompletionRecord {
  completionId: string;
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  requestId: string;
  request: McpCompletionRequest;
  requestDigest: string;
  result: McpCompletionResult | null;
  resultDigest: string | null;
  status: "pending" | "committed" | "failed" | "cancelled";
  createdAt: string;
  completedAt: string | null;
  failure: JsonObject | null;
  metadata: JsonObject;
}

export interface McpCompletionSnapshot {
  version: "zyra.mcp-completion-runtime/v1";
  revision: number;
  records: McpCompletionRecord[];
  digest: string;
  capturedAt: string;
}

export class McpCompletionRuntime {
  private readonly codec = new McpProtocolCodec();
  private readonly records = new Map<string, McpCompletionRecord>();
  private readonly inFlight = new Map<string, Promise<McpCompletionResult>>();
  private readonly now: () => Date;
  private readonly maximumRecords: number;
  private readonly maximumValues: number;
  private readonly maximumValueLength: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumRecords?: number; maximumValues?: number; maximumValueLength?: number; snapshot?: McpCompletionSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumRecords = options.maximumRecords ?? 10_000;
    this.maximumValues = options.maximumValues ?? 1_000;
    this.maximumValueLength = options.maximumValueLength ?? 16_384;
    if (options.snapshot) this.restore(options.snapshot);
  }

  async complete(input: {
    serverId: string;
    connectionId: string;
    connectionEpoch: number;
    requestId: string;
    request: McpCompletionRequest;
    transport: McpTransportAdapter;
    signal?: AbortSignal;
    metadata?: JsonObject;
  }): Promise<McpCompletionResult> {
    const request = normalize(input.request);
    const requestDigest = sha256(request);
    const completionId = deterministicMcpId("mcp-completion", {
      server_id: input.serverId,
      connection_id: input.connectionId,
      connection_epoch: input.connectionEpoch,
      request_id: input.requestId,
      request_digest: requestDigest,
    }, 40);
    const existing = this.records.get(completionId);
    if (existing?.status === "committed" && existing.result) return cloneJson(existing.result);
    const pending = this.inFlight.get(completionId);
    if (pending) return pending;
    const promise = this.perform(completionId, input, request, requestDigest);
    this.inFlight.set(completionId, promise);
    try {
      return await promise;
    } finally {
      this.inFlight.delete(completionId);
    }
  }

  get(completionId: string): McpCompletionRecord | null {
    const record = this.records.get(completionId);
    return record ? cloneJson(record) : null;
  }

  list(serverId?: string): McpCompletionRecord[] {
    return [...this.records.values()].filter((record) => !serverId || record.serverId === serverId).sort((left, right) => left.createdAt.localeCompare(right.createdAt)).map(cloneJson);
  }

  snapshot(): McpCompletionSnapshot {
    const withoutDigest = {
      version: "zyra.mcp-completion-runtime/v1" as const,
      revision: this.revision,
      records: this.list(),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  restore(snapshot: McpCompletionSnapshot): void {
    if (snapshot.version !== "zyra.mcp-completion-runtime/v1") throw completionError("", "unsupported_completion_snapshot", "unsupported completion snapshot version");
    const { digest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== digest) throw completionError("", "completion_snapshot_digest_mismatch", "completion snapshot digest mismatch");
    this.records.clear();
    this.revision = snapshot.revision;
    for (const value of snapshot.records) {
      const record = cloneJson(value);
      if (record.status === "pending") {
        record.status = "failed";
        record.completedAt = snapshot.capturedAt;
        record.failure = { code: "completion_restart_interrupted", message: "completion request interrupted by restart" };
      }
      this.records.set(record.completionId, record);
    }
  }

  private async perform(
    completionId: string,
    input: Parameters<McpCompletionRuntime["complete"]>[0],
    request: McpCompletionRequest,
    requestDigest: string,
  ): Promise<McpCompletionResult> {
    const record: McpCompletionRecord = {
      completionId,
      serverId: input.serverId,
      connectionId: input.connectionId,
      connectionEpoch: input.connectionEpoch,
      requestId: input.requestId,
      request,
      requestDigest,
      result: null,
      resultDigest: null,
      status: "pending",
      createdAt: this.timestamp(),
      completedAt: null,
      failure: null,
      metadata: cloneJson(input.metadata ?? {}),
    };
    this.records.set(completionId, record);
    this.revision += 1;
    try {
      const response = await input.transport.request({
        requestId: input.requestId,
        method: "completion/complete",
        message: this.codec.request(input.requestId, "completion/complete", request as unknown as JsonObject),
        timeoutMs: 30_000,
        idempotent: true,
        idempotencyKey: completionId,
        authorization: null,
        headers: {},
        signal: input.signal,
        metadata: { completion_id: completionId },
      });
      if (!("result" in response.message)) {
        const error = "error" in response.message ? response.message.error : null;
        throw completionError(input.serverId, "completion_remote_rejected", error?.message ?? "completion/complete returned no result");
      }
      const object = response.message.result && typeof response.message.result === "object" && !Array.isArray(response.message.result)
        ? response.message.result as JsonObject
        : {};
      const completion = object.completion && typeof object.completion === "object" && !Array.isArray(object.completion)
        ? object.completion as JsonObject
        : object;
      const rawValues = Array.isArray(completion.values) ? completion.values : [];
      if (rawValues.length > this.maximumValues) throw completionError(input.serverId, "completion_values_oversized", `completion returned ${rawValues.length} values`);
      const values = rawValues.map((value) => {
        if (typeof value !== "string") throw completionError(input.serverId, "completion_value_invalid", "completion value must be string");
        if (value.length > this.maximumValueLength) throw completionError(input.serverId, "completion_value_oversized", `completion value exceeds ${this.maximumValueLength}`);
        return value;
      });
      const result: McpCompletionResult = {
        values,
        total: typeof completion.total === "number" && Number.isSafeInteger(completion.total) && completion.total >= 0 ? completion.total : null,
        hasMore: completion.hasMore === true,
      };
      record.status = "committed";
      record.result = result;
      record.resultDigest = sha256(result);
      record.completedAt = this.timestamp();
      this.revision += 1;
      this.trim();
      return cloneJson(result);
    } catch (error) {
      record.status = input.signal?.aborted ? "cancelled" : "failed";
      record.completedAt = this.timestamp();
      record.failure = { name: error instanceof Error ? error.name : "Error", message: error instanceof Error ? error.message : String(error) };
      this.revision += 1;
      this.trim();
      throw error;
    }
  }

  private trim(): void {
    while (this.records.size > this.maximumRecords) {
      const first = this.records.keys().next().value as string | undefined;
      if (!first) break;
      if (this.inFlight.has(first)) break;
      this.records.delete(first);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function normalize(value: McpCompletionRequest): McpCompletionRequest {
  const request = cloneJson(value);
  if (request.ref.type === "ref/prompt" && !request.ref.name) throw completionError("", "completion_prompt_name_missing", "prompt completion requires ref.name");
  if (request.ref.type === "ref/resource" && !request.ref.uri) throw completionError("", "completion_resource_uri_missing", "resource completion requires ref.uri");
  if (!request.argument.name || typeof request.argument.value !== "string") throw completionError("", "completion_argument_invalid", "completion argument name/value are required");
  const args = request.context?.arguments ?? {};
  for (const [key, item] of Object.entries(args)) if (!key || typeof item !== "string") throw completionError("", "completion_context_invalid", "completion context arguments must be strings");
  return canonicalJson(request) as unknown as McpCompletionRequest;
}

function completionError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-completion", { server_id: serverId, code, message }),
    category: code.includes("remote") ? "remote" : "protocol",
    code,
    message,
    serverId,
    retryable: false,
    disposition: "terminal",
  });
}
