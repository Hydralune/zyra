import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

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
  SkillArgumentDescriptor,
  SkillContextPolicy,
  SkillDescriptor,
  SkillExecutionPolicy,
  SkillHookDescriptor,
  SkillResourceDescriptor,
  SkillSourceFile,
  SkillToolScope,
} from "./contracts-v2.ts";

export interface SkillFrontmatterParseOptions {
  maximumBodyBytes?: number;
  maximumFrontmatterBytes?: number;
  now?: () => Date;
}

export class SkillFrontmatterRuntime {
  private readonly maximumBodyBytes: number;
  private readonly maximumFrontmatterBytes: number;
  private readonly now: () => Date;

  constructor(options: SkillFrontmatterParseOptions = {}) {
    this.maximumBodyBytes = options.maximumBodyBytes ?? 4 * 1024 * 1024;
    this.maximumFrontmatterBytes = options.maximumFrontmatterBytes ?? 512 * 1024;
    this.now = options.now ?? (() => new Date());
  }

  async parse(source: SkillSourceFile): Promise<SkillDescriptor> {
    const bytes = await readFile(source.realPath);
    if (bytes.byteLength > this.maximumBodyBytes) throw new Error(`skill ${source.manifestPath} exceeds ${this.maximumBodyBytes} bytes`);
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes).replace(/^\uFEFF/, "");
    return this.parseText(source, text);
  }

  parseText(source: SkillSourceFile, textValue: string): SkillDescriptor {
    const text = textValue.replace(/\r\n?/g, "\n");
    const split = splitFrontmatter(text, this.maximumFrontmatterBytes);
    const frontmatter = parseYamlSubset(split.frontmatter);
    const warnings: string[] = [];
    const name = requireString(frontmatter.name, "skill name");
    const skillId = normalizeIdentifier(
      stringValue(frontmatter.id) || `${source.pluginId ? `${source.pluginId}:` : ""}${name}`,
      "skill id",
    );
    const displayName = stringValue(frontmatter.display_name ?? frontmatter.displayName) || name;
    const description = stringValue(frontmatter.description) || firstParagraph(split.body);
    if (!description) warnings.push("skill has no description");
    const argumentsValue = parseArguments(frontmatter.arguments ?? frontmatter.args, warnings);
    const toolScope = parseToolScope(frontmatter.tools ?? frontmatter.tool_scope ?? frontmatter.toolScope);
    const context = parseContext(frontmatter.context);
    const execution = parseExecution(frontmatter.execution ?? frontmatter.invocation ?? frontmatter.agent);
    const resources = parseResources(frontmatter.resources, source, warnings);
    const hooks = parseHooks(frontmatter.hooks, warnings);
    const body = split.body.trim();
    if (!body) warnings.push("skill body is empty");
    const environmentHandles = stringRecord(frontmatter.environment ?? frontmatter.env, "environment");
    const known = new Set([
      "id", "name", "display_name", "displayName", "description", "version", "license", "author",
      "tags", "aliases", "arguments", "args", "tools", "tool_scope", "toolScope", "context",
      "execution", "invocation", "agent", "resources", "hooks", "environment", "env", "enabled", "metadata",
    ]);
    const unknown: JsonObject = {};
    for (const [key, value] of Object.entries(frontmatter)) if (!known.has(key)) unknown[key] = canonicalize(value);
    if (Object.keys(unknown).length) warnings.push(`unknown frontmatter keys: ${Object.keys(unknown).sort().join(", ")}`);
    const frontmatterDigest = digest(frontmatter);
    const bodyDigest = digest(body);
    const descriptorBase = {
      skillId,
      name: normalizeName(name, 256),
      displayName: normalizeName(displayName, 512),
      description,
      version: stringValue(frontmatter.version) || "0.0.0",
      license: nullableString(frontmatter.license),
      author: nullableString(frontmatter.author),
      tags: stringArray(frontmatter.tags),
      aliases: stringArray(frontmatter.aliases),
      source: cloneJson(source),
      availability: frontmatter.enabled === false ? "disabled" as const : "available" as const,
      disabledReason: frontmatter.enabled === false ? "disabled_by_frontmatter" : null,
      body,
      bodyDigest,
      frontmatterDigest,
      arguments: argumentsValue,
      toolScope,
      context,
      execution,
      resources,
      hooks,
      environmentHandles,
      metadata: {
        ...(optionalObject(frontmatter.metadata) as JsonObject),
        unknown_frontmatter: unknown,
      },
      warnings,
      parsedAt: this.now().toISOString(),
    };
    return {
      ...descriptorBase,
      descriptorDigest: digest(descriptorBase),
    };
  }
}

function splitFrontmatter(text: string, maximumBytes: number): { frontmatter: string; body: string } {
  if (!text.startsWith("---\n")) return { frontmatter: "", body: text };
  const end = text.indexOf("\n---\n", 4);
  if (end < 0) throw new Error("skill frontmatter is not terminated with ---");
  const frontmatter = text.slice(4, end);
  if (Buffer.byteLength(frontmatter, "utf8") > maximumBytes) throw new Error(`skill frontmatter exceeds ${maximumBytes} bytes`);
  return { frontmatter, body: text.slice(end + 5) };
}

function parseYamlSubset(text: string): JsonObject {
  if (!text.trim()) return {};
  const lines = text.split("\n");
  const root: JsonObject = {};
  const stack: { indent: number; value: JsonObject | JsonValue[]; key: string | null }[] = [{ indent: -1, value: root, key: null }];
  for (let index = 0; index < lines.length; index += 1) {
    const raw = lines[index];
    if (!raw.trim() || raw.trimStart().startsWith("#")) continue;
    if (/\t/.test(raw.slice(0, raw.length - raw.trimStart().length))) throw new Error(`tabs are not allowed in frontmatter indentation at line ${index + 1}`);
    const indent = raw.length - raw.trimStart().length;
    const content = stripComment(raw.trim());
    while (stack.length > 1 && indent <= stack.at(-1)!.indent) stack.pop();
    const parent = stack.at(-1)!;
    if (content.startsWith("- ") || content === "-") {
      if (!Array.isArray(parent.value)) throw new Error(`unexpected YAML list item at line ${index + 1}`);
      const item = content.slice(1).trim();
      if (!item) {
        const child: JsonObject = {};
        parent.value.push(child);
        stack.push({ indent, value: child, key: null });
      } else if (/^[^:]+:\s*/.test(item)) {
        const [key, rest] = splitKey(item, index + 1);
        const child: JsonObject = {};
        child[key] = rest ? parseScalar(rest) : {};
        parent.value.push(child);
        stack.push({ indent, value: child, key });
      } else {
        parent.value.push(parseScalar(item));
      }
      continue;
    }
    if (Array.isArray(parent.value)) throw new Error(`mapping entry cannot be placed directly in list at line ${index + 1}`);
    const [key, rest] = splitKey(content, index + 1);
    if (rest) {
      parent.value[key] = parseScalar(rest);
      continue;
    }
    const next = nextContent(lines, index + 1);
    const child: JsonObject | JsonValue[] = next?.trimStart().startsWith("-") ? [] : {};
    parent.value[key] = child;
    stack.push({ indent, value: child, key });
  }
  return root;
}

function parseArguments(value: unknown, warnings: string[]): SkillArgumentDescriptor[] {
  if (value === undefined || value === null) return [];
  const entries: [string, JsonObject][] = [];
  if (Array.isArray(value)) {
    for (const [index, item] of value.entries()) {
      if (typeof item === "string") entries.push([item, {}]);
      else {
        const object = objectValue(item, `argument ${index}`);
        entries.push([requireString(object.name, `argument ${index} name`), object]);
      }
    }
  } else {
    const object = objectValue(value, "arguments");
    for (const [name, options] of Object.entries(object)) entries.push([name, typeof options === "object" && options !== null && !Array.isArray(options) ? options as JsonObject : { default: canonicalize(options) }]);
  }
  const seen = new Set<string>();
  return entries.map(([nameValue, options], index) => {
    const name = normalizeIdentifier(nameValue, "skill argument name");
    if (seen.has(name)) throw new Error(`duplicate skill argument ${name}`);
    seen.add(name);
    const type = stringValue(options.type) || "string";
    if (!["string", "number", "integer", "boolean", "json", "path", "enum"].includes(type)) throw new Error(`unsupported type ${type} for skill argument ${name}`);
    const enumValues = arrayValue(options.enum ?? options.values).map(canonicalize);
    if (type === "enum" && !enumValues.length) warnings.push(`enum argument ${name} has no values`);
    return {
      name,
      description: stringValue(options.description),
      required: options.required === true,
      type: type as SkillArgumentDescriptor["type"],
      defaultValue: options.default === undefined ? null : canonicalize(options.default),
      enumValues,
      minimum: finiteNumber(options.minimum),
      maximum: finiteNumber(options.maximum),
      pattern: nullableString(options.pattern),
      positional: options.positional === true,
      rest: options.rest === true,
      sensitive: options.sensitive === true,
    };
  });
}

function parseToolScope(value: unknown): SkillToolScope {
  const object = value === undefined || value === null ? {} : objectValue(value, "tool scope");
  return {
    allowed: stringArray(object.allowed ?? object.allow ?? ["*"]),
    denied: stringArray(object.denied ?? object.deny),
    namespaces: stringArray(object.namespaces ?? ["*"]),
    mcpServers: stringArray(object.mcp_servers ?? object.mcpServers ?? ["*"]),
    readOnly: object.read_only === true || object.readOnly === true,
    inheritParent: object.inherit_parent !== false && object.inheritParent !== false,
    maximumCalls: integerOrNull(object.maximum_calls ?? object.maximumCalls),
    maximumParallel: integerValue(object.maximum_parallel ?? object.maximumParallel, 1),
    requireApproval: stringArray(object.require_approval ?? object.requireApproval),
  };
}

function parseContext(value: unknown): SkillContextPolicy {
  const object = value === undefined || value === null ? {} : objectValue(value, "context policy");
  return {
    inheritConversation: object.inherit_conversation !== false && object.inheritConversation !== false,
    inheritSystem: object.inherit_system !== false && object.inheritSystem !== false,
    inheritMemory: object.inherit_memory !== false && object.inheritMemory !== false,
    includeWorkspaceInstructions: object.include_workspace_instructions !== false && object.includeWorkspaceInstructions !== false,
    includeMcpInstructions: object.include_mcp_instructions !== false && object.includeMcpInstructions !== false,
    includeFiles: stringArray(object.include_files ?? object.includeFiles),
    excludeFiles: stringArray(object.exclude_files ?? object.excludeFiles),
    maximumInputTokens: integerValue(object.maximum_input_tokens ?? object.maximumInputTokens, 32_000),
    maximumResourceTokens: integerValue(object.maximum_resource_tokens ?? object.maximumResourceTokens, 16_000),
    maximumOutputTokens: integerValue(object.maximum_output_tokens ?? object.maximumOutputTokens, 8_000),
    compactionStrategy: parseCompaction(stringValue(object.compaction_strategy ?? object.compactionStrategy) || "truncate_resources"),
  };
}

function parseExecution(value: unknown): SkillExecutionPolicy {
  const object = typeof value === "string" ? { agent: value } : value === undefined || value === null ? {} : objectValue(value, "execution policy");
  const mode = stringValue(object.mode) || "inline";
  if (mode !== "inline" && mode !== "fork" && mode !== "background") throw new Error(`unsupported skill execution mode ${mode}`);
  const sandbox = stringValue(object.sandbox) || "inherit";
  if (!["inherit", "workspace_read", "workspace_write", "isolated"].includes(sandbox)) throw new Error(`unsupported skill sandbox ${sandbox}`);
  return {
    mode,
    agent: nullableString(object.agent),
    model: nullableString(object.model),
    maximumSkillDepth: skillDepthValue(
      object["max-skill-depth"]
        ?? object.max_skill_depth
        ?? object.maximum_skill_depth
        ?? object.maximumSkillDepth,
    ),
    timeoutMs: integerValue(object.timeout_ms ?? object.timeoutMs, 300_000),
    maximumTurns: integerValue(object.maximum_turns ?? object.maximumTurns, 32),
    maximumCostMicros: integerOrNull(object.maximum_cost_micros ?? object.maximumCostMicros),
    workingDirectory: nullableString(object.working_directory ?? object.workingDirectory),
    sandbox: sandbox as SkillExecutionPolicy["sandbox"],
    permissionMode: nullableString(object.permission_mode ?? object.permissionMode),
    allowNetwork: object.allow_network === true || object.allowNetwork === true,
    persistTranscript: object.persist_transcript !== false && object.persistTranscript !== false,
    persistArtifacts: object.persist_artifacts !== false && object.persistArtifacts !== false,
  };
}

function skillDepthValue(value: unknown): number {
  if (value === undefined || value === null || value === "") return 0;
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0 || parsed > 8) {
    throw new Error("skill maximum depth must be an integer from 0 to 8");
  }
  return parsed;
}

function parseResources(value: unknown, source: SkillSourceFile, warnings: string[]): SkillResourceDescriptor[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new Error("skill resources must be an array");
  return value.map((item, index) => {
    const object = typeof item === "string" ? { path: item } : objectValue(item, `resource ${index}`);
    const path = requireString(object.path, `resource ${index} path`);
    if (resolve(source.skillDirectory, path) === resolve(source.manifestPath)) warnings.push(`resource ${path} references the skill manifest itself`);
    const kind = stringValue(object.kind) || inferResourceKind(path);
    if (!["markdown", "text", "json", "yaml", "image", "binary"].includes(kind)) throw new Error(`unsupported skill resource kind ${kind}`);
    return {
      resourceId: normalizeIdentifier(stringValue(object.id) || `${source.sourceId}:${path}`, "skill resource id"),
      path,
      kind: kind as SkillResourceDescriptor["kind"],
      required: object.required !== false,
      maximumBytes: integerValue(object.maximum_bytes ?? object.maximumBytes, 2 * 1024 * 1024),
      charset: nullableString(object.charset),
      mediaType: nullableString(object.media_type ?? object.mediaType),
      digest: nullableString(object.digest),
      metadata: optionalObject(object.metadata),
    };
  });
}

function parseHooks(value: unknown, warnings: string[]): SkillHookDescriptor[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new Error("skill hooks must be an array");
  return value.map((item, index) => {
    const object = objectValue(item, `hook ${index}`);
    const event = requireString(object.event, `hook ${index} event`);
    if (!["before_invoke", "after_invoke", "before_tool", "after_tool", "on_failure"].includes(event)) throw new Error(`unsupported skill hook event ${event}`);
    const command = nullableString(object.command);
    const module = nullableString(object.module);
    if (!command && !module) warnings.push(`hook ${index} has neither command nor module`);
    return {
      hookId: normalizeIdentifier(stringValue(object.id) || `hook-${index + 1}`, "skill hook id"),
      event: event as SkillHookDescriptor["event"],
      command,
      module,
      timeoutMs: integerValue(object.timeout_ms ?? object.timeoutMs, 30_000),
      failClosed: object.fail_closed !== false && object.failClosed !== false,
      mutationAllowed: object.mutation_allowed === true || object.mutationAllowed === true,
      priority: integerValue(object.priority, 0, true),
      metadata: optionalObject(object.metadata),
    };
  });
}

function firstParagraph(value: string): string {
  return value.split(/\n\s*\n/)[0]?.replace(/^#+\s*/, "").trim() ?? "";
}

function splitKey(value: string, line: number): [string, string] {
  const colon = value.indexOf(":");
  if (colon <= 0) throw new Error(`invalid YAML mapping at line ${line}`);
  const key = value.slice(0, colon).trim();
  if (!/^[A-Za-z_][A-Za-z0-9_.-]*$/.test(key)) throw new Error(`invalid YAML key ${key} at line ${line}`);
  return [key, value.slice(colon + 1).trim()];
}

function stripComment(value: string): string {
  let quote: string | null = null;
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if ((char === "\"" || char === "'") && value[index - 1] !== "\\") quote = quote === char ? null : quote ?? char;
    if (char === "#" && !quote && (index === 0 || /\s/.test(value[index - 1]))) return value.slice(0, index).trimEnd();
  }
  return value;
}

function parseScalar(value: string): JsonValue {
  if ((value.startsWith("\"") && value.endsWith("\"")) || (value.startsWith("'") && value.endsWith("'"))) return value.slice(1, -1).replace(/\\n/g, "\n").replace(/\\t/g, "\t");
  if (value === "true") return true;
  if (value === "false") return false;
  if (value === "null" || value === "~") return null;
  if (/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)) return Number(value);
  if ((value.startsWith("[") && value.endsWith("]")) || (value.startsWith("{") && value.endsWith("}"))) {
    try { return canonicalize(JSON.parse(value.replace(/'/g, "\""))); } catch { /* retain as string */ }
  }
  return value;
}

function nextContent(lines: string[], start: number): string | null {
  for (let index = start; index < lines.length; index += 1) if (lines[index].trim() && !lines[index].trimStart().startsWith("#")) return lines[index];
  return null;
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
  if (typeof value === "string") return value.split(",").map((item) => item.trim()).filter(Boolean);
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) throw new Error("expected string array");
  return [...new Set(value.map((item) => item.trim()).filter(Boolean))];
}

function stringRecord(value: unknown, label: string): Record<string, string> {
  if (value === undefined || value === null) return {};
  const object = objectValue(value, label);
  const output: Record<string, string> = {};
  for (const [key, child] of Object.entries(object)) output[key] = requireString(child, `${label}.${key}`);
  return output;
}

function objectValue(value: unknown, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} must be an object`);
  return canonicalize(value) as JsonObject;
}

function arrayValue(value: unknown): JsonValue[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new Error("expected array");
  return value.map(canonicalize);
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function integerOrNull(value: unknown): number | null {
  if (value === undefined || value === null) return null;
  return integerValue(value, 0);
}

function integerValue(value: unknown, fallback: number, allowNegative = false): number {
  if (value === undefined || value === null) return fallback;
  if (!Number.isSafeInteger(value) || (!allowNegative && (value as number) < 0)) throw new Error("expected integer");
  return value as number;
}

function parseCompaction(value: string): SkillContextPolicy["compactionStrategy"] {
  if (value === "reject" || value === "truncate_resources" || value === "compact_parent") return value;
  throw new Error(`unsupported context compaction strategy ${value}`);
}

function inferResourceKind(path: string): SkillResourceDescriptor["kind"] {
  if (/\.md$/i.test(path)) return "markdown";
  if (/\.json$/i.test(path)) return "json";
  if (/\.ya?ml$/i.test(path)) return "yaml";
  if (/\.(?:png|jpe?g|gif|webp|bmp)$/i.test(path)) return "image";
  if (/\.(?:txt|csv|tsv|xml|html|css|js|ts|py|rs)$/i.test(path)) return "text";
  return "binary";
}
