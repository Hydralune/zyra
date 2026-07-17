import type { JsonObject } from "../contracts.ts";
import {
  canonicalize,
  deterministicId,
  normalizePattern,
  optionalObject,
} from "../e02/canonical.ts";
import type {
  PermissionEffect,
  PermissionRuleRecord,
  PermissionRuleSource,
  PermissionScope,
  PermissionScopeKind,
} from "../e02/contracts.ts";
import { defaultScope, normalizeToolName } from "./model.ts";

export interface PermissionRuleParseDefaults {
  effect?: PermissionEffect;
  source?: PermissionRuleSource;
  priority?: number;
  kind?: PermissionScopeKind;
  now?: string;
  revision?: number;
  metadata?: JsonObject;
}
const VALID_EFFECTS = new Set<PermissionEffect>(["allow", "deny", "ask"]);
const VALID_SOURCES = new Set<PermissionRuleSource>([
  "managed",
  "policy",
  "user",
  "project",
  "workspace",
  "plugin",
  "command",
  "session",
  "standing-grant",
]);
const VALID_KINDS = new Set<PermissionScopeKind>([
  "tool",
  "command",
  "resource",
  "server",
  "session",
  "workspace",
]);

export class PermissionRuleParser {
  parse(value: string | JsonObject, defaults: PermissionRuleParseDefaults = {}): PermissionRuleRecord {
    const raw = typeof value === "string" ? this.parseString(value) : canonicalize(value) as JsonObject;
    const now = defaults.now ?? new Date().toISOString();
    const effect = parseEffect(raw.effect ?? defaults.effect ?? "ask");
    const source = parseSource(raw.source ?? defaults.source ?? "session");
    const kind = parseKind(raw.kind ?? optionalObject(raw.scope).kind ?? defaults.kind ?? "tool");
    const scope = this.parseScope(raw, kind);
    const priority = parseInteger(raw.priority ?? defaults.priority ?? 0, "priority", -1_000_000, 1_000_000);
    const revision = parseInteger(raw.revision ?? defaults.revision ?? 0, "revision", 0, Number.MAX_SAFE_INTEGER);
    const maxUses = raw.maxUses ?? raw.max_uses;
    const useCount = parseInteger(raw.useCount ?? raw.use_count ?? 0, "useCount", 0, Number.MAX_SAFE_INTEGER);
    const expiresAt = parseNullableTimestamp(raw.expiresAt ?? raw.expires_at);
    const ruleShape = {
      effect,
      source,
      scope,
      priority,
      expiresAt,
      maxUses: maxUses === null || maxUses === undefined
        ? null
        : parseInteger(maxUses, "maxUses", 1, Number.MAX_SAFE_INTEGER),
    };
    const ruleId = stringOrEmpty(raw.ruleId ?? raw.rule_id)
      || deterministicId("permission-rule", ruleShape, 32);
    return {
      ruleId,
      effect,
      source,
      scope,
      priority,
      enabled: raw.enabled !== false,
      reason: stringOrEmpty(raw.reason) || `${effect} by ${source} ${kind} rule`,
      expiresAt,
      maxUses: ruleShape.maxUses,
      useCount,
      revision,
      createdAt: parseTimestamp(raw.createdAt ?? raw.created_at, now),
      updatedAt: parseTimestamp(raw.updatedAt ?? raw.updated_at, now),
      metadata: {
        ...canonicalize(defaults.metadata ?? {}) as JsonObject,
        ...optionalObject(raw.metadata),
      },
    };
  }

  parseMany(values: readonly (string | JsonObject)[], defaults: PermissionRuleParseDefaults = {}): PermissionRuleRecord[] {
    const records = values.map((value) => this.parse(value, defaults));
    const byId = new Map<string, PermissionRuleRecord>();
    for (const record of records) {
      const existing = byId.get(record.ruleId);
      if (existing && this.serialize(existing) !== this.serialize(record)) {
        throw new Error(`conflicting permission rule id ${record.ruleId}`);
      }
      byId.set(record.ruleId, record);
    }
    return [...byId.values()];
  }

  serialize(rule: PermissionRuleRecord): string {
    const scope = rule.scope;
    const selector = serializeSelector(scope);
    const attributes: string[] = [];
    if (rule.source !== "session") attributes.push(`source=${escapeToken(rule.source)}`);
    if (rule.priority) attributes.push(`priority=${rule.priority}`);
    if (rule.expiresAt) attributes.push(`expires=${escapeToken(rule.expiresAt)}`);
    if (rule.maxUses !== null) attributes.push(`maxUses=${rule.maxUses}`);
    if (!rule.enabled) attributes.push("enabled=false");
    if (rule.reason) attributes.push(`reason=${quote(rule.reason)}`);
    return `${rule.effect}:${scope.kind}:${selector}${attributes.length ? `;${attributes.join(";")}` : ""}`;
  }

  normalize(value: string | JsonObject, defaults: PermissionRuleParseDefaults = {}): string {
    return this.serialize(this.parse(value, defaults));
  }

  roundTrip(value: string | JsonObject, defaults: PermissionRuleParseDefaults = {}): PermissionRuleRecord {
    const first = this.parse(value, defaults);
    const second = this.parse(this.serialize(first), {
      ...defaults,
      now: first.createdAt,
      revision: first.revision,
      metadata: first.metadata,
    });
    return {
      ...second,
      ruleId: first.ruleId,
      createdAt: first.createdAt,
      updatedAt: first.updatedAt,
      useCount: first.useCount,
    };
  }

  private parseString(value: string): JsonObject {
    const text = value.trim();
    if (!text) throw new Error("permission rule string is empty");
    if (text.startsWith("{") && text.endsWith("}")) {
      const parsed = JSON.parse(text) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("permission rule JSON must be an object");
      return canonicalize(parsed) as JsonObject;
    }
    const colon = splitEscaped(text, ":", 3);
    if (colon.length >= 3 && VALID_EFFECTS.has(colon[0] as PermissionEffect) && VALID_KINDS.has(colon[1] as PermissionScopeKind)) {
      const [selector, ...attributes] = splitEscaped(colon.slice(2).join(":"), ";");
      return {
        effect: colon[0],
        kind: colon[1],
        ...parseSelector(colon[1] as PermissionScopeKind, selector ?? "*"),
        ...parseAttributes(attributes),
      };
    }
    const legacy = parseLegacyToolRule(text);
    if (legacy) return legacy;
    throw new Error(`unsupported permission rule syntax: ${text}`);
  }

  private parseScope(raw: JsonObject, kind: PermissionScopeKind): PermissionScope {
    const nested = optionalObject(raw.scope);
    const read = (...keys: string[]): string => {
      for (const key of keys) {
        const direct = raw[key];
        if (typeof direct === "string") return normalizePattern(direct);
        const child = nested[key];
        if (typeof child === "string") return normalizePattern(child);
      }
      return "*";
    };
    const scope = defaultScope(kind);
    scope.toolPattern = normalizeToolPattern(read("toolPattern", "tool_pattern", "tool"));
    scope.namespacePattern = read("namespacePattern", "namespace_pattern", "namespace");
    scope.serverPattern = read("serverPattern", "server_pattern", "server_id", "server");
    scope.commandPattern = read("commandPattern", "command_pattern", "command");
    scope.resourcePattern = read("resourcePattern", "resource_pattern", "resource_uri", "resource");
    scope.operationPattern = read("operationPattern", "operation_pattern", "operation");
    scope.workspacePattern = read("workspacePattern", "workspace_pattern", "workspace_root", "workspace");
    scope.sessionPattern = read("sessionPattern", "session_pattern", "session_id", "session");
    scope.argumentPattern = read("argumentPattern", "argument_pattern", "arguments");
    return scope;
  }
}

function parseLegacyToolRule(text: string): JsonObject | null {
  const match = /^([A-Za-z_][A-Za-z0-9_.:-]*)(?:\((.*)\))?$/.exec(text);
  if (!match) return null;
  const tool = normalizeToolName(match[1]!);
  const rawArgument = match[2];
  const argumentPattern = rawArgument === undefined || rawArgument === "" || rawArgument === "*"
    ? "*"
    : unescapeToken(rawArgument);
  return {
    effect: "allow",
    kind: "tool",
    toolPattern: tool,
    argumentPattern,
    source: "session",
  };
}

function parseSelector(kind: PermissionScopeKind, selector: string): JsonObject {
  const values = splitEscaped(selector, ",").map(unescapeToken);
  if (kind === "tool") {
    return {
      toolPattern: values[0] || "*",
      namespacePattern: values[1] || "*",
      operationPattern: values[2] || "*",
      argumentPattern: values[3] || "*",
    };
  }
  if (kind === "command") return { commandPattern: values[0] || "*", argumentPattern: values[1] || "*" };
  if (kind === "resource") return { resourcePattern: values[0] || "*", serverPattern: values[1] || "*" };
  if (kind === "server") return { serverPattern: values[0] || "*", namespacePattern: values[1] || "*" };
  if (kind === "session") return { sessionPattern: values[0] || "*" };
  return { workspacePattern: values[0] || "*" };
}

function serializeSelector(scope: PermissionScope): string {
  const values = scope.kind === "tool"
    ? [scope.toolPattern, scope.namespacePattern, scope.operationPattern, scope.argumentPattern]
    : scope.kind === "command"
      ? [scope.commandPattern, scope.argumentPattern]
      : scope.kind === "resource"
        ? [scope.resourcePattern, scope.serverPattern]
        : scope.kind === "server"
          ? [scope.serverPattern, scope.namespacePattern]
          : scope.kind === "session"
            ? [scope.sessionPattern]
            : [scope.workspacePattern];
  while (values.length > 1 && values.at(-1) === "*") values.pop();
  return values.map(escapeToken).join(",");
}

function parseAttributes(values: readonly string[]): JsonObject {
  const output: JsonObject = {};
  for (const value of values) {
    if (!value) continue;
    const [rawKey, ...rawValue] = splitEscaped(value, "=", 2);
    const key = rawKey?.trim();
    if (!key || !rawValue.length) throw new Error(`invalid permission rule attribute ${value}`);
    const child = unquote(rawValue.join("=").trim());
    if (key === "priority" || key === "maxUses") output[key] = Number(child);
    else if (key === "enabled") output[key] = child !== "false";
    else if (key === "expires") output.expiresAt = child;
    else output[key] = child;
  }
  return output;
}

function splitEscaped(value: string, separator: string, maximum = Number.POSITIVE_INFINITY): string[] {
  const output: string[] = [];
  let current = "";
  let escaped = false;
  let quoteCharacter = "";
  for (const character of value) {
    if (escaped) {
      current += character;
      escaped = false;
      continue;
    }
    if (character === "\\") {
      escaped = true;
      current += character;
      continue;
    }
    if ((character === "\"" || character === "'") && (!quoteCharacter || quoteCharacter === character)) {
      quoteCharacter = quoteCharacter ? "" : character;
      current += character;
      continue;
    }
    if (!quoteCharacter && character === separator && output.length < maximum - 1) {
      output.push(current);
      current = "";
      continue;
    }
    current += character;
  }
  if (escaped) throw new Error("permission rule ends with an incomplete escape");
  if (quoteCharacter) throw new Error("permission rule contains an unterminated quote");
  output.push(current);
  return output;
}

function normalizeToolPattern(value: string): string {
  if (value === "*") return value;
  return value.includes("*") || value.includes("?") ? value : normalizeToolName(value);
}

function parseEffect(value: unknown): PermissionEffect {
  if (typeof value !== "string" || !VALID_EFFECTS.has(value as PermissionEffect)) throw new Error(`invalid permission effect ${String(value)}`);
  return value as PermissionEffect;
}

function parseSource(value: unknown): PermissionRuleSource {
  if (typeof value !== "string" || !VALID_SOURCES.has(value as PermissionRuleSource)) throw new Error(`invalid permission rule source ${String(value)}`);
  return value as PermissionRuleSource;
}

function parseKind(value: unknown): PermissionScopeKind {
  if (typeof value !== "string" || !VALID_KINDS.has(value as PermissionScopeKind)) throw new Error(`invalid permission scope kind ${String(value)}`);
  return value as PermissionScopeKind;
}

function parseInteger(value: unknown, label: string, minimum: number, maximum: number): number {
  if (!Number.isSafeInteger(value) || (value as number) < minimum || (value as number) > maximum) {
    throw new Error(`${label} must be an integer in [${minimum}, ${maximum}]`);
  }
  return value as number;
}

function parseNullableTimestamp(value: unknown): string | null {
  if (value === undefined || value === null || value === "") return null;
  return parseTimestamp(value, "");
}

function parseTimestamp(value: unknown, fallback: string): string {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value !== "string" || Number.isNaN(Date.parse(value))) throw new Error(`invalid timestamp ${String(value)}`);
  return new Date(value).toISOString();
}

function stringOrEmpty(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function escapeToken(value: string): string {
  return value.replace(/[\\,:;=()]/g, (character) => `\\${character}`);
}

function unescapeToken(value: string): string {
  return value.replace(/\\(.)/g, "$1");
}

function quote(value: string): string {
  return JSON.stringify(value);
}

function unquote(value: string): string {
  if ((value.startsWith("\"") && value.endsWith("\"")) || (value.startsWith("'") && value.endsWith("'"))) {
    return value.startsWith("\"") ? JSON.parse(value) as string : value.slice(1, -1).replace(/\\'/g, "'");
  }
  return unescapeToken(value);
}
