import type { JsonObject } from "../contracts.ts";
import type { McpCatalogServerSnapshot } from "../catalog/capability-catalog.ts";
import { canonicalJson, cloneJson, deterministicMcpId, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import type { McpPrompt } from "../core/protocol.ts";

export interface McpProjectedPromptArgument {
  name: string;
  title: string | null;
  description: string | null;
  required: boolean;
  ordinal: number;
}

export interface McpProjectedPrompt {
  projectionId: string;
  commandName: string;
  originalName: string;
  serverId: string;
  connectionId: string;
  catalogRevision: number;
  title: string | null;
  description: string | null;
  arguments: McpProjectedPromptArgument[];
  operation: "prompts/get";
  capabilityDigest: string;
  permissionScope: JsonObject;
  invocation: JsonObject;
  metadata: JsonObject;
}

export class McpPromptProjection {
  private readonly namespace: string;
  private readonly maximumNameLength: number;

  constructor(options: { namespace?: string; maximumNameLength?: number } = {}) {
    this.namespace = options.namespace ?? "mcp";
    this.maximumNameLength = options.maximumNameLength ?? 96;
  }

  materialize(server: McpCatalogServerSnapshot): McpProjectedPrompt[] {
    const output: McpProjectedPrompt[] = [];
    const commandNames = new Set<string>();
    for (const prompt of server.prompts) {
      let commandName = normalizeCommandName(`${this.namespace}:${server.serverId}:${prompt.name}`, this.maximumNameLength);
      if (commandNames.has(commandName)) {
        const suffix = sha256({ server_id: server.serverId, prompt_name: prompt.name }).slice(0, 10);
        commandName = `${commandName.slice(0, this.maximumNameLength - suffix.length - 1)}-${suffix}`;
      }
      if (commandNames.has(commandName)) throw promptProjectionError(server.serverId, "prompt_name_collision", `prompt ${prompt.name} collides at ${commandName}`);
      commandNames.add(commandName);
      output.push(this.project(server, prompt, commandName));
    }
    return output.sort((left, right) => left.commandName.localeCompare(right.commandName));
  }

  validateArguments(prompt: McpProjectedPrompt, input: JsonObject): JsonObject {
    const output: JsonObject = {};
    const allowed = new Set(prompt.arguments.map((argument) => argument.name));
    for (const key of Object.keys(input)) {
      if (!allowed.has(key)) throw promptProjectionError(prompt.serverId, "unknown_prompt_argument", `prompt ${prompt.originalName} does not accept argument ${key}`);
    }
    for (const argument of prompt.arguments) {
      const value = input[argument.name];
      if (argument.required && (value === undefined || value === null || value === "")) {
        throw promptProjectionError(prompt.serverId, "required_prompt_argument_missing", `prompt ${prompt.originalName} requires ${argument.name}`);
      }
      if (value !== undefined) output[argument.name] = cloneJson(value);
    }
    return output;
  }

  private project(server: McpCatalogServerSnapshot, prompt: McpPrompt, commandName: string): McpProjectedPrompt {
    const capabilityDigest = sha256(prompt);
    const args = prompt.arguments.map((argument, ordinal) => ({
      name: argument.name,
      title: argument.title,
      description: argument.description,
      required: argument.required,
      ordinal,
    }));
    return {
      projectionId: deterministicMcpId("mcp-prompt-projection", {
        server_id: server.serverId,
        connection_id: server.connectionId,
        revision: server.revision,
        prompt_name: prompt.name,
        capability_digest: capabilityDigest,
      }),
      commandName,
      originalName: prompt.name,
      serverId: server.serverId,
      connectionId: server.connectionId,
      catalogRevision: server.revision,
      title: prompt.title,
      description: prompt.description,
      arguments: args,
      operation: "prompts/get",
      capabilityDigest,
      permissionScope: canonicalJson({
        namespace: "mcp",
        server_id: server.serverId,
        prompt_name: prompt.name,
        operation: "prompts/get",
        read_only: true,
      }) as JsonObject,
      invocation: {
        method: "prompts/get",
        params_shape: { name: prompt.name, arguments: "$arguments" },
        connection_id: server.connectionId,
        connection_epoch: server.connectionEpoch,
        idempotent: true,
      },
      metadata: {
        icons: canonicalJson(prompt.icons),
        source_meta: canonicalJson(prompt.meta),
      },
    };
  }
}

function normalizeCommandName(value: string, maximum: number): string {
  const normalized = value.normalize("NFKC").toLowerCase().replace(/[^a-z0-9:_-]+/g, "-").replace(/^-+|-+$/g, "");
  if (normalized.length <= maximum) return normalized;
  const suffix = sha256(normalized).slice(0, 12);
  return `${normalized.slice(0, maximum - suffix.length - 1)}-${suffix}`;
}

function promptProjectionError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-prompt-projection", { server_id: serverId, code, message }),
    category: "capability",
    code,
    message,
    serverId,
    retryable: false,
    disposition: "replan",
  });
}
