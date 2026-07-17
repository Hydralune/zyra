import type { JsonObject } from "../contracts.ts";
import type { McpCatalogServerSnapshot } from "../catalog/capability-catalog.ts";
import { canonicalJson, cloneJson, deterministicMcpId, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import type { McpTool } from "../core/protocol.ts";

export interface McpProjectedTool {
  projectionId: string;
  name: string;
  originalName: string;
  serverId: string;
  connectionId: string;
  catalogRevision: number;
  description: string;
  inputSchema: JsonObject;
  outputSchema: JsonObject | null;
  operation: "tools/call";
  readOnly: boolean;
  destructive: boolean;
  idempotent: boolean;
  openWorld: boolean;
  schemaDigest: string;
  capabilityDigest: string;
  permissionScope: JsonObject;
  invocation: JsonObject;
  metadata: JsonObject;
}

export interface McpToolProjectionOptions {
  prefix?: string;
  maximumNameLength?: number;
  includeServerInstructions?: boolean;
  collisionPolicy?: "reject" | "digest_suffix";
}

export class McpToolProjection {
  private readonly prefix: string;
  private readonly maximumNameLength: number;
  private readonly includeServerInstructions: boolean;
  private readonly collisionPolicy: "reject" | "digest_suffix";

  constructor(options: McpToolProjectionOptions = {}) {
    this.prefix = options.prefix ?? "mcp";
    this.maximumNameLength = options.maximumNameLength ?? 128;
    this.includeServerInstructions = options.includeServerInstructions ?? true;
    this.collisionPolicy = options.collisionPolicy ?? "digest_suffix";
  }

  materialize(server: McpCatalogServerSnapshot): McpProjectedTool[] {
    const output: McpProjectedTool[] = [];
    const names = new Map<string, string>();
    for (const tool of server.tools) {
      const baseName = projectName(this.prefix, server.serverId, tool.name, this.maximumNameLength);
      let name = baseName;
      const existing = names.get(name);
      if (existing && existing !== tool.name) {
        if (this.collisionPolicy === "reject") throw projectionError(server.serverId, "tool_name_collision", `tools ${existing} and ${tool.name} project to ${name}`);
        const suffix = sha256({ server_id: server.serverId, tool_name: tool.name }).slice(0, 10);
        name = `${baseName.slice(0, Math.max(1, this.maximumNameLength - suffix.length - 1))}_${suffix}`;
      }
      names.set(name, tool.name);
      output.push(this.project(server, tool, name));
    }
    return output.sort((left, right) => left.name.localeCompare(right.name));
  }

  private project(server: McpCatalogServerSnapshot, tool: McpTool, name: string): McpProjectedTool {
    const annotations = tool.annotations;
    const schemaDigest = sha256(tool.inputSchema);
    const capabilityDigest = sha256(tool);
    const readOnly = annotations?.readOnlyHint === true;
    const destructive = annotations?.destructiveHint ?? !readOnly;
    const idempotent = annotations?.idempotentHint ?? readOnly;
    const openWorld = annotations?.openWorldHint ?? true;
    const instructions = this.includeServerInstructions ? server.instructions : "";
    return {
      projectionId: deterministicMcpId("mcp-tool-projection", {
        server_id: server.serverId,
        connection_id: server.connectionId,
        revision: server.revision,
        tool_name: tool.name,
        capability_digest: capabilityDigest,
      }),
      name,
      originalName: tool.name,
      serverId: server.serverId,
      connectionId: server.connectionId,
      catalogRevision: server.revision,
      description: [tool.description, instructions].filter(Boolean).join("\n\n"),
      inputSchema: cloneJson(tool.inputSchema),
      outputSchema: tool.outputSchema ? cloneJson(tool.outputSchema) : null,
      operation: "tools/call",
      readOnly,
      destructive,
      idempotent,
      openWorld,
      schemaDigest,
      capabilityDigest,
      permissionScope: canonicalJson({
        namespace: "mcp",
        server_id: server.serverId,
        tool_name: tool.name,
        operation: "tools/call",
        read_only: readOnly,
        destructive,
        open_world: openWorld,
      }) as JsonObject,
      invocation: {
        method: "tools/call",
        params_shape: { name: tool.name, arguments: "$arguments" },
        connection_id: server.connectionId,
        connection_epoch: server.connectionEpoch,
        idempotent,
      },
      metadata: {
        title: tool.title,
        icons: canonicalJson(tool.icons),
        annotations: canonicalJson(tool.annotations),
        source_meta: canonicalJson(tool.meta),
      },
    };
  }
}

function projectName(prefix: string, serverId: string, toolName: string, maximum: number): string {
  const normalize = (value: string): string => value.normalize("NFKC").replace(/[^A-Za-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "");
  const base = `${normalize(prefix)}__${normalize(serverId)}__${normalize(toolName)}`;
  if (base.length <= maximum) return base;
  const digest = sha256(base).slice(0, 12);
  return `${base.slice(0, Math.max(1, maximum - digest.length - 1))}_${digest}`;
}

function projectionError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-tool-projection", { server_id: serverId, code, message }),
    category: "capability",
    code,
    message,
    serverId,
    retryable: false,
    disposition: "replan",
  });
}
