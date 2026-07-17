import { readFile, realpath, stat } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";

import type { JsonObject, JsonValue } from "../contracts.ts";
import {
  canonicalize,
  cloneJson,
  digest,
  normalizeIdentifier,
  normalizeName,
  optionalObject,
} from "../e02/index.ts";
import type {
  PluginAgentDescriptor,
  PluginCommandDescriptor,
  PluginDependency,
  PluginHookDescriptor,
  PluginManifest,
  PluginMcpServerDescriptor,
  PluginSourceKind,
} from "./contracts.ts";

export class PluginManifestRuntime {
  private readonly workspaceRoot: string;
  private readonly allowOutsideWorkspace: boolean;
  private readonly maximumManifestBytes: number;

  constructor(options: { workspaceRoot: string; allowOutsideWorkspace?: boolean; maximumManifestBytes?: number }) {
    this.workspaceRoot = resolve(options.workspaceRoot);
    this.allowOutsideWorkspace = options.allowOutsideWorkspace ?? false;
    this.maximumManifestBytes = options.maximumManifestBytes ?? 1024 * 1024;
  }

  async parse(
    manifestPathValue: string,
    sourceKind: PluginSourceKind,
    overrides: JsonObject = {},
  ): Promise<PluginManifest> {
    const manifestPath = resolve(manifestPathValue);
    const realManifestPath = await realpath(manifestPath);
    const rootPath = dirname(realManifestPath);
    this.assertAllowed(rootPath);
    const metadata = await stat(realManifestPath);
    if (!metadata.isFile()) throw new Error(`plugin manifest ${manifestPath} is not a file`);
    if (metadata.size > this.maximumManifestBytes) throw new Error(`plugin manifest ${manifestPath} exceeds ${this.maximumManifestBytes} bytes`);
    const text = await readFile(realManifestPath, "utf8");
    let raw: JsonObject;
    try {
      raw = JSON.parse(text) as JsonObject;
    } catch (error) {
      throw new Error(`plugin manifest ${manifestPath} is invalid JSON: ${error instanceof Error ? error.message : String(error)}`);
    }
    const value = { ...canonicalize(raw) as JsonObject, ...cloneJson(overrides) };
    const manifestVersion = numberValue(value.manifest_version ?? value.manifestVersion ?? 1, "manifest version");
    if (manifestVersion !== 1) throw new Error(`unsupported plugin manifest version ${manifestVersion}`);
    const pluginId = normalizeIdentifier(requireString(value.id ?? value.plugin_id ?? value.pluginId, "plugin id"), "plugin id");
    const name = normalizeName(stringValue(value.name) || pluginId, 512);
    const version = requireString(value.version, "plugin version");
    if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/.test(version)) throw new Error(`plugin ${pluginId} version ${version} is not semantic version`);
    const dependencies = parseDependencies(value.dependencies);
    const skillRoots = parseSkillRoots(value.skills ?? value.skill_roots ?? value.skillRoots, pluginId, rootPath, sourceKind);
    const commands = parseCommands(value.commands, rootPath);
    const hooks = parseHooks(value.hooks, rootPath);
    const agents = parseAgents(value.agents, rootPath);
    const mcpServers = parseMcpServers(value.mcp ?? value.mcp_servers ?? value.mcpServers, rootPath);
    const manifestBase = {
      manifestVersion: 1 as const,
      pluginId,
      name,
      version,
      description: stringValue(value.description),
      author: nullableString(value.author),
      homepage: nullableString(value.homepage),
      license: nullableString(value.license),
      sourceKind,
      rootPath,
      manifestPath: realManifestPath,
      enabled: value.enabled !== false,
      minimumZyraVersion: nullableString(value.minimum_zyra_version ?? value.minimumZyraVersion),
      dependencies,
      skillRoots,
      commands,
      hooks,
      agents,
      mcpServers,
      environmentHandles: parseStringRecord(value.environment ?? value.env, "plugin environment"),
      permissions: optionalObject(value.permissions),
      metadata: {
        ...optionalObject(value.metadata),
        manifest_size_bytes: metadata.size,
        manifest_modified_at_ms: metadata.mtimeMs,
      },
    };
    return {
      ...manifestBase,
      manifestDigest: digest(manifestBase),
    };
  }

  private assertAllowed(path: string): void {
    if (this.allowOutsideWorkspace) return;
    const normalized = resolve(path).toLowerCase();
    const root = this.workspaceRoot.toLowerCase();
    if (normalized !== root && !normalized.startsWith(`${root}${sep}`)) throw new Error(`plugin path ${path} is outside workspace ${this.workspaceRoot}`);
  }
}

function parseDependencies(value: unknown): PluginDependency[] {
  if (value === undefined || value === null) return [];
  const entries: [string, unknown][] = Array.isArray(value)
    ? value.map((item, index) => [String(index), item])
    : Object.entries(objectValue(value, "plugin dependencies"));
  return entries.map(([key, item], index) => {
    if (typeof item === "string") {
      return { pluginId: normalizeIdentifier(key, "dependency plugin id"), versionRange: item, optional: false, capabilities: [] };
    }
    const object = objectValue(item, `dependency ${index}`);
    return {
      pluginId: normalizeIdentifier(stringValue(object.id ?? object.plugin_id ?? object.pluginId) || key, "dependency plugin id"),
      versionRange: stringValue(object.version ?? object.version_range ?? object.versionRange) || "*",
      optional: object.optional === true,
      capabilities: stringArray(object.capabilities).filter((capability): capability is PluginDependency["capabilities"][number] => ["skill", "command", "hook", "agent", "mcp", "resource"].includes(capability)),
    };
  });
}

function parseSkillRoots(value: unknown, pluginId: string, rootPath: string, sourceKind: PluginSourceKind) {
  const items = arrayOrSingle(value ?? "skills");
  return items.map((item, index) => {
    const object = typeof item === "string" ? { path: item } : objectValue(item, `skill root ${index}`);
    const path = resolveWithin(rootPath, requireString(object.path, `skill root ${index} path`));
    return {
      sourceId: normalizeIdentifier(`${pluginId}:skills:${index + 1}`, "skill source id"),
      kind: "plugin" as const,
      rootPath: path,
      priority: integerValue(object.priority, sourceKind === "managed" ? 500 : sourceKind === "project" ? 300 : 100),
      enabled: object.enabled !== false,
      recursive: object.recursive !== false,
      followSymlinks: object.follow_symlinks === true || object.followSymlinks === true,
      maximumDepth: integerValue(object.maximum_depth ?? object.maximumDepth, 8),
      includePatterns: stringArray(object.include ?? object.include_patterns ?? object.includePatterns),
      excludePatterns: stringArray(object.exclude ?? object.exclude_patterns ?? object.excludePatterns),
      pluginId,
      revision: 1,
      metadata: optionalObject(object.metadata),
    };
  });
}

function parseCommands(value: unknown, rootPath: string): PluginCommandDescriptor[] {
  return arrayOrSingle(value).map((item, index) => {
    const object = typeof item === "string" ? { path: item } : objectValue(item, `command ${index}`);
    const path = resolveWithin(rootPath, requireString(object.path, `command ${index} path`));
    return {
      name: normalizeIdentifier(stringValue(object.name) || basenameWithoutExtension(path), "plugin command name"),
      path,
      aliases: stringArray(object.aliases),
      hidden: object.hidden === true,
      permission: nullableString(object.permission),
      metadata: optionalObject(object.metadata),
    };
  });
}

function parseHooks(value: unknown, rootPath: string): PluginHookDescriptor[] {
  return arrayOrSingle(value).map((item, index) => {
    const object = objectValue(item, `hook ${index}`);
    const pathValue = nullableString(object.path ?? object.module);
    const command = nullableString(object.command);
    if (!pathValue && !command) throw new Error(`plugin hook ${index} requires path or command`);
    return {
      hookId: normalizeIdentifier(stringValue(object.id ?? object.hook_id ?? object.hookId) || `hook-${index + 1}`, "plugin hook id"),
      event: requireString(object.event, `hook ${index} event`),
      path: pathValue ? resolveWithin(rootPath, pathValue) : null,
      command,
      priority: integerValue(object.priority, 0, true),
      timeoutMs: integerValue(object.timeout_ms ?? object.timeoutMs, 30_000),
      failClosed: object.fail_closed !== false && object.failClosed !== false,
      canMutate: object.can_mutate === true || object.canMutate === true,
      toolPatterns: stringArray(object.tools ?? object.tool_patterns ?? object.toolPatterns ?? ["*"]),
      metadata: optionalObject(object.metadata),
    };
  });
}

function parseAgents(value: unknown, rootPath: string): PluginAgentDescriptor[] {
  return arrayOrSingle(value).map((item, index) => {
    const object = typeof item === "string" ? { path: item } : objectValue(item, `agent ${index}`);
    const path = resolveWithin(rootPath, requireString(object.path, `agent ${index} path`));
    return {
      agentId: normalizeIdentifier(stringValue(object.id ?? object.agent_id ?? object.agentId) || basenameWithoutExtension(path), "plugin agent id"),
      path,
      description: stringValue(object.description),
      model: nullableString(object.model),
      toolScope: optionalObject(object.tools ?? object.tool_scope ?? object.toolScope),
      metadata: optionalObject(object.metadata),
    };
  });
}

function parseMcpServers(value: unknown, rootPath: string): PluginMcpServerDescriptor[] {
  const entries: [string, unknown][] = Array.isArray(value)
    ? value.map((item, index) => [String(index), item])
    : value && typeof value === "object"
      ? Object.entries(value as Record<string, unknown>)
      : [];
  return entries.map(([key, item], index) => {
    const object = objectValue(item, `MCP server ${index}`);
    const configPath = nullableString(object.config_path ?? object.configPath);
    return {
      serverId: normalizeIdentifier(stringValue(object.id ?? object.server_id ?? object.serverId) || key, "plugin MCP server id"),
      configPath: configPath ? resolveWithin(rootPath, configPath) : null,
      config: optionalObject(object.config),
      enabledByDefault: object.enabled !== false && object.enabled_by_default !== false && object.enabledByDefault !== false,
      permissionScope: optionalObject(object.permissions ?? object.permission_scope ?? object.permissionScope),
    };
  });
}

function resolveWithin(root: string, path: string): string {
  const target = resolve(root, path);
  const normalized = target.toLowerCase();
  const rootValue = resolve(root).toLowerCase();
  if (normalized !== rootValue && !normalized.startsWith(`${rootValue}${sep}`)) throw new Error(`plugin path ${path} escapes ${root}`);
  return target;
}

function arrayOrSingle(value: unknown): unknown[] {
  if (value === undefined || value === null) return [];
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
  if (value === undefined || value === null) return [];
  if (typeof value === "string") return [value];
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) throw new Error("expected string array");
  return [...new Set(value as string[])];
}

function parseStringRecord(value: unknown, label: string): Record<string, string> {
  if (value === undefined || value === null) return {};
  const object = objectValue(value, label);
  const output: Record<string, string> = {};
  for (const [key, child] of Object.entries(object)) output[key] = requireString(child, `${label}.${key}`);
  return output;
}

function integerValue(value: unknown, fallback: number, allowNegative = false): number {
  if (value === undefined || value === null) return fallback;
  if (!Number.isSafeInteger(value) || (!allowNegative && (value as number) < 0)) throw new Error("expected integer");
  return value as number;
}

function numberValue(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value)) throw new Error(`${label} must be integer`);
  return value as number;
}

function basenameWithoutExtension(path: string): string {
  const name = path.replace(/\\/g, "/").split("/").at(-1) ?? path;
  return name.replace(/\.[^.]+$/, "");
}
