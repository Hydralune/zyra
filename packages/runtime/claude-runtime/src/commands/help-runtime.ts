import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest } from "../e02/index.ts";
import type { CommandArgument, CommandDescriptor, CommandOption } from "./contracts.ts";
import { CommandRegistryRuntime } from "./registry-runtime.ts";

export interface CommandHelpDocument {
  documentId: string;
  commandId: string;
  commandName: string;
  registryRevision: number;
  title: string;
  synopsis: string;
  description: string;
  sections: { heading: string; lines: string[] }[];
  plainText: string;
  markdown: string;
  digest: string;
  metadata: JsonObject;
}

export class CommandHelpRuntime {
  private readonly registry: CommandRegistryRuntime;
  private readonly cache = new Map<string, CommandHelpDocument>();

  constructor(registry: CommandRegistryRuntime) {
    this.registry = registry;
  }

  render(name: string, revision = this.registry.currentRevision): CommandHelpDocument {
    const descriptor = this.registry.resolve(name, revision);
    const key = `${revision}\0${descriptor.commandId}\0${descriptor.descriptorDigest}`;
    const cached = this.cache.get(key);
    if (cached) return cloneJson(cached);
    const sections: CommandHelpDocument["sections"] = [];
    if (descriptor.arguments.length) sections.push({ heading: "Arguments", lines: descriptor.arguments.map(argumentLine) });
    if (descriptor.options.length) sections.push({ heading: "Options", lines: descriptor.options.map(optionLine) });
    if (descriptor.aliases.length) sections.push({ heading: "Aliases", lines: descriptor.aliases.map((alias) => `/${alias}`) });
    if (descriptor.examples.length) sections.push({ heading: "Examples", lines: descriptor.examples });
    sections.push({ heading: "Permission", lines: permissionLines(descriptor) });
    sections.push({ heading: "Handler", lines: handlerLines(descriptor) });
    const synopsis = descriptor.usage || synopsisFor(descriptor);
    const plainText = [descriptor.displayName, synopsis, descriptor.description, ...sections.flatMap((section) => [section.heading, ...section.lines.map((line) => `  ${line}`)])].join("\n");
    const markdown = [`# ${descriptor.displayName}`, "", `\`${synopsis}\``, "", descriptor.description, ...sections.flatMap((section) => ["", `## ${section.heading}`, "", ...section.lines.map((line) => `- ${line}`)])].join("\n");
    const base = {
      commandId: descriptor.commandId,
      commandName: descriptor.name,
      registryRevision: revision,
      title: descriptor.displayName,
      synopsis,
      description: descriptor.description,
      sections,
      plainText,
      markdown,
      metadata: { category: descriptor.category, source_kind: descriptor.sourceKind, source_id: descriptor.sourceId },
    };
    const documentId = deterministicId("command-help-document", base, 32);
    const document: CommandHelpDocument = { documentId, ...base, digest: digest({ documentId, ...base }) };
    this.cache.set(key, document);
    return cloneJson(document);
  }

  index(options: { revision?: number; category?: string; includeHidden?: boolean } = {}): JsonObject {
    const revision = options.revision ?? this.registry.currentRevision;
    const commands = this.registry.list({ revision, category: options.category }).filter((descriptor) => options.includeHidden || !descriptor.hidden).map((descriptor) => ({
      name: descriptor.name,
      display_name: descriptor.displayName,
      description: descriptor.description,
      usage: descriptor.usage,
      category: descriptor.category,
      aliases: descriptor.aliases,
      source: descriptor.sourceKind,
      permission_risk: descriptor.permission.risk,
    }));
    return { registry_revision: revision, commands, digest: digest(commands) };
  }

  clear(): void {
    this.cache.clear();
  }
}

function argumentLine(argument: CommandArgument): string {
  const name = argument.required ? `<${argument.name}>` : `[${argument.name}]`;
  const rest = argument.rest ? "..." : "";
  const defaultValue = argument.defaultValue === null ? "" : `; default ${JSON.stringify(argument.defaultValue)}`;
  const enumValue = argument.enumValues.length ? `; one of ${argument.enumValues.map((value) => JSON.stringify(value)).join(", ")}` : "";
  return `${name}${rest}: ${argument.type} — ${argument.description}${defaultValue}${enumValue}`;
}

function optionLine(option: CommandOption): string {
  const names = [`--${option.name}`, ...(option.short ? [`-${option.short}`] : [])].join(", ");
  const required = option.required ? " (required)" : "";
  const repeatable = option.repeatable ? " (repeatable)" : "";
  return `${names}: ${option.type}${required}${repeatable} — ${option.description}`;
}

function permissionLines(descriptor: CommandDescriptor): string[] {
  const permission = descriptor.permission;
  return [
    `operation: ${permission.operation}`,
    `risk: ${permission.risk}`,
    `interactive approval: ${permission.askInInteractive ? "required" : "not required"}`,
    `sealed mode: ${permission.denyInSealed ? "denied" : "eligible"}`,
    `effects: ${[permission.workspaceMutation && "workspace mutation", permission.networkAccess && "network", permission.processExecution && "process"].filter(Boolean).join(", ") || "none declared"}`,
  ];
}

function handlerLines(descriptor: CommandDescriptor): string[] {
  const handler = descriptor.handler;
  return [
    `kind: ${handler.kind}`,
    `id: ${handler.handlerId}`,
    ...(handler.skillName ? [`skill: ${handler.skillName}`] : []),
    ...(handler.mcpServerId ? [`MCP server: ${handler.mcpServerId}`] : []),
    ...(handler.mcpPromptName ? [`MCP prompt: ${handler.mcpPromptName}`] : []),
    ...(handler.pluginId ? [`plugin: ${handler.pluginId}`] : []),
    ...(handler.controlCommand ? [`control command: ${handler.controlCommand}`] : []),
  ];
}

function synopsisFor(descriptor: CommandDescriptor): string {
  const argumentsText = descriptor.arguments.filter((argument) => argument.positional).map((argument) => `${argument.required ? "<" : "["}${argument.name}${argument.rest ? "..." : ""}${argument.required ? ">" : "]"}`).join(" ");
  return `/${descriptor.name}${argumentsText ? ` ${argumentsText}` : ""}`;
}
