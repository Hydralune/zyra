import {
  asArray,
  asObject,
  asString,
  type JsonObject,
  type JsonRpcMessage,
  type JsonRpcRequest,
  type JsonRpcResponse,
  type JsonValue,
  type McpElicitationHandler,
  type McpPromptDefinition,
  McpProtocolError,
  type McpResourceDefinition,
  type McpServerCatalog,
  type McpServerConfig,
  type McpToolDefinition,
  type McpTransport,
} from "./contracts.ts";
import { HttpMcpTransport, StdioMcpTransport } from "./transport.ts";

interface PendingRequest {
  resolve(value: JsonValue): void;
  reject(error: Error): void;
  timer: NodeJS.Timeout;
}

const LIST_CHANGED = new Set([
  "notifications/tools/list_changed",
  "notifications/resources/list_changed",
  "notifications/prompts/list_changed",
]);

export class McpClient {
  readonly serverId: string;
  private readonly config: McpServerConfig;
  private readonly transport: McpTransport;
  private readonly pending = new Map<number | string, PendingRequest>();
  private nextRequestId = 1;
  private initialized = false;
  private closed = false;
  private generation = 0;
  private catalogDirty = true;
  private catalogValue: McpServerCatalog;
  private elicitationHandler: McpElicitationHandler | null = null;

  constructor(config: McpServerConfig) {
    this.config = config;
    this.serverId = config.id;
    this.transport = config.transport === "stdio"
      ? new StdioMcpTransport(config)
      : new HttpMcpTransport(config);
    this.catalogValue = emptyCatalog(config.id);
  }

  setElicitationHandler(handler: McpElicitationHandler | null): void {
    this.elicitationHandler = handler;
  }

  async initialize(): Promise<McpServerCatalog> {
    if (this.closed) {
      throw new McpProtocolError("client_closed", "MCP client is closed", this.serverId);
    }
    if (!this.transport.isConnected()) {
      await this.transport.connect((message) => this.onMessage(message));
    }
    if (!this.initialized) {
      try {
        const result = asObject(await this.request("initialize", {
          protocolVersion: "2025-06-18",
          capabilities: {
            roots: { listChanged: true },
            elicitation: {},
          },
          clientInfo: { name: "zyra-claude-runtime", version: "0.1.0" },
        }));
        this.catalogValue.protocolVersion = asString(result.protocolVersion);
        this.catalogValue.serverInfo = asObject(result.serverInfo);
        this.catalogValue.capabilities = asObject(result.capabilities);
        this.catalogValue.instructions = asString(result.instructions);
        this.catalogValue.authStatus = "ready";
        this.catalogValue.connected = true;
        this.catalogValue.lastError = "";
        this.initialized = true;
        await this.notify("notifications/initialized", {});
      } catch (error) {
        const protocolError = error instanceof McpProtocolError ? error : null;
        const data = asObject(protocolError?.data);
        this.catalogValue.authStatus = asString(data.status) === "needs_auth"
          ? "needs_auth"
          : "failed";
        this.catalogValue.lastError = error instanceof Error ? error.message : String(error);
        throw error;
      }
    }
    return this.refreshCatalog();
  }

  async refreshCatalog(force = false): Promise<McpServerCatalog> {
    if (!this.initialized) {
      return this.initialize();
    }
    if (!force && !this.catalogDirty) {
      return cloneCatalog(this.catalogValue);
    }
    const [toolsResult, resourcesResult, promptsResult] = await Promise.all([
      this.request("tools/list", {}),
      this.request("resources/list", {}),
      this.request("prompts/list", {}),
    ]);
    this.catalogValue.tools = asArray(asObject(toolsResult).tools)
      .map((value) => normalizeTool(asObject(value)))
      .filter((value) => Boolean(value.name));
    this.catalogValue.resources = asArray(asObject(resourcesResult).resources)
      .map((value) => normalizeResource(asObject(value)))
      .filter((value) => Boolean(value.uri));
    this.catalogValue.prompts = asArray(asObject(promptsResult).prompts)
      .map((value) => normalizePrompt(asObject(value)))
      .filter((value) => Boolean(value.name));
    this.generation += 1;
    this.catalogValue.generation = this.generation;
    this.catalogValue.connected = true;
    this.catalogDirty = false;
    return cloneCatalog(this.catalogValue);
  }

  catalog(): McpServerCatalog {
    return cloneCatalog(this.catalogValue);
  }

  async callTool(name: string, argumentsValue: JsonObject): Promise<JsonObject> {
    return asObject(await this.request("tools/call", {
      name,
      arguments: argumentsValue,
    }));
  }

  async readResource(uri: string): Promise<JsonObject> {
    return asObject(await this.request("resources/read", { uri }));
  }

  async getPrompt(name: string, argumentsValue: JsonObject = {}): Promise<JsonObject> {
    return asObject(await this.request("prompts/get", {
      name,
      arguments: argumentsValue,
    }));
  }

  async ping(): Promise<void> {
    await this.request("ping", {});
  }

  async request(method: string, params: JsonObject): Promise<JsonValue> {
    const id = this.nextRequestId++;
    const message: JsonRpcRequest = { jsonrpc: "2.0", id, method, params };
    const timeoutMs = Math.max(100, this.config.requestTimeoutMs ?? 30_000);
    const response = new Promise<JsonValue>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new McpProtocolError(
          "request_timeout",
          `MCP request ${method} timed out after ${timeoutMs}ms`,
          this.serverId,
        ));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
    });
    try {
      await this.transport.send(message);
    } catch (error) {
      const pending = this.pending.get(id);
      if (pending) {
        clearTimeout(pending.timer);
        this.pending.delete(id);
        pending.reject(error instanceof Error ? error : new Error(String(error)));
      }
    }
    return response;
  }

  async notify(method: string, params: JsonObject): Promise<void> {
    await this.transport.send({ jsonrpc: "2.0", method, params });
  }

  async reconnect(): Promise<McpServerCatalog> {
    if (this.closed) {
      throw new McpProtocolError("client_closed", "MCP client is closed", this.serverId);
    }
    this.rejectAll("transport_reconnecting", "MCP transport is reconnecting");
    await this.transport.close();
    this.initialized = false;
    this.catalogDirty = true;
    this.catalogValue.connected = false;
    this.catalogValue.lastError = "";
    return this.initialize();
  }

  async close(): Promise<void> {
    this.closed = true;
    this.initialized = false;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new McpProtocolError(
        "client_closed",
        "MCP client closed before response",
        this.serverId,
      ));
    }
    this.pending.clear();
    await this.transport.close();
  }

  private onMessage(message: JsonRpcMessage): void {
    if ("id" in message && ("result" in message || "error" in message)) {
      this.onResponse(message as JsonRpcResponse);
      return;
    }
    if (!("method" in message)) {
      return;
    }
    if (LIST_CHANGED.has(message.method)) {
      this.catalogDirty = true;
      return;
    }
    if (message.method === "notifications/zyra_transport_closed") {
      this.initialized = false;
      this.catalogValue.connected = false;
      this.catalogValue.lastError = asString(asObject(message.params).stderr)
        || "MCP transport closed";
      this.rejectAll("transport_closed", this.catalogValue.lastError);
      return;
    }
    if (message.method === "notifications/zyra_transport_error"
      || message.method === "notifications/zyra_protocol_error") {
      this.catalogValue.lastError = asString(asObject(message.params).message);
      return;
    }
    if ("id" in message) {
      void this.handleServerRequest(message as JsonRpcRequest);
    }
  }

  private onResponse(response: JsonRpcResponse): void {
    if (response.id === null) {
      return;
    }
    const pending = this.pending.get(response.id);
    if (!pending) {
      return;
    }
    clearTimeout(pending.timer);
    this.pending.delete(response.id);
    if (response.error) {
      pending.reject(new McpProtocolError(
        `json_rpc_${response.error.code}`,
        response.error.message,
        this.serverId,
        response.error.data,
      ));
      return;
    }
    pending.resolve(response.result ?? null);
  }

  private async handleServerRequest(request: JsonRpcRequest): Promise<void> {
    if (!request.method.startsWith("elicitation/")) {
      await this.transport.send({
        jsonrpc: "2.0",
        id: request.id,
        error: { code: -32601, message: "client method not supported" },
      });
      return;
    }
    try {
      const result = this.elicitationHandler
        ? await this.elicitationHandler({
          serverId: this.serverId,
          method: request.method,
          params: asObject(request.params),
        })
        : { action: "decline", reason: "no_user_approval_channel" };
      await this.transport.send({
        jsonrpc: "2.0",
        id: request.id,
        result,
      });
    } catch (error) {
      await this.transport.send({
        jsonrpc: "2.0",
        id: request.id,
        error: {
          code: -32000,
          message: error instanceof Error ? error.message : String(error),
        },
      });
    }
  }

  private rejectAll(code: string, message: string): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new McpProtocolError(code, message, this.serverId));
    }
    this.pending.clear();
  }
}

function normalizeTool(value: JsonObject): McpToolDefinition {
  return {
    name: asString(value.name),
    description: asString(value.description),
    inputSchema: asObject(value.inputSchema),
    outputSchema: asObject(value.outputSchema),
    annotations: asObject(value.annotations),
  };
}

function normalizeResource(value: JsonObject): McpResourceDefinition {
  return {
    uri: asString(value.uri),
    name: asString(value.name),
    description: asString(value.description),
    mimeType: asString(value.mimeType),
  };
}

function normalizePrompt(value: JsonObject): McpPromptDefinition {
  return {
    name: asString(value.name),
    description: asString(value.description),
    arguments: asArray(value.arguments).map((item) => asObject(item)),
  };
}

function emptyCatalog(serverId: string): McpServerCatalog {
  return {
    serverId,
    protocolVersion: "",
    serverInfo: {},
    capabilities: {},
    instructions: "",
    tools: [],
    resources: [],
    prompts: [],
    generation: 0,
    connected: false,
    authStatus: "ready",
    lastError: "",
  };
}

function cloneCatalog(value: McpServerCatalog): McpServerCatalog {
  return JSON.parse(JSON.stringify(value)) as McpServerCatalog;
}
