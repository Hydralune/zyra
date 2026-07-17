import type { JsonObject, JsonRpcMessage, JsonRpcResponse, JsonValue } from "../contracts.ts";
import type { McpServerConfigRecord } from "../config/config-store.ts";
import { McpServerPolicy, type McpPolicyOperation } from "../config/policy.ts";
import { McpCapabilityCatalog, type McpCatalogDiff, type McpCatalogServerSnapshot } from "../catalog/capability-catalog.ts";
import { McpConnectionRuntime, type McpConnectedServer } from "../connection/connection-runtime.ts";
import type { McpTransportAdapter } from "../connection/contracts.ts";
import { canonicalJson, cloneJson, deterministicMcpId, monotonicNow, sha256 } from "../core/canonical.ts";
import { McpRuntimeError, normalizeFailure, type McpFailureRecord } from "../core/failure.ts";
import {
  McpProtocolCodec,
  type McpPromptResult,
  type McpToolResult,
} from "../core/protocol.ts";
import { McpInstructionRuntime } from "../projection/instruction-runtime.ts";
import { McpPromptProjection } from "../projection/prompt-projection.ts";
import { McpResourceProjection } from "../projection/resource-projection.ts";
import { McpToolProjection } from "../projection/tool-projection.ts";
import { McpPaginationRuntime } from "./pagination-runtime.ts";
import { McpRateLimitRuntime } from "./rate-limit-runtime.ts";
import { McpRequestJournal, type McpRequestIdentity } from "./request-journal.ts";
import { McpRetryRuntime } from "./retry-runtime.ts";
import { McpSchemaRuntime } from "./schema-runtime.ts";
import type { McpServerRequestRuntime } from "./server-request-runtime.ts";
import type { McpTaskRuntime } from "./task-runtime.ts";

export interface McpClientInvocationIdentity extends McpRequestIdentity {
  workspaceRoot: string;
  interactive: boolean;
  sealedAutonomous: boolean;
}

export interface McpClientRuntimeOptions {
  policy: McpServerPolicy;
  connections: McpConnectionRuntime;
  catalog: McpCapabilityCatalog;
  journal: McpRequestJournal;
  rateLimits: McpRateLimitRuntime;
  retry: McpRetryRuntime;
  schemas: McpSchemaRuntime;
  pagination: McpPaginationRuntime;
  instructions: McpInstructionRuntime;
  serverRequests: McpServerRequestRuntime;
  tasks: McpTaskRuntime;
  now?: () => Date;
}

export interface McpClientExecution<T extends JsonValue = JsonValue> {
  executionId: string;
  serverId: string;
  operation: string;
  capabilityName: string;
  requestId: string;
  journalId: string;
  transitionId: string;
  catalogRevision: number;
  connectionEpoch: number;
  idempotent: boolean;
  idempotencyKey: string | null;
  result: T;
  resultDigest: string;
  retries: number;
  replayed: boolean;
  startedAt: string;
  completedAt: string;
  metadata: JsonObject;
}

export interface McpProjectionSnapshot {
  serverId: string;
  catalogRevision: number;
  tools: ReturnType<McpToolProjection["materialize"]>;
  resources: ReturnType<McpResourceProjection["materialize"]>;
  prompts: ReturnType<McpPromptProjection["materialize"]>;
  instructions: ReturnType<McpInstructionRuntime["compose"]>;
  digest: string;
}

export class McpClientRuntime {
  private readonly policy: McpServerPolicy;
  private readonly connections: McpConnectionRuntime;
  private readonly catalog: McpCapabilityCatalog;
  private readonly journal: McpRequestJournal;
  private readonly rateLimits: McpRateLimitRuntime;
  private readonly retry: McpRetryRuntime;
  private readonly schemas: McpSchemaRuntime;
  private readonly pagination: McpPaginationRuntime;
  private readonly instructions: McpInstructionRuntime;
  private readonly serverRequests: McpServerRequestRuntime;
  private readonly tasks: McpTaskRuntime;
  private readonly codec = new McpProtocolCodec();
  private readonly toolProjection = new McpToolProjection();
  private readonly resourceProjection = new McpResourceProjection();
  private readonly promptProjection = new McpPromptProjection();
  private readonly now: () => Date;
  private readonly configs = new Map<string, McpServerConfigRecord>();
  private readonly unsubscribers = new Map<string, () => void>();
  private lastTimestamp: string | null = null;

  constructor(options: McpClientRuntimeOptions) {
    this.policy = options.policy;
    this.connections = options.connections;
    this.catalog = options.catalog;
    this.journal = options.journal;
    this.rateLimits = options.rateLimits;
    this.retry = options.retry;
    this.schemas = options.schemas;
    this.pagination = options.pagination;
    this.instructions = options.instructions;
    this.serverRequests = options.serverRequests;
    this.tasks = options.tasks;
    this.now = options.now ?? (() => new Date());
  }

  async connect(configValue: McpServerConfigRecord, signal?: AbortSignal): Promise<McpCatalogServerSnapshot> {
    const config = cloneJson(configValue);
    this.configs.set(config.serverId, config);
    this.rateLimits.configure(config.serverId, config.rateLimit);
    const connected = await this.connections.connect(config, signal);
    this.subscribe(connected);
    const catalog = await this.refreshCatalog(config.serverId, signal);
    return catalog;
  }

  async reconnect(serverId: string, reason = "client_reconnect", signal?: AbortSignal): Promise<McpCatalogServerSnapshot> {
    const connected = await this.connections.reconnect(serverId, reason, signal);
    this.subscribe(connected);
    return this.refreshCatalog(serverId, signal);
  }

  async disconnect(serverId: string, reason = "client_disconnect"): Promise<void> {
    this.unsubscribers.get(serverId)?.();
    this.unsubscribers.delete(serverId);
    await this.connections.disconnect(serverId, reason);
    this.catalog.remove(serverId, reason);
    this.instructions.applyDelta({
      serverId,
      connectionId: "disconnected",
      connectionEpoch: Number.MAX_SAFE_INTEGER,
      catalogRevision: (this.catalog.get(serverId)?.revision ?? 0) + 1,
      instructions: null,
      metadata: { reason },
    });
  }

  async refreshCatalog(serverId: string, signal?: AbortSignal): Promise<McpCatalogServerSnapshot> {
    const connected = this.connections.requireConnected(serverId);
    const tools = await this.collectList(connected, "tools/list", "tools", (value) => value.name, signal);
    const resources = await this.collectList(connected, "resources/list", "resources", (value) => value.uri, signal);
    const templates = await this.collectList(connected, "resources/templates/list", "resourceTemplates", (value) => value.uriTemplate, signal, true);
    const prompts = await this.collectList(connected, "prompts/list", "prompts", (value) => value.name, signal);
    const prior = this.catalog.get(serverId);
    const diff = this.catalog.applySnapshot({
      serverId,
      connectionId: connected.record.connectionId,
      connectionEpoch: connected.record.epoch,
      initialize: connected.initialize,
      tools: tools as never[],
      resources: resources as never[],
      resourceTemplates: templates as never[],
      prompts: prompts as never[],
      expectedRevision: prior?.revision,
      metadata: { refreshed_by: "McpClientRuntime.refreshCatalog" },
    });
    const snapshot = this.catalog.require(serverId);
    this.connections.updateCatalogRevision(serverId, snapshot.revision);
    this.instructions.applyDelta({
      serverId,
      connectionId: snapshot.connectionId,
      connectionEpoch: snapshot.connectionEpoch,
      catalogRevision: snapshot.revision,
      instructions: snapshot.instructions,
      metadata: { catalog_digest: snapshot.digest },
    });
    void diff;
    return snapshot;
  }

  projections(serverId: string): McpProjectionSnapshot {
    const catalog = this.catalog.require(serverId);
    const tools = this.toolProjection.materialize(catalog);
    const resources = this.resourceProjection.materialize(catalog);
    const prompts = this.promptProjection.materialize(catalog);
    const instructions = this.instructions.compose([serverId]);
    const base = { serverId, catalogRevision: catalog.revision, tools, resources, prompts, instructions };
    return { ...base, digest: sha256(base) };
  }

  async callTool(
    identity: McpClientInvocationIdentity,
    name: string,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ): Promise<McpClientExecution<JsonObject>> {
    const catalog = this.catalog.require(identity.serverId);
    const tool = catalog.tools.find((value) => value.name === name);
    if (!tool) throw clientError(identity.serverId, "tool_not_found", `MCP tool ${name} was not found`);
    const validated = this.schemas.require(tool.inputSchema, argumentsValue, `arguments for ${name}`) as JsonObject;
    const idempotent = tool.annotations?.idempotentHint ?? tool.annotations?.readOnlyHint ?? false;
    const execution = await this.execute(identity, "tools/call", name, { name, arguments: validated }, idempotent, signal);
    const result = this.codec.parseToolResult(execution.result);
    if (tool.outputSchema && result.structuredContent) this.schemas.require(tool.outputSchema, result.structuredContent, `output for ${name}`);
    if (result.isError) {
      throw clientError(identity.serverId, "tool_reported_error", `MCP tool ${name} returned isError`, false, {
        content: canonicalJson(result.content),
        execution_id: execution.executionId,
      });
    }
    return { ...execution, result: canonicalJson(result) as JsonObject, resultDigest: sha256(result) };
  }

  async readResource(
    identity: McpClientInvocationIdentity,
    uri: string,
    signal?: AbortSignal,
  ): Promise<McpClientExecution<JsonObject>> {
    const catalog = this.catalog.require(identity.serverId);
    const exists = catalog.resources.some((resource) => resource.uri === uri)
      || catalog.resourceTemplates.some((template) => templateMatches(template.uriTemplate, uri));
    if (!exists) throw clientError(identity.serverId, "resource_not_found", `MCP resource ${uri} is not in catalog`);
    const execution = await this.execute(identity, "resources/read", uri, { uri }, true, signal);
    const contents = this.codec.parseResourceContents(execution.result);
    return {
      ...execution,
      result: canonicalJson({ contents }) as JsonObject,
      resultDigest: sha256(contents),
    };
  }

  async getPrompt(
    identity: McpClientInvocationIdentity,
    name: string,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ): Promise<McpClientExecution<JsonObject>> {
    const catalog = this.catalog.require(identity.serverId);
    const prompt = catalog.prompts.find((value) => value.name === name);
    if (!prompt) throw clientError(identity.serverId, "prompt_not_found", `MCP prompt ${name} was not found`);
    const known = new Set(prompt.arguments.map((argument) => argument.name));
    for (const key of Object.keys(argumentsValue)) if (!known.has(key)) throw clientError(identity.serverId, "unknown_prompt_argument", `prompt ${name} does not accept ${key}`);
    for (const argument of prompt.arguments) if (argument.required && argumentsValue[argument.name] === undefined) throw clientError(identity.serverId, "required_prompt_argument_missing", `prompt ${name} requires ${argument.name}`);
    const execution = await this.execute(identity, "prompts/get", name, { name, arguments: argumentsValue }, true, signal);
    const result = this.codec.parsePromptResult(execution.result);
    return { ...execution, result: canonicalJson(result) as JsonObject, resultDigest: sha256(result) };
  }

  private async execute(
    identityValue: McpClientInvocationIdentity,
    operation: McpPolicyOperation,
    capabilityName: string,
    params: JsonObject,
    idempotent: boolean,
    signal?: AbortSignal,
  ): Promise<McpClientExecution<JsonObject>> {
    const identity = cloneJson(identityValue);
    const config = this.requireConfig(identity.serverId);
    const connected = this.connections.requireConnected(identity.serverId);
    if (identity.connectionId !== connected.record.connectionId || identity.connectionEpoch !== connected.record.epoch) {
      throw clientError(identity.serverId, "connection_identity_mismatch", "MCP invocation belongs to another connection epoch");
    }
    const decision = this.policy.evaluate({
      serverId: identity.serverId,
      transport: config.transport.kind,
      operation,
      capabilityName,
      resourceUri: operation === "resources/read" ? capabilityName : "",
      workspaceRoot: identity.workspaceRoot,
      source: config.source,
      interactive: identity.interactive,
      sealedAutonomous: identity.sealedAutonomous,
      networkReachable: true,
      authenticated: connected.record.authRevision > 0 || config.authProviderId === null,
      config,
      metadata: { request_id: identity.requestId },
    });
    if (decision.effect !== "allow") throw new McpRuntimeError({
      failureId: decision.decisionId,
      category: "policy",
      code: decision.reasonCode,
      message: decision.reason,
      serverId: identity.serverId,
      operation,
      requestId: identity.requestId,
      retryable: false,
      disposition: "replan",
      details: { recovery_input: decision.recoveryInput },
    });
    const wireId = deterministicMcpId("mcp-json-rpc", {
      server_id: identity.serverId,
      connection_epoch: identity.connectionEpoch,
      request_id: identity.requestId,
      operation,
      params_digest: sha256(params),
    });
    const message = this.codec.request(wireId, operation, params);
    const idempotencyKey = idempotent
      ? deterministicMcpId("mcp-idempotency", { identity, operation, params }, 40)
      : null;
    // The client identity also carries policy-only fields such as
    // `interactive`, `sealedAutonomous`, and `workspaceRoot`.  Those fields
    // must never leak into the durable request identity: the journal schema
    // deliberately owns only correlation and connection-epoch data.  Passing
    // the structural superset used to make the journal validate booleans as
    // strings and rejected every projected MCP tool on the real coordinator
    // path even though isolated transports still worked.
    const journalIdentity: McpRequestIdentity = {
      runId: identity.runId,
      taskId: identity.taskId,
      sessionId: identity.sessionId,
      sessionRevision: identity.sessionRevision,
      workerRequestId: identity.workerRequestId,
      toolCallId: identity.toolCallId,
      serverId: identity.serverId,
      connectionId: identity.connectionId,
      connectionEpoch: identity.connectionEpoch,
      requestId: identity.requestId,
      // Bind the durable identity to the physical JSON-RPC operation.  The
      // coordinator-level identity uses the placeholder `dynamic` before a
      // projected capability is resolved; persisting that placeholder would
      // make recovery unable to distinguish tools/call, resources/read, and
      // prompts/get for the same correlation tuple.
      method: operation,
    };
    const prepared = this.journal.prepare({
      identity: journalIdentity,
      message,
      arguments: params,
      idempotent,
      idempotencyKey,
      metadata: { policy_decision_id: decision.decisionId, policy_digest: decision.policyDigest },
    });
    const startedAt = this.timestamp();
    const lease = await this.rateLimits.acquire(identity.serverId, operation, identity.requestId, signal);
    try {
      this.journal.markSent(prepared.journalId, prepared.transitionId, identity.connectionEpoch);
      const retryResult = await this.retry.execute({
        serverId: identity.serverId,
        requestId: identity.requestId,
        operation,
        idempotent,
        idempotencyKey,
        connectionEpoch: identity.connectionEpoch,
        policy: config.retry,
        metadata: { journal_id: prepared.journalId },
      }, async (_attempt, attemptSignal) => {
        const current = this.connections.requireConnected(identity.serverId);
        const response = await current.transport.request({
          requestId: identity.requestId,
          method: operation,
          message,
          timeoutMs: config.timeouts.requestMs,
          idempotent,
          idempotencyKey,
          authorization: null,
          headers: {},
          signal: attemptSignal,
          metadata: { journal_id: prepared.journalId, transition_id: prepared.transitionId },
        });
        if (!("result" in response.message)) {
          const error = "error" in response.message ? response.message.error : null;
          throw clientError(identity.serverId, "json_rpc_request_failed", error?.message ?? `${operation} returned no result`, Boolean(error && config.retry.retryJsonRpcCodes.includes(error.code)), {
            json_rpc_code: error?.code ?? null,
            json_rpc_data: error?.data ?? null,
          });
        }
        return response;
      }, {
        signal,
        reconnect: async (_failure, attempt) => {
          if (!idempotent && !idempotencyKey) throw clientError(identity.serverId, "unsafe_non_idempotent_reconnect", `cannot reconnect non-idempotent ${operation}`);
          const reconnected = await this.connections.reconnect(identity.serverId, `retry_attempt_${attempt}`, signal);
          return reconnected.record.epoch;
        },
      });
      const response = retryResult.value;
      const result = (response.message as JsonRpcResponse).result ?? null;
      this.journal.commit({
        journalId: prepared.journalId,
        transitionId: prepared.transitionId,
        status: "committed",
        response: response.message,
        failure: null,
        metadata: { retries: retryResult.retries, replayed: response.replayed },
      });
      const completedAt = this.timestamp();
      const base = {
        executionId: deterministicMcpId("mcp-client-execution", { transition_id: prepared.transitionId, response_digest: sha256(response.message) }, 40),
        serverId: identity.serverId,
        operation,
        capabilityName,
        requestId: identity.requestId,
        journalId: prepared.journalId,
        transitionId: prepared.transitionId,
        catalogRevision: this.catalog.require(identity.serverId).revision,
        connectionEpoch: response.connectionEpoch,
        idempotent,
        idempotencyKey,
        result: canonicalJson(result) as JsonObject,
        resultDigest: sha256(result),
        retries: retryResult.retries,
        replayed: response.replayed,
        startedAt,
        completedAt,
        metadata: { policy_decision_id: decision.decisionId, rate_lease_id: lease.leaseId },
      };
      return base;
    } catch (error) {
      const failure = normalizeFailure(error, {
        failureId: deterministicMcpId("mcp-client-failure", { transition_id: prepared.transitionId }),
        serverId: identity.serverId,
        operation,
        requestId: identity.requestId,
      });
      this.journal.commit({
        journalId: prepared.journalId,
        transitionId: prepared.transitionId,
        status: signal?.aborted ? "cancelled" : "failed",
        response: null,
        failure,
      });
      throw error;
    } finally {
      lease.release();
    }
  }

  private async collectList(
    connected: McpConnectedServer,
    method: string,
    property: string,
    key: (value: any) => string,
    signal?: AbortSignal,
    optional = false,
  ): Promise<any[]> {
    try {
      const result = await this.pagination.collect<JsonValue>(method, async (cursor, page) => {
        const requestId = deterministicMcpId("mcp-list-page", {
          server_id: connected.record.serverId,
          connection_epoch: connected.record.epoch,
          method,
          cursor,
          page,
        });
        const response = await connected.transport.request({
          requestId,
          method,
          message: this.codec.request(requestId, method, cursor ? { cursor } : {}),
          timeoutMs: this.requireConfig(connected.record.serverId).timeouts.requestMs,
          idempotent: true,
          idempotencyKey: requestId,
          authorization: null,
          headers: {},
          signal,
          metadata: { catalog_refresh: true, page },
        });
        if (!("result" in response.message)) throw clientError(connected.record.serverId, "catalog_list_failed", `${method} returned no result`);
        const object = response.message.result && typeof response.message.result === "object" && !Array.isArray(response.message.result)
          ? response.message.result as JsonObject
          : {};
        const values = Array.isArray(object[property]) ? object[property] as JsonValue[] : [];
        return {
          items: values.map((value) => parseCatalogItem(this.codec, method, property, value)),
          nextCursor: typeof object.nextCursor === "string" ? object.nextCursor : null,
          metadata: { page },
        };
      }, { itemKey: key, signal });
      return result.items;
    } catch (error) {
      if (optional && error instanceof McpRuntimeError && /method|not found|32601/i.test(error.message)) return [];
      throw error;
    }
  }

  private subscribe(connected: McpConnectedServer): void {
    this.unsubscribers.get(connected.record.serverId)?.();
    const unsubscribe = connected.transport.onMessage(async (message) => {
      if ("method" in message && !("id" in message)) {
        const method = message.method;
        if (method === "notifications/tools/list_changed" || method === "notifications/resources/list_changed" || method === "notifications/prompts/list_changed") {
          this.catalog.applyNotification({
            serverId: connected.record.serverId,
            connectionId: connected.record.connectionId,
            connectionEpoch: connected.record.epoch,
            method,
            params: message.params ?? {},
          });
          await this.refreshCatalog(connected.record.serverId);
          return;
        }
      }
      await this.serverRequests.handle({
        serverId: connected.record.serverId,
        connectionId: connected.record.connectionId,
        connectionEpoch: connected.record.epoch,
        sessionId: "mcp-session",
        taskId: "mcp-server-request",
        workspaceRoot: "",
        transport: connected.transport,
        metadata: {},
      }, message);
    });
    this.unsubscribers.set(connected.record.serverId, unsubscribe);
  }

  private requireConfig(serverId: string): McpServerConfigRecord {
    const config = this.configs.get(serverId);
    if (!config) throw clientError(serverId, "client_config_not_found", `MCP client config ${serverId} was not found`);
    return config;
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function parseCatalogItem(codec: McpProtocolCodec, method: string, property: string, value: JsonValue): JsonValue {
  if (method === "tools/list") return canonicalJson(codec.parseToolsPage({ tools: [value] }).items[0]);
  if (method === "resources/list") return canonicalJson(codec.parseResourcesPage({ resources: [value] }).items[0]);
  if (method === "resources/templates/list") return canonicalJson(codec.parseResourceTemplatesPage({ resourceTemplates: [value] }).items[0]);
  if (method === "prompts/list") return canonicalJson(codec.parsePromptsPage({ prompts: [value] }).items[0]);
  throw new Error(`unsupported catalog property ${property}`);
}

function templateMatches(template: string, uri: string): boolean {
  const pattern = template.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\\\{[^}]+\\\}/g, "[^?#]+");
  return new RegExp(`^${pattern}$`).test(uri);
}

function clientError(
  serverId: string,
  code: string,
  message: string,
  retryable = false,
  details: JsonObject = {},
): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-client", { server_id: serverId, code, message }),
    category: code.includes("not_found") ? "capability" : code.includes("identity") ? "conflict" : "remote",
    code,
    message,
    serverId,
    retryable,
    disposition: retryable ? "retry_same_connection" : "terminal",
    details,
  });
}
