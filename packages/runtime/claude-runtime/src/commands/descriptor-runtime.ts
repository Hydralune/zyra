import { readFile } from "node:fs/promises";
import { basename, resolve } from "node:path";

import type { JsonObject, JsonValue } from "../contracts.ts";
import { canonicalize, digest, normalizeIdentifier, normalizeName, optionalObject } from "../e02/index.ts";
import type {
  CommandArgument,
  CommandDescriptor,
  CommandHandlerDescriptor,
  CommandOption,
  CommandPermissionDescriptor,
  CommandSourceKind,
} from "./contracts.ts";

export class CommandDescriptorRuntime {
  async parse(input: {
    path: string;
    sourceKind: CommandSourceKind;
    sourceId: string;
    sourcePriority: number;
    pluginId?: string | null;
    overrides?: JsonObject;
  }): Promise<CommandDescriptor> {
    const path = resolve(input.path);
    const text = await readFile(path, "utf8");
    return this.parseText({
      text,
      path,
      sourceKind: input.sourceKind,
      sourceId: input.sourceId,
      sourcePriority: input.sourcePriority,
      pluginId: input.pluginId ?? null,
      overrides: input.overrides ?? {},
    });
  }

  parseText(input: {
    text: string;
    path: string | null;
    sourceKind: CommandSourceKind;
    sourceId: string;
    sourcePriority: number;
    pluginId?: string | null;
    overrides?: JsonObject;
  }): CommandDescriptor {
    const text = input.text.replace(/\r\n?/g, "\n");
    const { frontmatter, body } = splitFrontmatter(text);
    const parsed = { ...parseFlatYaml(frontmatter), ...(input.overrides ?? {}) };
    const fallbackName = input.path ? basename(input.path).replace(/\.[^.]+$/, "") : "command";
    const name = normalizeIdentifier(stringValue(parsed.name) || fallbackName, "command name");
    const handler = parseHandler(parsed, name, input.pluginId ?? null);
    const permission = parsePermission(parsed.permission, handler);
    const descriptorBase = {
      commandId: normalizeIdentifier(stringValue(parsed.id ?? parsed.command_id ?? parsed.commandId) || `${input.sourceId}:${name}`, "command id"),
      name,
      aliases: stringArray(parsed.aliases).map((alias) => normalizeIdentifier(alias, "command alias")),
      displayName: normalizeName(stringValue(parsed.display_name ?? parsed.displayName) || name, 512),
      description: stringValue(parsed.description) || firstParagraph(body),
      usage: stringValue(parsed.usage) || `/${name}`,
      examples: stringArray(parsed.examples),
      category: stringValue(parsed.category) || "general",
      sourceKind: input.sourceKind,
      sourceId: input.sourceId,
      sourcePath: input.path,
      sourcePriority: input.sourcePriority,
      hidden: parsed.hidden === true,
      enabled: parsed.enabled !== false,
      arguments: parseArguments(parsed.arguments ?? parsed.args),
      options: parseOptions(parsed.options),
      handler,
      permission,
      body: body.trim(),
      metadata: optionalObject(parsed.metadata),
    };
    validatePositionals(descriptorBase.arguments);
    validateOptions(descriptorBase.options);
    return {
      ...descriptorBase,
      descriptorDigest: digest(descriptorBase),
    };
  }
}

function parseHandler(value: JsonObject, commandName: string, pluginId: string | null): CommandHandlerDescriptor {
  const handlerValue = value.handler;
  const object = typeof handlerValue === "string"
    ? { kind: handlerValue }
    : handlerValue && typeof handlerValue === "object" && !Array.isArray(handlerValue)
      ? handlerValue as JsonObject
      : {};
  const kind = stringValue(object.kind ?? value.kind) || (pluginId ? "plugin" : "local");
  if (!["builtin", "local", "skill", "mcp_prompt", "plugin", "control"].includes(kind)) throw new Error(`unsupported command handler kind ${kind}`);
  return {
    kind: kind as CommandHandlerDescriptor["kind"],
    handlerId: normalizeIdentifier(stringValue(object.id ?? object.handler_id ?? object.handlerId) || `${kind}:${commandName}`, "command handler id"),
    modulePath: nullableString(object.module ?? object.module_path ?? object.modulePath),
    skillName: nullableString(object.skill ?? object.skill_name ?? object.skillName),
    mcpServerId: nullableString(object.server ?? object.mcp_server_id ?? object.mcpServerId),
    mcpPromptName: nullableString(object.prompt ?? object.mcp_prompt_name ?? object.mcpPromptName),
    pluginId: nullableString(object.plugin ?? object.plugin_id ?? object.pluginId) ?? pluginId,
    controlCommand: nullableString(object.control ?? object.control_command ?? object.controlCommand),
    metadata: optionalObject(object.metadata),
  };
}

function parsePermission(value: unknown, handler: CommandHandlerDescriptor): CommandPermissionDescriptor {
  const object = value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {};
  const risk = stringValue(object.risk) || defaultRisk(handler.kind);
  if (!["low", "medium", "high", "critical"].includes(risk)) throw new Error(`unsupported command risk ${risk}`);
  return {
    operation: stringValue(object.operation) || `command/${handler.kind}`,
    risk: risk as CommandPermissionDescriptor["risk"],
    askInInteractive: object.ask_in_interactive !== false && object.askInInteractive !== false && risk !== "low",
    denyInSealed: object.deny_in_sealed !== false && object.denyInSealed !== false && (risk === "high" || risk === "critical"),
    workspaceMutation: object.workspace_mutation === true || object.workspaceMutation === true,
    networkAccess: object.network_access === true || object.networkAccess === true,
    processExecution: object.process_execution === true || object.processExecution === true || handler.kind === "local",
    scope: optionalObject(object.scope),
  };
}

function parseArguments(value: unknown): CommandArgument[] {
  const items = arrayValue(value);
  return items.map((item, index) => {
    const object = typeof item === "string" ? { name: item } : objectValue(item, `command argument ${index}`);
    const type = stringValue(object.type) || "string";
    if (!["string", "number", "integer", "boolean", "json", "path", "enum"].includes(type)) throw new Error(`unsupported command argument type ${type}`);
    return {
      name: normalizeIdentifier(requireString(object.name, `command argument ${index} name`), "command argument name"),
      description: stringValue(object.description),
      required: object.required === true,
      positional: object.positional !== false,
      rest: object.rest === true,
      type: type as CommandArgument["type"],
      defaultValue: object.default === undefined ? null : canonicalize(object.default),
      enumValues: arrayValue(object.enum ?? object.values).map(canonicalize),
    };
  });
}

function parseOptions(value: unknown): CommandOption[] {
  const items = arrayValue(value);
  return items.map((item, index) => {
    const object = objectValue(item, `command option ${index}`);
    const type = stringValue(object.type) || "boolean";
    if (!["string", "number", "integer", "boolean", "json", "path", "enum"].includes(type)) throw new Error(`unsupported command option type ${type}`);
    return {
      name: normalizeIdentifier(requireString(object.name, `command option ${index} name`), "command option name"),
      short: nullableString(object.short),
      description: stringValue(object.description),
      type: type as CommandOption["type"],
      required: object.required === true,
      repeatable: object.repeatable === true,
      defaultValue: object.default === undefined ? null : canonicalize(object.default),
      enumValues: arrayValue(object.enum ?? object.values).map(canonicalize),
    };
  });
}

function validatePositionals(argumentsValue: CommandArgument[]): void {
  let optionalSeen = false;
  let restSeen = false;
  for (const argument of argumentsValue.filter((value) => value.positional)) {
    if (restSeen) throw new Error(`argument ${argument.name} appears after rest argument`);
    if (!argument.required) optionalSeen = true;
    if (argument.required && optionalSeen) throw new Error(`required argument ${argument.name} appears after optional argument`);
    if (argument.rest) restSeen = true;
  }
}

function validateOptions(options: CommandOption[]): void {
  const names = new Set<string>();
  const shorts = new Set<string>();
  for (const option of options) {
    if (names.has(option.name)) throw new Error(`duplicate command option ${option.name}`);
    names.add(option.name);
    if (option.short) {
      if (!/^[A-Za-z0-9]$/.test(option.short)) throw new Error(`command option ${option.name} short name must be one character`);
      if (shorts.has(option.short)) throw new Error(`duplicate command short option ${option.short}`);
      shorts.add(option.short);
    }
  }
}

function splitFrontmatter(text: string): { frontmatter: string; body: string } {
  if (!text.startsWith("---\n")) return { frontmatter: "", body: text };
  const end = text.indexOf("\n---\n", 4);
  if (end < 0) throw new Error("command frontmatter is not terminated");
  return { frontmatter: text.slice(4, end), body: text.slice(end + 5) };
}

function parseFlatYaml(text: string): JsonObject {
  const output: JsonObject = {};
  for (const [index, raw] of text.split("\n").entries()) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const colon = line.indexOf(":");
    if (colon <= 0) throw new Error(`invalid command frontmatter line ${index + 1}`);
    const key = line.slice(0, colon).trim();
    const value = line.slice(colon + 1).trim();
    output[key] = parseScalar(value);
  }
  return output;
}

function parseScalar(value: string): JsonValue {
  if (!value) return "";
  if (value === "true") return true;
  if (value === "false") return false;
  if (value === "null") return null;
  if (/^-?\d+(?:\.\d+)?$/.test(value)) return Number(value);
  if (value.startsWith("[") || value.startsWith("{")) {
    try { return canonicalize(JSON.parse(value)); } catch { return value; }
  }
  return value.replace(/^['"]|['"]$/g, "");
}

function firstParagraph(body: string): string {
  return body.split(/\n\s*\n/)[0]?.replace(/^#+\s*/, "").trim() ?? "";
}

function defaultRisk(kind: CommandHandlerDescriptor["kind"]): CommandPermissionDescriptor["risk"] {
  if (kind === "builtin" || kind === "control") return "low";
  if (kind === "skill" || kind === "mcp_prompt") return "medium";
  return "high";
}

function arrayValue(value: unknown): unknown[] {
  if (value === undefined || value === null || value === "") return [];
  return Array.isArray(value) ? value : [value];
}

function objectValue(value: unknown, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} must be object`);
  return canonicalize(value) as JsonObject;
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function requireString(value: unknown, label: string): string {
  const output = stringValue(value);
  if (!output) throw new Error(`${label} is required`);
  return output;
}

function nullableString(value: unknown): string | null {
  return stringValue(value) || null;
}

function stringArray(value: unknown): string[] {
  if (value === undefined || value === null || value === "") return [];
  if (typeof value === "string") return value.split(",").map((item) => item.trim()).filter(Boolean);
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) throw new Error("expected string array");
  return [...new Set(value as string[])];
}
