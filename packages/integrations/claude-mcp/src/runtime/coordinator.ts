import type { JsonObject, JsonValue } from "../contracts.ts";
import type { McpProcessFactory, McpFetch } from "../connection/contracts.ts";
import { McpOAuthRuntime, type McpOAuthCallbackInput, type McpOAuthChallenge, type McpOAuthProviderConfig, type McpOAuthSnapshot, type McpOAuthTokenSet } from "../auth/oauth-runtime.ts";
import { InMemoryMcpTokenVault, type McpTokenVaultAdapter } from "../auth/token-vault-port.ts";
import { McpCapabilityCatalog, type McpCapabilityCatalogSnapshot } from "../catalog/capability-catalog.ts";
import { McpConfigStore, type McpConfigLayer, type McpConfigMergeResult, type McpConfigSnapshot, type McpServerConfigRecord } from "../config/config-store.ts";
import { McpServerPolicy, type McpPolicyRule, type McpPolicySnapshot } from "../config/policy.ts";
import {
  assertMcpSourceRuntimeEnabled,
  McpConnectionRuntime,
} from "../connection/connection-runtime.ts";
import type { McpConnectionSnapshot } from "../connection/contracts.ts";
import { canonicalJson, cloneJson, deterministicMcpId, monotonicNow, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import type { McpCreateMessageRequest, McpCreateMessageResult, McpElicitationRequest, McpElicitationResult } from "../core/protocol.ts";
import { McpInstructionRuntime, type McpInstructionSnapshot } from "../projection/instruction-runtime.ts";
import type { McpProjectedTool } from "../projection/tool-projection.ts";
import { McpCancellationRuntime, type McpCancellationSnapshot } from "./cancellation-runtime.ts";
import { McpAuditRuntime, type McpAuditSnapshot } from "./audit-runtime.ts";
import { McpClientRuntime, type McpClientExecution, type McpClientInvocationIdentity } from "./client-runtime.ts";
import { McpCompletionRuntime, type McpCompletionRequest, type McpCompletionSnapshot } from "./completion-runtime.ts";
import { McpElicitationRuntime, type McpElicitationSnapshot } from "./elicitation-runtime.ts";
import { McpLoggingRuntime, type McpLoggingSnapshot } from "./logging-runtime.ts";
import { McpPaginationRuntime } from "./pagination-runtime.ts";
import { McpProgressRuntime, type McpProgressSnapshot } from "./progress-runtime.ts";
import { McpRateLimitRuntime } from "./rate-limit-runtime.ts";
import { McpRequestJournal, type McpRequestJournalSnapshot } from "./request-journal.ts";
import { McpRetryRuntime } from "./retry-runtime.ts";
import { McpRootRuntime, type McpRootSnapshot } from "./root-runtime.ts";
import { McpSamplingRuntime, type McpSamplingProvider, type McpSamplingSnapshot } from "./sampling-runtime.ts";
import { McpSchemaRuntime } from "./schema-runtime.ts";
import { McpServerRequestRuntime } from "./server-request-runtime.ts";
import { McpSessionRuntime, type McpSessionSnapshot } from "./session-runtime.ts";
import { McpSubscriptionRuntime, type McpSubscriptionSnapshot } from "./subscription-runtime.ts";
import { McpTaskRuntime, type McpTaskSnapshot } from "./task-runtime.ts";

export interface McpCoordinatorIdentity {
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  workspaceRoot: string;
  interactive: boolean;
  sealedAutonomous: boolean;
}

export interface McpCoordinatorToolSpec {
  name: string;
  purpose: string;
  source: "typescript-mcp";
  input_schema: JsonObject;
  output_schema: JsonObject;
  metadata: Record<string, string>;
  execution_provenance: JsonObject;
}

export interface McpCoordinatorExecution {
  summary: string;
  output: JsonObject;
  metadata: Record<string, string>;
}

export interface McpElicitationPortInput {
  record: ReturnType<McpElicitationRuntime["request"]>;
  request: McpElicitationRequest;
  identity: McpCoordinatorIdentity;
  signal?: AbortSignal;
}

export type McpElicitationPort = (input: McpElicitationPortInput) => Promise<McpElicitationResult>;

export interface McpCoordinatorSnapshot {
  version: "zyra.mcp-runtime-coordinator/v1";
  identity: {
    sessionId: string;
    workspaceRoot: string;
    interactive: boolean;
    sealedAutonomous: boolean;
  };
  opened: boolean;
  restoredBeforeBootstrap: boolean;
  requestSequence: number;
  config: McpConfigSnapshot;
  policy: McpPolicySnapshot;
  oauth: McpOAuthSnapshot;
  connections: McpConnectionSnapshot;
  catalog: McpCapabilityCatalogSnapshot;
  instructions: McpInstructionSnapshot;
  journal: McpRequestJournalSnapshot;
  sampling: McpSamplingSnapshot;
  elicitation: McpElicitationSnapshot;
  tasks: McpTaskSnapshot;
  subscriptions: McpSubscriptionSnapshot;
  roots: McpRootSnapshot;
  progress: McpProgressSnapshot;
  cancellation: McpCancellationSnapshot;
  logging: McpLoggingSnapshot;
  completion: McpCompletionSnapshot;
  session: McpSessionSnapshot;
  audit: McpAuditSnapshot;
  digest: string;
  capturedAt: string;
}

export interface McpRuntimeCoordinatorOptions {
  sessionId: string;
  workspaceRoot: string;
  interactive: boolean;
  sealedAutonomous: boolean;
  tokenVault?: McpTokenVaultAdapter;
  samplingProvider?: McpSamplingProvider;
  elicitationPort?: McpElicitationPort;
  customServerRequest?: (serverId: string, method: string, params: JsonObject, signal?: AbortSignal) => Promise<JsonValue>;
  processFactory?: McpProcessFactory;
  fetch?: McpFetch;
  now?: () => Date;
  epoch?: number;
  snapshot?: McpCoordinatorSnapshot | null;
}

const builtinSpecs: McpCoordinatorToolSpec[] = [
  spec("mcp_list_resources", "List TypeScript-owned MCP resources", {}, "read"),
  spec("mcp_read_resource", "Read a TypeScript-owned MCP resource", { server_id: { type: "string" }, uri: { type: "string" } }, "read", ["server_id", "uri"]),
  spec("mcp_subscribe_resource", "Subscribe to MCP resource updates", { server_id: { type: "string" }, uri: { type: "string" } }, "execute", ["server_id", "uri"]),
  spec("mcp_unsubscribe_resource", "Unsubscribe from MCP resource updates", { subscription_id: { type: "string" }, server_id: { type: "string" } }, "execute", ["subscription_id", "server_id"]),
  spec("mcp_list_prompts", "List TypeScript-owned MCP prompts", {}, "read"),
  spec("mcp_get_prompt", "Resolve a TypeScript-owned MCP prompt", { server_id: { type: "string" }, name: { type: "string" }, arguments: { type: "object" } }, "read", ["server_id", "name"]),
  spec("mcp_list_tasks", "List durable MCP tasks", { server_id: { type: "string" } }, "read"),
  spec("mcp_poll_task", "Poll a durable MCP task", { server_id: { type: "string" }, task_id: { type: "string" } }, "read", ["server_id", "task_id"]),
  spec("mcp_task_result", "Read a terminal MCP task result", { server_id: { type: "string" }, task_id: { type: "string" } }, "read", ["server_id", "task_id"]),
  spec("mcp_cancel_task", "Cancel a durable MCP task", { server_id: { type: "string" }, task_id: { type: "string" }, reason: { type: "string" } }, "execute", ["server_id", "task_id"]),
];

export class McpRuntimeCoordinator {
  readonly config: McpConfigStore;
  readonly policy: McpServerPolicy;
  readonly oauth: McpOAuthRuntime;
  readonly connections: McpConnectionRuntime;
  readonly catalog: McpCapabilityCatalog;
  readonly instructions: McpInstructionRuntime;
  readonly journal: McpRequestJournal;
  readonly sampling: McpSamplingRuntime;
  readonly elicitation: McpElicitationRuntime;
  readonly tasks: McpTaskRuntime;
  readonly subscriptions: McpSubscriptionRuntime;
  readonly roots: McpRootRuntime;
  readonly progress: McpProgressRuntime;
  readonly cancellation: McpCancellationRuntime;
  readonly logging: McpLoggingRuntime;
  readonly completion: McpCompletionRuntime;
  readonly session: McpSessionRuntime;
  readonly audit: McpAuditRuntime;
  readonly client: McpClientRuntime;
  readonly serverRequests: McpServerRequestRuntime;
  private readonly sessionId: string;
  private readonly workspaceRoot: string;
  private readonly interactive: boolean;
  private readonly sealedAutonomous: boolean;
  private readonly now: () => Date;
  private readonly elicitationPort: McpElicitationPort | null;
  private readonly customServerRequest: McpRuntimeCoordinatorOptions["customServerRequest"];
  private readonly projections = new Map<string, McpProjectedTool>();
  private opened = false;
  private restoredBeforeBootstrap = false;
  private requestSequence = 0;
  private lastTimestamp: string | null = null;

  assertSourceRuntimeEnabled(): void {
    assertMcpSourceRuntimeEnabled();
  }

  constructor(options: McpRuntimeCoordinatorOptions) {
    if (!options.sessionId || !options.workspaceRoot) throw coordinatorError("coordinator_identity_incomplete", "MCP coordinator requires session and workspace identity");
    this.sessionId = options.sessionId;
    this.workspaceRoot = options.workspaceRoot;
    this.interactive = options.interactive;
    this.sealedAutonomous = options.sealedAutonomous;
    this.now = options.now ?? (() => new Date());
    this.elicitationPort = options.elicitationPort ?? null;
    this.customServerRequest = options.customServerRequest;
    const snapshot = options.snapshot ?? null;
    this.config = new McpConfigStore(snapshot?.config ?? null);
    this.policy = new McpServerPolicy(snapshot?.policy ?? null);
    const vault = options.tokenVault ?? new InMemoryMcpTokenVault({ now: this.now });
    this.oauth = new McpOAuthRuntime({ vault, now: this.now, snapshot: snapshot?.oauth ?? null });
    this.catalog = new McpCapabilityCatalog({ now: this.now, snapshot: snapshot?.catalog ?? null });
    this.instructions = new McpInstructionRuntime({ now: this.now, snapshot: snapshot?.instructions ?? null });
    this.journal = new McpRequestJournal({ now: this.now, snapshot: snapshot?.journal ?? null });
    this.sampling = new McpSamplingRuntime({ provider: options.samplingProvider ?? rejectSamplingProvider, now: this.now, snapshot: snapshot?.sampling ?? null });
    this.elicitation = new McpElicitationRuntime({ now: this.now, allowUrlMode: this.interactive, snapshot: snapshot?.elicitation ?? null });
    this.tasks = new McpTaskRuntime({ now: this.now, snapshot: snapshot?.tasks ?? null });
    this.subscriptions = new McpSubscriptionRuntime({ now: this.now, snapshot: snapshot?.subscriptions ?? null });
    this.roots = new McpRootRuntime({ workspaceRoot: this.workspaceRoot, now: this.now, snapshot: snapshot?.roots ?? null });
    this.progress = new McpProgressRuntime({ now: this.now, snapshot: snapshot?.progress ?? null });
    this.cancellation = new McpCancellationRuntime({ now: this.now, snapshot: snapshot?.cancellation ?? null });
    this.logging = new McpLoggingRuntime({ now: this.now, snapshot: snapshot?.logging ?? null });
    this.completion = new McpCompletionRuntime({ now: this.now, snapshot: snapshot?.completion ?? null });
    this.audit = new McpAuditRuntime({ now: this.now, snapshot: snapshot?.audit ?? null });
    this.session = new McpSessionRuntime({
      identity: {
        sessionId: this.sessionId,
        workspaceRoot: this.workspaceRoot,
        epoch: options.epoch ?? snapshot?.session.identity.epoch ?? 0,
      },
      now: this.now,
      snapshot: snapshot?.session ?? null,
    });
    if (!snapshot) {
      this.roots.replace([{ path: ".", name: "workspace", source: "workspace" }], 0, { bootstrap: true });
    }
    this.serverRequests = new McpServerRequestRuntime({
      now: this.now,
      handlers: {
        roots: async (context) => this.roots.list({ serverAllowedRoots: this.config.get(context.serverId)?.allowedRoots }),
        sample: async (context, request, signal) => this.sampleServerRequest(context.serverId, context.connectionId, context.connectionEpoch, context.taskId, context.metadata, request, signal),
        elicit: async (context, request, signal) => this.elicitServerRequest(context.serverId, context.connectionId, context.connectionEpoch, context.taskId, context.metadata, request, signal),
        log: async (context, params) => { await this.logging.append({ serverId: context.serverId, connectionId: context.connectionId, connectionEpoch: context.connectionEpoch, params, metadata: context.metadata }); },
        custom: async (context, method, params, signal) => this.handleServerMessage(context.serverId, context.connectionId, context.connectionEpoch, method, params, signal),
      },
    });
    this.connections = new McpConnectionRuntime({
      policy: this.policy,
      sessionId: this.sessionId,
      workspaceRoot: this.workspaceRoot,
      interactive: this.interactive,
      sealedAutonomous: this.sealedAutonomous,
      processFactory: options.processFactory,
      fetch: options.fetch,
      credentialResolver: async (_serverId, handle) => (await vault.read(handle))?.value ?? null,
      authorizationProvider: async (serverId) => {
        const providerId = this.config.get(serverId)?.authProviderId;
        return providerId ? this.oauth.authorization(providerId) : null;
      },
      authenticationChallenge: async (serverId, challenge) => {
        const providerId = this.config.get(serverId)?.authProviderId;
        if (!providerId) return null;
        this.oauth.challenge({
          serverId,
          providerId,
          sessionId: this.sessionId,
          requestId: this.nextRequestId("oauth-challenge", serverId),
          challenge,
          returnTo: null,
        });
        return null;
      },
      now: this.now,
      snapshot: snapshot?.connections ?? null,
    });
    const rateLimits = new McpRateLimitRuntime({ now: this.now });
    const retry = new McpRetryRuntime({ now: this.now });
    const schemas = new McpSchemaRuntime();
    const pagination = new McpPaginationRuntime();
    this.client = new McpClientRuntime({
      policy: this.policy,
      connections: this.connections,
      catalog: this.catalog,
      journal: this.journal,
      rateLimits,
      retry,
      schemas,
      pagination,
      instructions: this.instructions,
      serverRequests: this.serverRequests,
      tasks: this.tasks,
      now: this.now,
    });
    this.requestSequence = snapshot?.requestSequence ?? 0;
    this.opened = false;
    this.restoredBeforeBootstrap = Boolean(snapshot);
    this.catalog.onEvent(() => this.rebuildProjections());
    this.rebuildProjections();
  }

  mergeConfig(layer: McpConfigLayer | JsonObject, expectedRevision = this.config.revision): McpConfigMergeResult {
    const result = this.config.merge(layer, expectedRevision);
    this.rebuildPolicy();
    return result;
  }

  registerOAuthProvider(config: McpOAuthProviderConfig): McpOAuthProviderConfig {
    return this.oauth.register(config);
  }

  oauthChallenge(serverId: string, requestId: string, challenge: string, returnTo: string | null = null): McpOAuthChallenge {
    const providerId = this.config.get(serverId)?.authProviderId;
    if (!providerId) throw coordinatorError("oauth_provider_missing", `MCP server ${serverId} has no OAuth provider`);
    return this.oauth.challenge({ serverId, providerId, sessionId: this.sessionId, requestId, challenge, returnTo });
  }

  async oauthCallback(input: McpOAuthCallbackInput, signal?: AbortSignal): Promise<McpOAuthTokenSet> {
    const token = await this.oauth.callback(input, signal);
    const connection = this.connections.get(token.serverId);
    if (connection) this.connections.updateAuthRevision(token.serverId, token.revision);
    return token;
  }

  async open(signal?: AbortSignal): Promise<void> {
    if (this.opened) return;
    if (this.config.list().length && this.policy.revision === 0) this.rebuildPolicy();
    for (const config of this.config.list({ enabledOnly: true })) await this.client.connect(config, signal);
    this.rebuildProjections();
    this.synchronizeSession("open");
    const recovery = this.session.planRecovery(
      this.journal.classifyRestore(),
      this.journal.snapshot(),
      { restored_before_bootstrap: this.restoredBeforeBootstrap },
    );
    if (recovery.length) {
      this.audit.append({
        kind: "recovery_planned",
        sessionId: this.sessionId,
        operation: "restore",
        outcome: "observation",
        stateDigest: sha256(recovery),
        details: { recovery_actions: canonicalJson(recovery) },
      });
    }
    this.opened = true;
  }

  async reload(signal?: AbortSignal): Promise<void> {
    for (const config of this.config.list()) {
      if (!config.enabled) {
        if (this.connections.get(config.serverId)) await this.client.disconnect(config.serverId, "config_disabled");
        continue;
      }
      const connected = this.connections.get(config.serverId);
      if (!connected) await this.client.connect(config, signal);
      else if (connected.configDigest !== config.configDigest) {
        await this.client.disconnect(config.serverId, "config_changed");
        await this.client.connect(config, signal);
      }
    }
    this.rebuildProjections();
    this.synchronizeSession("reload");
  }

  owns(toolName: string): boolean {
    return this.projections.has(toolName) || builtinSpecs.some((value) => value.name === toolName);
  }

  toolSpecs(): McpCoordinatorToolSpec[] {
    const projected = [...this.projections.values()].map((tool): McpCoordinatorToolSpec => ({
      name: tool.name,
      purpose: tool.description || `Invoke ${tool.originalName} on MCP server ${tool.serverId}`,
      source: "typescript-mcp",
      input_schema: cloneJson(tool.inputSchema),
      output_schema: cloneJson(tool.outputSchema ?? { type: "object" }),
      metadata: {
        access_mode: tool.readOnly ? "read" : "remote_execute",
        canonical_runtime_owner: "typescript",
        mcp_server_id: tool.serverId,
        mcp_tool_name: tool.originalName,
        catalog_revision: String(tool.catalogRevision),
        idempotent: String(tool.idempotent),
      },
      execution_provenance: {
        namespace: "mcp",
        server_id: tool.serverId,
        version: "2025-06-18",
        schema_digest: tool.schemaDigest,
        source: "zyra-e02-mcp-coordinator",
      },
    }));
    return [...projected, ...builtinSpecs].sort((left, right) => left.name.localeCompare(right.name));
  }

  async execute(toolName: string, argumentsValue: JsonObject, identityValue: Partial<McpCoordinatorIdentity> = {}, signal?: AbortSignal): Promise<McpCoordinatorExecution> {
    if (!this.opened) throw coordinatorError("mcp_coordinator_not_open", "MCP coordinator must be opened before execution");
    const identity = this.identity(identityValue);
    const projection = this.projections.get(toolName);
    const serverId = projection?.serverId ?? optionalString(argumentsValue, "server_id");
    if (!serverId) {
      return this.executeBound(toolName, argumentsValue, identity, signal);
    }
    this.synchronizeSession("before_execute");
    const connection = this.connections.requireConnected(serverId).record;
    const idempotent = projection?.idempotent ?? builtinIdempotent(toolName);
    const operation = projection
      ? `tools/call:${projection.originalName}`
      : toolName;
    const lease = this.session.acquire({
      serverId,
      connectionId: connection.connectionId,
      connectionEpoch: connection.epoch,
      taskId: identity.taskId,
      workerRequestId: identity.workerRequestId,
      toolCallId: identity.toolCallId,
      operation,
      arguments: argumentsValue,
      idempotent,
      metadata: {
        projected_tool_name: toolName,
        catalog_revision: projection?.catalogRevision ?? connection.catalogRevision,
      },
    });
    const requestId = this.nextRequestId("session-execute", serverId);
    this.audit.requestStarted({
      serverId,
      sessionId: identity.sessionId,
      taskId: identity.taskId,
      workerRequestId: identity.workerRequestId,
      toolCallId: identity.toolCallId,
      requestId,
      connectionId: connection.connectionId,
      connectionEpoch: connection.epoch,
      operation,
      arguments: argumentsValue,
      metadata: {
        lease_id: lease.leaseId,
        idempotent,
      },
    });
    try {
      const result = await this.executeBound(toolName, argumentsValue, identity, signal);
      this.session.settle(lease.leaseId, {
        status: "completed",
        result: result.output,
        metadata: { audit_request_id: requestId },
      });
      this.audit.requestSettled({
        serverId,
        sessionId: identity.sessionId,
        taskId: identity.taskId,
        workerRequestId: identity.workerRequestId,
        toolCallId: identity.toolCallId,
        requestId,
        connectionId: connection.connectionId,
        connectionEpoch: connection.epoch,
        operation,
        output: result.output,
        metadata: {
          lease_id: lease.leaseId,
          result_metadata: result.metadata,
        },
      });
      this.synchronizeSession("after_execute");
      return result;
    } catch (error) {
      const failureCode = errorCode(error);
      const cancelled = signal?.aborted === true;
      const indeterminate = !idempotent
        && failureCode !== "mcp_tool_not_owned"
        && failureCode !== "schema_validation_failed";
      this.session.settle(lease.leaseId, {
        status: cancelled
          ? "cancelled"
          : indeterminate
            ? "indeterminate"
            : "failed",
        failureCode,
        metadata: {
          audit_request_id: requestId,
          error_message: error instanceof Error ? error.message : String(error),
        },
      });
      this.audit.requestSettled({
        kind: cancelled ? "request_cancelled" : "request_failed",
        serverId,
        sessionId: identity.sessionId,
        taskId: identity.taskId,
        workerRequestId: identity.workerRequestId,
        toolCallId: identity.toolCallId,
        requestId,
        connectionId: connection.connectionId,
        connectionEpoch: connection.epoch,
        operation,
        failure: {
          code: failureCode,
          message: error instanceof Error ? error.message : String(error),
          indeterminate,
        },
        metadata: { lease_id: lease.leaseId },
      });
      this.synchronizeSession("after_execute_failure");
      throw error;
    }
  }

  private async executeBound(toolName: string, argumentsValue: JsonObject, identity: McpCoordinatorIdentity, signal?: AbortSignal): Promise<McpCoordinatorExecution> {
    const projection = this.projections.get(toolName);
    if (projection) {
      const result = await this.client.callTool(this.clientIdentity(identity, projection.serverId), projection.originalName, argumentsValue, signal);
      return execution(`MCP tool ${projection.serverId}/${projection.originalName} completed`, result.result, projection.serverId, result);
    }
    if (toolName === "mcp_list_resources") return this.listResources(argumentsValue);
    if (toolName === "mcp_read_resource") {
      const serverId = requiredString(argumentsValue, "server_id");
      const uri = requiredString(argumentsValue, "uri");
      const result = await this.client.readResource(this.clientIdentity(identity, serverId), uri, signal);
      return execution(`Read MCP resource ${uri}`, result.result, serverId, result);
    }
    if (toolName === "mcp_subscribe_resource") {
      const serverId = requiredString(argumentsValue, "server_id");
      const connection = this.connections.requireConnected(serverId);
      const record = await this.subscriptions.subscribe({
        serverId,
        connectionId: connection.record.connectionId,
        connectionEpoch: connection.record.epoch,
        sessionId: identity.sessionId,
        taskId: identity.taskId,
        uri: requiredString(argumentsValue, "uri"),
        transport: connection.transport,
        signal,
        metadata: { tool_call_id: identity.toolCallId },
      });
      return simpleExecution(`Subscribed MCP resource ${record.uri}`, canonicalJson(record) as JsonObject, serverId);
    }
    if (toolName === "mcp_unsubscribe_resource") {
      const serverId = requiredString(argumentsValue, "server_id");
      const record = await this.subscriptions.unsubscribe(requiredString(argumentsValue, "subscription_id"), this.connections.requireConnected(serverId).transport, "tool_request", signal);
      return simpleExecution(`Unsubscribed MCP resource ${record.uri}`, canonicalJson(record) as JsonObject, serverId);
    }
    if (toolName === "mcp_list_prompts") return this.listPrompts(argumentsValue);
    if (toolName === "mcp_get_prompt") {
      const serverId = requiredString(argumentsValue, "server_id");
      const result = await this.client.getPrompt(this.clientIdentity(identity, serverId), requiredString(argumentsValue, "name"), objectValue(argumentsValue.arguments), signal);
      return execution(`Resolved MCP prompt ${serverId}/${String(argumentsValue.name)}`, result.result, serverId, result);
    }
    if (toolName === "mcp_list_tasks") {
      const tasks = this.tasks.list({ serverId: optionalString(argumentsValue, "server_id") || undefined, sessionId: identity.sessionId });
      return simpleExecution(`Listed ${tasks.length} MCP tasks`, { tasks: canonicalJson(tasks) }, optionalString(argumentsValue, "server_id"));
    }
    if (toolName === "mcp_poll_task") {
      const serverId = requiredString(argumentsValue, "server_id");
      const task = await this.tasks.poll(serverId, requiredString(argumentsValue, "task_id"), this.connections.requireConnected(serverId).transport, signal);
      return simpleExecution(`Polled MCP task ${task.remoteTaskId}`, canonicalJson(task) as JsonObject, serverId);
    }
    if (toolName === "mcp_task_result") {
      const serverId = requiredString(argumentsValue, "server_id");
      const result = await this.tasks.result(serverId, requiredString(argumentsValue, "task_id"), this.connections.requireConnected(serverId).transport, signal);
      return simpleExecution(`Read MCP task result ${result.task.taskId}`, canonicalJson(result) as JsonObject, serverId);
    }
    if (toolName === "mcp_cancel_task") {
      const serverId = requiredString(argumentsValue, "server_id");
      const task = await this.tasks.cancel(serverId, requiredString(argumentsValue, "task_id"), this.connections.requireConnected(serverId).transport, optionalString(argumentsValue, "reason") || "tool_request", signal);
      return simpleExecution(`Cancelled MCP task ${task.remoteTaskId}`, canonicalJson(task) as JsonObject, serverId);
    }
    throw coordinatorError("mcp_tool_not_owned", `MCP coordinator does not own ${toolName}`);
  }

  snapshot(): McpCoordinatorSnapshot {
    const withoutDigest = {
      version: "zyra.mcp-runtime-coordinator/v1" as const,
      identity: {
        sessionId: this.sessionId,
        workspaceRoot: this.workspaceRoot,
        interactive: this.interactive,
        sealedAutonomous: this.sealedAutonomous,
      },
      opened: this.opened,
      restoredBeforeBootstrap: this.restoredBeforeBootstrap,
      requestSequence: this.requestSequence,
      config: this.config.snapshot(),
      policy: this.policy.snapshot(),
      oauth: this.oauth.snapshot(),
      connections: this.connections.snapshot(),
      catalog: this.catalog.snapshot(),
      instructions: this.instructions.snapshot(),
      journal: this.journal.snapshot(),
      sampling: this.sampling.snapshot(),
      elicitation: this.elicitation.snapshot(),
      tasks: this.tasks.snapshot(),
      subscriptions: this.subscriptions.snapshot(),
      roots: this.roots.snapshot(),
      progress: this.progress.snapshot(),
      cancellation: this.cancellation.snapshot(),
      logging: this.logging.snapshot(),
      completion: this.completion.snapshot(),
      session: this.session.snapshot(),
      audit: this.audit.snapshot(),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  async close(): Promise<void> {
    await this.connections.disconnectAll("coordinator_close");
    this.opened = false;
  }

  health(): JsonObject {
    return {
      canonical_owner: "typescript",
      opened: this.opened,
      restored_before_bootstrap: this.restoredBeforeBootstrap,
      connected_servers: this.connections.list().filter((value) => value.phase === "ready").length,
      config_revision: this.config.revision,
      policy_revision: this.policy.revision,
      catalog_revision: this.catalog.snapshot().revision,
      pending_requests: this.journal.snapshot().records.filter((value) => value.status === "prepared" || value.status === "sent" || value.status === "effect_observed" || value.status === "indeterminate").length,
      session_ready_servers: this.session.listServers().filter((value) => value.phase === "ready").length,
      session_reconciling_servers: this.session.listServers().filter((value) => value.phase === "reconciling").length,
      indeterminate_invocations: this.session.listLeases({ status: "indeterminate" }).length,
      unresolved_recovery_actions: this.session.listRecovery({ unresolvedOnly: true }).length,
      audit_head_hash: this.audit.snapshot().headHash,
      python_live_client_fallback: false,
      snapshot_digest: this.snapshot().digest,
    };
  }

  private rebuildPolicy(): void {
    const rules: McpPolicyRule[] = [];
    for (const config of this.config.list()) {
      rules.push({
        ruleId: `managed:${config.serverId}:connect`,
        effect: config.enabled ? "allow" : "deny",
        priority: 10_000,
        serverPattern: config.serverId,
        transportPattern: config.transport.kind,
        operationPattern: "connect",
        capabilityPattern: "*",
        resourcePattern: "*",
        workspacePattern: this.workspaceRoot,
        sourcePattern: config.source,
        interactiveOnly: false,
        autonomousOnly: false,
        expiresAt: null,
        reason: config.enabled ? "configured MCP server connection" : "disabled MCP server",
        metadata: { config_digest: config.configDigest },
      });
      for (const operation of config.allowedOperations.length ? config.allowedOperations : ["*"]) {
        rules.push({
          ruleId: `managed:${config.serverId}:operation:${operation}`,
          effect: "allow",
          priority: 9_000,
          serverPattern: config.serverId,
          transportPattern: config.transport.kind,
          operationPattern: operation,
          capabilityPattern: "*",
          resourcePattern: "*",
          workspacePattern: this.workspaceRoot,
          sourcePattern: config.source,
          interactiveOnly: false,
          autonomousOnly: false,
          expiresAt: null,
          reason: "operation explicitly enabled by the merged MCP config",
          metadata: { config_digest: config.configDigest },
        });
      }
    }
    this.policy.replace(rules, this.policy.revision, { interactive: "deny", autonomous: "deny" });
  }

  private synchronizeSession(reason: string): void {
    this.session.synchronize(
      this.connections.snapshot(),
      this.catalog.snapshot(),
      this.journal.snapshot(),
      { reason },
    );
  }

  private rebuildProjections(): void {
    this.projections.clear();
    for (const server of this.catalog.list()) {
      for (const tool of this.client.projections(server.serverId).tools) this.projections.set(tool.name, tool);
    }
  }

  private listResources(argumentsValue: JsonObject): McpCoordinatorExecution {
    const requested = optionalString(argumentsValue, "server_id");
    const values = this.catalog.list().filter((server) => !requested || server.serverId === requested).map((server) => this.client.projections(server.serverId).resources);
    const count = values.reduce((total, value) => total + value.resources.length + value.templates.length, 0);
    return simpleExecution(`Listed ${count} MCP resources`, { servers: canonicalJson(values) }, requested);
  }

  private listPrompts(argumentsValue: JsonObject): McpCoordinatorExecution {
    const requested = optionalString(argumentsValue, "server_id");
    const values = this.catalog.list().filter((server) => !requested || server.serverId === requested).flatMap((server) => this.client.projections(server.serverId).prompts);
    return simpleExecution(`Listed ${values.length} MCP prompts`, { prompts: canonicalJson(values) }, requested);
  }

  private clientIdentity(identity: McpCoordinatorIdentity, serverId: string): McpClientInvocationIdentity {
    const connection = this.connections.requireConnected(serverId).record;
    return {
      runId: identity.runId,
      sessionRevision: identity.sessionRevision,
      workerRequestId: identity.workerRequestId,
      serverId,
      connectionId: connection.connectionId,
      connectionEpoch: connection.epoch,
      requestId: this.nextRequestId("client", serverId),
      method: "dynamic",
      sessionId: identity.sessionId,
      taskId: identity.taskId,
      toolCallId: identity.toolCallId,
      workspaceRoot: identity.workspaceRoot,
      interactive: identity.interactive,
      sealedAutonomous: identity.sealedAutonomous,
    };
  }

  private identity(value: Partial<McpCoordinatorIdentity>): McpCoordinatorIdentity {
    return {
      runId: value.runId || "mcp-run",
      taskId: value.taskId || "mcp-task",
      sessionId: value.sessionId || this.sessionId,
      sessionRevision: value.sessionRevision ?? 0,
      workerRequestId: value.workerRequestId || "mcp-worker",
      toolCallId: value.toolCallId || this.nextRequestId("tool-call", "runtime"),
      workspaceRoot: value.workspaceRoot || this.workspaceRoot,
      interactive: value.interactive ?? this.interactive,
      sealedAutonomous: value.sealedAutonomous ?? this.sealedAutonomous,
    };
  }

  private async sampleServerRequest(
    serverId: string,
    connectionId: string,
    connectionEpoch: number,
    taskId: string,
    metadata: JsonObject,
    request: McpCreateMessageRequest,
    signal?: AbortSignal,
  ): Promise<McpCreateMessageResult> {
    const config = this.config.get(serverId);
    if (!config?.allowSampling) throw coordinatorError("mcp_sampling_disabled", `sampling is disabled for ${serverId}`);
    return this.sampling.sample({
      serverId,
      connectionId,
      connectionEpoch,
      requestId: typeof metadata.server_request_id === "string" ? metadata.server_request_id : this.nextRequestId("sampling", serverId),
      sessionId: this.sessionId,
      taskId,
      workspaceRoot: this.workspaceRoot,
      policyDigest: this.policy.digest,
      allowedModels: Array.isArray(config.metadata.allowed_models) ? config.metadata.allowed_models.filter((value): value is string => typeof value === "string") : [],
      maximumTokens: typeof config.metadata.sampling_token_budget === "number" ? config.metadata.sampling_token_budget : 32_768,
      maximumContextTokens: typeof config.metadata.sampling_context_budget === "number" ? config.metadata.sampling_context_budget : 131_072,
      allowTools: config.metadata.sampling_allow_tools === true,
      allowServerContext: config.metadata.sampling_allow_server_context === true,
      metadata,
    }, request, signal);
  }

  private async elicitServerRequest(
    serverId: string,
    connectionId: string,
    connectionEpoch: number,
    taskId: string,
    metadata: JsonObject,
    request: McpElicitationRequest,
    signal?: AbortSignal,
  ): Promise<McpElicitationResult> {
    const config = this.config.get(serverId);
    if (!config?.allowElicitation) throw coordinatorError("mcp_elicitation_disabled", `elicitation is disabled for ${serverId}`);
    const identity = this.identity({
      taskId,
      toolCallId: typeof metadata.server_request_id === "string" ? metadata.server_request_id : this.nextRequestId("elicitation-tool", serverId),
    });
    const record = this.elicitation.request({
      runId: identity.runId,
      taskId: identity.taskId,
      sessionId: identity.sessionId,
      sessionRevision: identity.sessionRevision,
      workerRequestId: identity.workerRequestId,
      toolCallId: identity.toolCallId,
      serverId,
      connectionId,
      connectionEpoch,
      requestId: typeof metadata.server_request_id === "string" ? metadata.server_request_id : this.nextRequestId("elicitation", serverId),
    }, request, { metadata });
    const result = this.sealedAutonomous || !this.elicitationPort
      ? { action: "decline" as const, content: {}, meta: { reason: this.sealedAutonomous ? "sealed_autonomous" : "approval_port_unavailable" } }
      : await this.elicitationPort({ record, request, identity, signal });
    const resumed = this.elicitation.resume({
      elicitationId: record.elicitationId,
      continuationId: record.continuationId,
      runId: identity.runId,
      taskId: identity.taskId,
      sessionId: identity.sessionId,
      sessionRevision: identity.sessionRevision,
      workerRequestId: identity.workerRequestId,
      toolCallId: identity.toolCallId,
      serverId,
      connectionId,
      connectionEpoch,
      requestId: record.identity.requestId,
      result,
    });
    return cloneJson(resumed.response!);
  }

  private async handleServerMessage(
    serverId: string,
    connectionId: string,
    connectionEpoch: number,
    method: string,
    params: JsonObject,
    signal?: AbortSignal,
  ): Promise<JsonValue> {
    if (method === "notifications/resources/updated") {
      const identity = this.identity({ taskId: "resource-update", toolCallId: this.nextRequestId("resource-update", serverId) });
      const updates = await this.subscriptions.applyNotification({
        serverId,
        connectionId,
        connectionEpoch,
        params,
        read: async (uri) => (await this.client.readResource(this.clientIdentity(identity, serverId), uri, signal)).result,
      });
      return canonicalJson({ updates });
    }
    if (method === "notifications/progress") return canonicalJson(await this.progress.handleNotification(serverId, connectionEpoch, params));
    if (method === "notifications/cancelled") return canonicalJson(this.cancellation.cancelByNotification(serverId, connectionEpoch, params));
    if (this.customServerRequest) return canonicalJson(await this.customServerRequest(serverId, method, params, signal));
    throw coordinatorError("mcp_client_method_not_found", `unsupported MCP client method ${method}`, { server_id: serverId, method });
  }

  private nextRequestId(kind: string, serverId: string): string {
    this.requestSequence += 1;
    return deterministicMcpId("mcp-coordinator-request", {
      session_id: this.sessionId,
      server_id: serverId,
      kind,
      sequence: this.requestSequence,
    }, 32);
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function spec(name: string, purpose: string, properties: JsonObject, access: string, required: string[] = []): McpCoordinatorToolSpec {
  return {
    name,
    purpose,
    source: "typescript-mcp",
    input_schema: { type: "object", properties, ...(required.length ? { required } : {}) },
    output_schema: { type: "object" },
    metadata: { access_mode: access, canonical_runtime_owner: "typescript" },
    execution_provenance: { namespace: "mcp", server_id: "", version: "2025-06-18", source: "zyra-e02-mcp-coordinator" },
  };
}

function execution(summary: string, output: JsonObject, serverId: string, result: McpClientExecution<JsonObject>): McpCoordinatorExecution {
  return {
    summary,
    output,
    metadata: {
      canonical_runtime_owner: "typescript",
      capability_owner: "typescript-mcp",
      mcp_server_id: serverId,
      mcp_request_id: result.requestId,
      mcp_transition_id: result.transitionId,
      mcp_catalog_revision: String(result.catalogRevision),
      mcp_connection_epoch: String(result.connectionEpoch),
      mcp_replayed: String(result.replayed),
    },
  };
}

function simpleExecution(summary: string, output: JsonObject, serverId = ""): McpCoordinatorExecution {
  return {
    summary,
    output,
    metadata: {
      canonical_runtime_owner: "typescript",
      capability_owner: "typescript-mcp",
      mcp_server_id: serverId,
    },
  };
}

function requiredString(value: JsonObject, key: string): string {
  const item = value[key];
  if (typeof item !== "string" || !item.trim()) throw coordinatorError("mcp_argument_missing", `MCP argument ${key} is required`);
  return item;
}

function optionalString(value: JsonObject, key: string): string {
  const item = value[key];
  return typeof item === "string" ? item : "";
}

function objectValue(value: JsonValue | undefined): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? cloneJson(value as JsonObject) : {};
}

function builtinIdempotent(toolName: string): boolean {
  return new Set([
    "mcp_list_resources",
    "mcp_read_resource",
    "mcp_list_prompts",
    "mcp_get_prompt",
    "mcp_list_tasks",
    "mcp_poll_task",
    "mcp_task_result",
  ]).has(toolName);
}

function errorCode(error: unknown): string {
  if (error && typeof error === "object") {
    const value = (error as { code?: unknown }).code;
    if (typeof value === "string" && value) return value;
  }
  return error instanceof Error && error.name
    ? error.name
    : "mcp_execution_failed";
}

async function rejectSamplingProvider(): Promise<never> {
  throw coordinatorError("mcp_sampling_provider_unavailable", "MCP sampling provider is not connected");
}

function coordinatorError(code: string, message: string, details: JsonObject = {}): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-coordinator", { code, message, details }),
    category: code.includes("permission") || code.includes("disabled") ? "policy" : code.includes("not_found") ? "capability" : "internal",
    code,
    message,
    retryable: false,
    disposition: code.includes("disabled") || code.includes("not_found") ? "replan" : "terminal",
    details,
  });
}
