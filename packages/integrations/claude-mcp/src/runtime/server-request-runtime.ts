import type {
  JsonObject,
  JsonRpcMessage,
  JsonRpcRequest,
  JsonValue,
} from "../contracts.ts";
import type { McpTransportAdapter } from "../connection/contracts.ts";
import { canonicalJson, cloneJson, deterministicMcpId, monotonicNow, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import {
  McpProtocolCodec,
  type McpCreateMessageRequest,
  type McpCreateMessageResult,
  type McpElicitationRequest,
  type McpElicitationResult,
  type McpRoot,
} from "../core/protocol.ts";

export interface McpServerRequestContext {
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  sessionId: string;
  taskId: string;
  workspaceRoot: string;
  transport: McpTransportAdapter;
  metadata: JsonObject;
}

export interface McpServerRequestHandlers {
  roots(context: McpServerRequestContext): Promise<McpRoot[]>;
  sample(context: McpServerRequestContext, request: McpCreateMessageRequest, signal?: AbortSignal): Promise<McpCreateMessageResult>;
  elicit(context: McpServerRequestContext, request: McpElicitationRequest, signal?: AbortSignal): Promise<McpElicitationResult>;
  log(context: McpServerRequestContext, params: JsonObject): Promise<void>;
  custom(context: McpServerRequestContext, method: string, params: JsonObject, signal?: AbortSignal): Promise<JsonValue>;
}

export interface McpServerRequestRecord {
  executionId: string;
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  requestId: string;
  method: string;
  paramsDigest: string;
  status: "pending" | "committed" | "failed" | "cancelled";
  responseDigest: string | null;
  startedAt: string;
  completedAt: string | null;
  failure: JsonObject | null;
  metadata: JsonObject;
}

export class McpServerRequestRuntime {
  private readonly handlers: McpServerRequestHandlers;
  private readonly codec = new McpProtocolCodec();
  private readonly now: () => Date;
  private readonly records = new Map<string, McpServerRequestRecord>();
  private readonly inFlight = new Map<string, AbortController>();
  private readonly maximumRecords: number;
  private lastTimestamp: string | null = null;

  constructor(options: { handlers: McpServerRequestHandlers; now?: () => Date; maximumRecords?: number }) {
    this.handlers = options.handlers;
    this.now = options.now ?? (() => new Date());
    this.maximumRecords = options.maximumRecords ?? 10_000;
  }

  async handle(context: McpServerRequestContext, message: JsonRpcMessage): Promise<boolean> {
    if (!("method" in message)) return false;
    if (!("id" in message) || message.id === undefined || message.id === null) {
      await this.handleNotification(context, message.method, message.params ?? {});
      return true;
    }
    const request = message as JsonRpcRequest;
    const requestId = String(request.id);
    const params = request.params ?? {};
    const executionId = deterministicMcpId("mcp-server-request", {
      server_id: context.serverId,
      connection_id: context.connectionId,
      connection_epoch: context.connectionEpoch,
      request_id: requestId,
      method: request.method,
      params_digest: sha256(params),
    }, 40);
    const existing = this.records.get(executionId);
    if (existing?.status === "committed") return true;
    if (this.inFlight.has(executionId)) return true;
    const controller = new AbortController();
    this.inFlight.set(executionId, controller);
    const record: McpServerRequestRecord = {
      executionId,
      serverId: context.serverId,
      connectionId: context.connectionId,
      connectionEpoch: context.connectionEpoch,
      requestId,
      method: request.method,
      paramsDigest: sha256(params),
      status: "pending",
      responseDigest: null,
      startedAt: this.timestamp(),
      completedAt: null,
      failure: null,
      metadata: cloneJson(context.metadata),
    };
    this.records.set(executionId, record);
    try {
      const boundContext: McpServerRequestContext = {
        ...context,
        metadata: {
          ...context.metadata,
          server_request_id: requestId,
          server_request_execution_id: executionId,
        },
      };
      const result = await this.dispatch(boundContext, request.method, params, controller.signal);
      const response = this.codec.success(request.id, result);
      await context.transport.notify(response, controller.signal);
      record.status = "committed";
      record.responseDigest = sha256(response);
      record.completedAt = this.timestamp();
      return true;
    } catch (error) {
      const protocolError = error instanceof McpRuntimeError ? error : null;
      const code = protocolError?.failure.json_rpc_code
        ?? (protocolError?.failure.code === "method_not_found" ? -32601 : -32000);
      const response = this.codec.error(request.id, code, error instanceof Error ? error.message : String(error), protocolError?.toJSON() ?? null);
      try {
        await context.transport.notify(response);
      } finally {
        record.status = controller.signal.aborted ? "cancelled" : "failed";
        record.completedAt = this.timestamp();
        record.responseDigest = sha256(response);
        record.failure = {
          name: error instanceof Error ? error.name : "Error",
          message: error instanceof Error ? error.message : String(error),
          code: protocolError?.code ?? "server_request_failed",
        };
      }
      return true;
    } finally {
      this.inFlight.delete(executionId);
      this.trim();
    }
  }

  cancel(serverId: string, requestId: string, reason = "cancelled_by_server"): boolean {
    const record = [...this.records.values()].find((value) => value.serverId === serverId && value.requestId === requestId && value.status === "pending");
    if (!record) return false;
    const controller = this.inFlight.get(record.executionId);
    controller?.abort(new Error(reason));
    record.status = "cancelled";
    record.completedAt = this.timestamp();
    record.failure = { code: "server_request_cancelled", reason };
    return true;
  }

  history(serverId?: string): McpServerRequestRecord[] {
    return [...this.records.values()]
      .filter((record) => !serverId || record.serverId === serverId)
      .sort((left, right) => left.startedAt.localeCompare(right.startedAt))
      .map(cloneJson);
  }

  private async dispatch(
    context: McpServerRequestContext,
    method: string,
    params: JsonObject,
    signal: AbortSignal,
  ): Promise<JsonValue> {
    if (method === "ping") return {};
    if (method === "roots/list") {
      const roots = await this.handlers.roots(context);
      return canonicalJson({ roots });
    }
    if (method === "sampling/createMessage") {
      const request = this.codec.parseSamplingRequest(params);
      return canonicalJson(await this.handlers.sample(context, request, signal));
    }
    if (method === "elicitation/create") {
      const request = this.codec.parseElicitationRequest(params);
      return canonicalJson(await this.handlers.elicit(context, request, signal));
    }
    return canonicalJson(await this.handlers.custom(context, method, params, signal));
  }

  private async handleNotification(
    context: McpServerRequestContext,
    method: string,
    params: JsonObject,
  ): Promise<void> {
    if (method === "notifications/cancelled") {
      const id = params.requestId;
      if (typeof id === "string" || typeof id === "number") this.cancel(context.serverId, String(id), typeof params.reason === "string" ? params.reason : "cancelled_by_server");
      return;
    }
    if (method === "notifications/message") {
      await this.handlers.log(context, params);
      return;
    }
    await this.handlers.custom(context, method, params);
  }

  private trim(): void {
    if (this.records.size <= this.maximumRecords) return;
    for (const [id, record] of this.records) {
      if (this.records.size <= this.maximumRecords) break;
      if (record.status !== "pending") this.records.delete(id);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}
