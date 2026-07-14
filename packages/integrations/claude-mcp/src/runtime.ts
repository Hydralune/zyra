import { createHash } from "node:crypto";

import {
  asObject,
  asString,
  type JsonObject,
  type JsonValue,
  type McpServerCatalog,
  type McpServerConfig,
} from "./contracts.ts";
import { McpClient } from "./client.ts";

export interface McpRuntimeToolSpec {
  name: string;
  purpose: string;
  source: "typescript-mcp";
  input_schema: JsonObject;
  output_schema: JsonObject;
  metadata: Record<string, string>;
  execution_provenance: JsonObject;
}

export interface McpExecutionResult {
  summary: string;
  output: JsonObject;
  metadata: Record<string, string>;
}

export class TypeScriptMcpRuntime {
  private readonly clients = new Map<string, McpClient>();
  private readonly toolBindings = new Map<string, { serverId: string; toolName: string }>();
  private catalogs: McpServerCatalog[] = [];
  private opened = false;

  constructor(configs: McpServerConfig[]) {
    for (const config of configs) {
      if (config.enabled === false || !config.id || this.clients.has(config.id)) {
        continue;
      }
      this.clients.set(config.id, new McpClient(config));
    }
  }

  async open(): Promise<void> {
    if (this.opened) {
      return;
    }
    const catalogs: McpServerCatalog[] = [];
    for (const client of this.clients.values()) {
      try {
        catalogs.push(await client.initialize());
      } catch {
        catalogs.push(client.catalog());
      }
    }
    this.catalogs = catalogs.sort((left, right) => left.serverId.localeCompare(right.serverId));
    this.rebuildBindings();
    this.opened = true;
  }

  toolSpecs(): McpRuntimeToolSpec[] {
    const specs: McpRuntimeToolSpec[] = [];
    for (const catalog of this.catalogs) {
      if (!catalog.connected || catalog.authStatus !== "ready") {
        continue;
      }
      for (const tool of catalog.tools) {
        const name = mcpToolName(catalog.serverId, tool.name);
        specs.push({
          name,
          purpose: tool.description || `Invoke ${tool.name} on MCP server ${catalog.serverId}`,
          source: "typescript-mcp",
          input_schema: tool.inputSchema ?? { type: "object" },
          output_schema: tool.outputSchema ?? { type: "object" },
          metadata: {
            access_mode: "remote_execute",
            canonical_runtime_owner: "typescript",
            mcp_server_id: catalog.serverId,
            mcp_tool_name: tool.name,
            catalog_generation: String(catalog.generation),
          },
          execution_provenance: {
            namespace: "mcp",
            server_id: catalog.serverId,
            version: asString(catalog.serverInfo.version),
            source: "typescript-mcp-client",
          },
        });
      }
    }
    return specs.sort((left, right) => left.name.localeCompare(right.name));
  }

  owns(toolName: string): boolean {
    return this.toolBindings.has(toolName)
      || toolName === "mcp_list_resources"
      || toolName === "mcp_read_resource"
      || toolName === "mcp_list_prompts"
      || toolName === "mcp_get_prompt";
  }

  async execute(toolName: string, argumentsValue: JsonObject): Promise<McpExecutionResult> {
    if (toolName === "mcp_list_resources") {
      return this.listResources(argumentsValue);
    }
    if (toolName === "mcp_read_resource") {
      return this.readResource(argumentsValue);
    }
    if (toolName === "mcp_list_prompts") {
      return this.listPrompts(argumentsValue);
    }
    if (toolName === "mcp_get_prompt") {
      return this.getPrompt(argumentsValue);
    }
    const binding = this.toolBindings.get(toolName);
    if (!binding) {
      throw new Error(`TypeScript MCP runtime does not own ${toolName}`);
    }
    const client = this.requiredClient(binding.serverId);
    const output = await client.callTool(binding.toolName, argumentsValue);
    return {
      summary: `MCP tool ${binding.serverId}/${binding.toolName} completed`,
      output,
      metadata: {
        canonical_runtime_owner: "typescript",
        capability_owner: "typescript-mcp",
        mcp_server_id: binding.serverId,
        mcp_tool_name: binding.toolName,
      },
    };
  }

  catalogToolSpecs(): McpRuntimeToolSpec[] {
    if (this.clients.size === 0) {
      return [];
    }
    const common = {
      source: "typescript-mcp" as const,
      output_schema: { type: "object" },
      execution_provenance: {
        namespace: "mcp",
        server_id: "",
        version: "2025-06-18",
        source: "typescript-mcp-client",
      },
    };
    return [
      {
        ...common,
        name: "mcp_list_resources",
        purpose: "List resources from TypeScript-owned MCP connections",
        input_schema: { type: "object", properties: { server_id: { type: "string" } } },
        metadata: { access_mode: "read", canonical_runtime_owner: "typescript" },
      },
      {
        ...common,
        name: "mcp_read_resource",
        purpose: "Read a resource through a TypeScript-owned MCP connection",
        input_schema: {
          type: "object",
          required: ["server_id", "uri"],
          properties: { server_id: { type: "string" }, uri: { type: "string" } },
        },
        metadata: { access_mode: "read", canonical_runtime_owner: "typescript" },
      },
      {
        ...common,
        name: "mcp_list_prompts",
        purpose: "List prompts from TypeScript-owned MCP connections",
        input_schema: { type: "object", properties: { server_id: { type: "string" } } },
        metadata: { access_mode: "read", canonical_runtime_owner: "typescript" },
      },
      {
        ...common,
        name: "mcp_get_prompt",
        purpose: "Resolve an MCP prompt through a TypeScript-owned connection",
        input_schema: {
          type: "object",
          required: ["server_id", "name"],
          properties: {
            server_id: { type: "string" },
            name: { type: "string" },
            arguments: { type: "object" },
          },
        },
        metadata: { access_mode: "read", canonical_runtime_owner: "typescript" },
      },
    ];
  }

  snapshot(): JsonObject {
    const catalogs = this.catalogs.map((catalog) => ({
      server_id: catalog.serverId,
      protocol_version: catalog.protocolVersion,
      generation: catalog.generation,
      connected: catalog.connected,
      auth_status: catalog.authStatus,
      tool_count: catalog.tools.length,
      resource_count: catalog.resources.length,
      prompt_count: catalog.prompts.length,
      last_error: catalog.lastError,
    }));
    return {
      version: "zyra.typescript-mcp-runtime.v1",
      canonical_owner: "typescript",
      catalogs,
      digest: digestJson(catalogs),
    };
  }

  async close(): Promise<void> {
    await Promise.all([...this.clients.values()].map((client) => client.close()));
    this.opened = false;
  }

  private rebuildBindings(): void {
    this.toolBindings.clear();
    for (const catalog of this.catalogs) {
      for (const tool of catalog.tools) {
        this.toolBindings.set(mcpToolName(catalog.serverId, tool.name), {
          serverId: catalog.serverId,
          toolName: tool.name,
        });
      }
    }
  }

  private async listResources(argumentsValue: JsonObject): Promise<McpExecutionResult> {
    const serverId = asString(argumentsValue.server_id);
    const catalogs = serverId
      ? this.catalogs.filter((catalog) => catalog.serverId === serverId)
      : this.catalogs;
    return {
      summary: `Listed ${catalogs.reduce((total, item) => total + item.resources.length, 0)} MCP resources`,
      output: {
        resources: catalogs.flatMap((catalog) => catalog.resources.map((resource) => ({
          ...resource,
          server_id: catalog.serverId,
        }))) as unknown as JsonValue,
      },
      metadata: { canonical_runtime_owner: "typescript", capability_owner: "typescript-mcp" },
    };
  }

  private async readResource(argumentsValue: JsonObject): Promise<McpExecutionResult> {
    const serverId = asString(argumentsValue.server_id);
    const uri = asString(argumentsValue.uri);
    const output = await this.requiredClient(serverId).readResource(uri);
    return {
      summary: `Read MCP resource ${uri}`,
      output,
      metadata: {
        canonical_runtime_owner: "typescript",
        capability_owner: "typescript-mcp",
        mcp_server_id: serverId,
      },
    };
  }

  private async listPrompts(argumentsValue: JsonObject): Promise<McpExecutionResult> {
    const serverId = asString(argumentsValue.server_id);
    const catalogs = serverId
      ? this.catalogs.filter((catalog) => catalog.serverId === serverId)
      : this.catalogs;
    return {
      summary: `Listed ${catalogs.reduce((total, item) => total + item.prompts.length, 0)} MCP prompts`,
      output: {
        prompts: catalogs.flatMap((catalog) => catalog.prompts.map((prompt) => ({
          ...prompt,
          server_id: catalog.serverId,
        }))) as unknown as JsonValue,
      },
      metadata: { canonical_runtime_owner: "typescript", capability_owner: "typescript-mcp" },
    };
  }

  private async getPrompt(argumentsValue: JsonObject): Promise<McpExecutionResult> {
    const serverId = asString(argumentsValue.server_id);
    const name = asString(argumentsValue.name);
    const output = await this.requiredClient(serverId).getPrompt(
      name,
      asObject(argumentsValue.arguments),
    );
    return {
      summary: `Resolved MCP prompt ${serverId}/${name}`,
      output,
      metadata: {
        canonical_runtime_owner: "typescript",
        capability_owner: "typescript-mcp",
        mcp_server_id: serverId,
      },
    };
  }

  private requiredClient(serverId: string): McpClient {
    const client = this.clients.get(serverId);
    if (!client) {
      throw new Error(`Unknown TypeScript MCP server: ${serverId}`);
    }
    return client;
  }
}

export function parseMcpServerConfigs(value: unknown): McpServerConfig[] {
  const values = Array.isArray(value) ? value : [];
  const configs: McpServerConfig[] = [];
  for (const item of values) {
    const object = asObject(item);
    const id = asString(object.id) || asString(object.server_id);
    const transport = asString(object.transport);
    if (!id) {
      continue;
    }
    if (transport === "stdio") {
      const rawCommand = Array.isArray(object.command) ? object.command : [];
      const command = rawCommand.filter((entry): entry is string => typeof entry === "string");
      if (command.length === 0) {
        continue;
      }
      configs.push({
        id,
        transport: "stdio",
        command,
        cwd: asString(object.cwd) || undefined,
        envHandles: stringRecord(object.env_handles ?? object.envHandles),
        enabled: object.enabled !== false,
        requestTimeoutMs: numeric(object.request_timeout_ms ?? object.requestTimeoutMs),
        reconnectAttempts: numeric(object.reconnect_attempts ?? object.reconnectAttempts),
        metadata: asObject(object.metadata),
      });
    } else if (transport === "http") {
      const url = asString(object.url);
      if (!url) {
        continue;
      }
      configs.push({
        id,
        transport: "http",
        url,
        headers: stringRecord(object.headers),
        bearerTokenEnv: asString(object.bearer_token_env ?? object.bearerTokenEnv) || undefined,
        enabled: object.enabled !== false,
        requestTimeoutMs: numeric(object.request_timeout_ms ?? object.requestTimeoutMs),
        reconnectAttempts: numeric(object.reconnect_attempts ?? object.reconnectAttempts),
        metadata: asObject(object.metadata),
      });
    }
  }
  return configs;
}

export function mcpToolName(serverId: string, toolName: string): string {
  const cleanServer = serverId.replace(/[^a-zA-Z0-9_-]+/g, "_");
  const cleanTool = toolName.replace(/[^a-zA-Z0-9_-]+/g, "_");
  return `mcp__${cleanServer}__${cleanTool}`;
}

function stringRecord(value: unknown): Record<string, string> | undefined {
  const object = asObject(value);
  const result: Record<string, string> = {};
  for (const [key, item] of Object.entries(object)) {
    if (typeof item === "string") {
      result[key] = item;
    }
  }
  return Object.keys(result).length > 0 ? result : undefined;
}

function numeric(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function digestJson(value: unknown): string {
  return `sha256:${createHash("sha256").update(JSON.stringify(value)).digest("hex")}`;
}
