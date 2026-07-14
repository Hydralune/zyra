export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export type JsonObject = { [key: string]: JsonValue };

export interface JsonRpcRequest {
  jsonrpc: "2.0";
  id: number | string;
  method: string;
  params?: JsonObject;
}

export interface JsonRpcNotification {
  jsonrpc: "2.0";
  method: string;
  params?: JsonObject;
}

export interface JsonRpcResponse {
  jsonrpc: "2.0";
  id: number | string | null;
  result?: JsonValue;
  error?: {
    code: number;
    message: string;
    data?: JsonValue;
  };
}

export type JsonRpcMessage = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse;

export interface McpStdioServerConfig {
  id: string;
  transport: "stdio";
  command: string[];
  cwd?: string;
  envHandles?: Record<string, string>;
  enabled?: boolean;
  requestTimeoutMs?: number;
  reconnectAttempts?: number;
  metadata?: JsonObject;
}

export interface McpHttpServerConfig {
  id: string;
  transport: "http";
  url: string;
  headers?: Record<string, string>;
  bearerTokenEnv?: string;
  enabled?: boolean;
  requestTimeoutMs?: number;
  reconnectAttempts?: number;
  metadata?: JsonObject;
}

export type McpServerConfig = McpStdioServerConfig | McpHttpServerConfig;

export interface McpToolDefinition {
  name: string;
  description?: string;
  inputSchema?: JsonObject;
  outputSchema?: JsonObject;
  annotations?: JsonObject;
}

export interface McpResourceDefinition {
  uri: string;
  name?: string;
  description?: string;
  mimeType?: string;
}

export interface McpPromptDefinition {
  name: string;
  description?: string;
  arguments?: JsonObject[];
}

export interface McpServerCatalog {
  serverId: string;
  protocolVersion: string;
  serverInfo: JsonObject;
  capabilities: JsonObject;
  instructions: string;
  tools: McpToolDefinition[];
  resources: McpResourceDefinition[];
  prompts: McpPromptDefinition[];
  generation: number;
  connected: boolean;
  authStatus: "ready" | "needs_auth" | "failed";
  lastError: string;
}

export interface McpElicitationRequest {
  serverId: string;
  method: string;
  params: JsonObject;
}

export type McpElicitationHandler = (
  request: McpElicitationRequest,
) => Promise<JsonObject>;

export interface McpTransport {
  readonly serverId: string;
  connect(onMessage: (message: JsonRpcMessage) => void): Promise<void>;
  send(message: JsonRpcMessage): Promise<void>;
  close(): Promise<void>;
  isConnected(): boolean;
}

export class McpProtocolError extends Error {
  readonly code: string;
  readonly serverId: string;
  readonly data: JsonValue | undefined;

  constructor(code: string, message: string, serverId: string, data?: JsonValue) {
    super(message);
    this.name = "McpProtocolError";
    this.code = code;
    this.serverId = serverId;
    this.data = data;
  }
}

export function asObject(value: unknown): JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject
    : {};
}

export function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function asArray(value: unknown): JsonValue[] {
  return Array.isArray(value) ? value as JsonValue[] : [];
}
