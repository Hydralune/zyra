import type { JsonObject, JsonValue } from "../contracts.ts";
import { cloneJson, deterministicId, digest } from "../e02/index.ts";
import type { CommandArgument, CommandDescriptor, CommandOption } from "./contracts.ts";
import { CommandRegistryRuntime } from "./registry-runtime.ts";

export interface CommandCompletionRequest {
  input: string;
  cursor: number;
  registryRevision: number;
  workspacePaths?: string[];
  dynamicValues?: Record<string, JsonValue[]>;
  includeHidden?: boolean;
  maximumResults?: number;
  metadata?: JsonObject;
}

export interface CommandCompletionItem {
  itemId: string;
  value: string;
  display: string;
  description: string;
  kind: "command" | "alias" | "option" | "argument" | "enum" | "path" | "dynamic";
  replacementStart: number;
  replacementEnd: number;
  commandId: string | null;
  score: number;
  metadata: JsonObject;
}

export interface CommandCompletionResult {
  completionId: string;
  input: string;
  cursor: number;
  registryRevision: number;
  commandName: string | null;
  tokenIndex: number;
  items: CommandCompletionItem[];
  digest: string;
  metadata: JsonObject;
}

export class CommandCompletionRuntime {
  private readonly registry: CommandRegistryRuntime;

  constructor(registry: CommandRegistryRuntime) {
    this.registry = registry;
  }

  complete(requestValue: CommandCompletionRequest): CommandCompletionResult {
    const request = normalizeRequest(requestValue);
    const beforeCursor = request.input.slice(0, request.cursor);
    const parsed = tokenizeWithOffsets(beforeCursor.replace(/^\//, ""));
    const current = parsed.at(-1) ?? { value: "", start: beforeCursor.length, end: beforeCursor.length };
    const tokenIndex = Math.max(0, parsed.length - 1);
    const commandToken = parsed[0]?.value ?? "";
    const commands = this.registry.list({ revision: request.registryRevision }).filter((descriptor) => request.includeHidden || !descriptor.hidden);
    let descriptor: CommandDescriptor | null = null;
    if (commandToken) {
      try { descriptor = this.registry.resolve(commandToken, request.registryRevision); } catch { }
    }
    const items = descriptor && (parsed.length > 1 || beforeCursor.endsWith(" "))
      ? this.completeArguments(descriptor, parsed, current, request)
      : this.completeCommands(commands, commandToken || current.value, current.start + (request.input.startsWith("/") ? 1 : 0), current.end + (request.input.startsWith("/") ? 1 : 0));
    const capped = items.sort((left, right) => right.score - left.score || left.value.localeCompare(right.value)).slice(0, request.maximumResults);
    const base = {
      input: request.input,
      cursor: request.cursor,
      registryRevision: request.registryRevision,
      commandName: descriptor?.name ?? null,
      tokenIndex,
      items: capped,
      metadata: cloneJson(request.metadata ?? {}),
    };
    const completionId = deterministicId("command-completion", { input_digest: digest(request.input), cursor: request.cursor, registry_revision: request.registryRevision }, 32);
    return { completionId, ...base, digest: digest({ completionId, ...base }) };
  }

  private completeCommands(commands: CommandDescriptor[], prefix: string, start: number, end: number): CommandCompletionItem[] {
    const normalized = prefix.toLowerCase();
    const output: CommandCompletionItem[] = [];
    for (const descriptor of commands) {
      if (descriptor.name.toLowerCase().startsWith(normalized)) output.push(item(descriptor.name, descriptor.displayName, descriptor.description, "command", start, end, descriptor.commandId, descriptor.name === prefix ? 100 : 80));
      for (const alias of descriptor.aliases) if (alias.toLowerCase().startsWith(normalized)) output.push(item(alias, alias, `Alias for /${descriptor.name}`, "alias", start, end, descriptor.commandId, alias === prefix ? 95 : 70));
    }
    return output;
  }

  private completeArguments(descriptor: CommandDescriptor, tokens: { value: string; start: number; end: number }[], current: { value: string; start: number; end: number }, request: Required<CommandCompletionRequest>): CommandCompletionItem[] {
    const output: CommandCompletionItem[] = [];
    const offset = request.input.startsWith("/") ? 1 : 0;
    const usedOptions = new Set(tokens.slice(1).filter((token) => token.value.startsWith("--")).map((token) => token.value.replace(/^--/, "").split("=")[0]));
    if (current.value.startsWith("-")) {
      for (const option of descriptor.options) {
        if (usedOptions.has(option.name) && !option.repeatable) continue;
        const long = `--${option.name}`;
        if (long.startsWith(current.value)) output.push(item(long, long, option.description, "option", current.start + offset, current.end + offset, descriptor.commandId, 80));
        if (option.short) {
          const short = `-${option.short}`;
          if (short.startsWith(current.value)) output.push(item(short, short, option.description, "option", current.start + offset, current.end + offset, descriptor.commandId, 70));
        }
      }
      return output;
    }
    const option = optionAwaitingValue(descriptor, tokens);
    if (option) return this.valuesFor(option, current, request, descriptor.commandId, offset);
    const positionalTokens = tokens.slice(1).filter((token) => token.value && !token.value.startsWith("-"));
    const argument = descriptor.arguments.filter((value) => value.positional)[Math.max(0, positionalTokens.length - (current.value ? 1 : 0))];
    if (argument) return this.valuesFor(argument, current, request, descriptor.commandId, offset);
    return output;
  }

  private valuesFor(descriptor: CommandArgument | CommandOption, current: { value: string; start: number; end: number }, request: Required<CommandCompletionRequest>, commandId: string, offset: number): CommandCompletionItem[] {
    const values: { value: string; kind: CommandCompletionItem["kind"] }[] = [];
    for (const value of descriptor.enumValues) values.push({ value: typeof value === "string" ? value : JSON.stringify(value), kind: "enum" });
    if (descriptor.type === "boolean") values.push({ value: "true", kind: "argument" }, { value: "false", kind: "argument" });
    if (descriptor.type === "path") for (const path of request.workspacePaths) values.push({ value: path, kind: "path" });
    for (const value of request.dynamicValues[descriptor.name] ?? []) values.push({ value: typeof value === "string" ? value : JSON.stringify(value), kind: "dynamic" });
    return [...new Map(values.map((value) => [value.value, value])).values()]
      .filter((value) => value.value.toLowerCase().startsWith(current.value.toLowerCase()))
      .map((value) => item(value.value, value.value, descriptor.description, value.kind, current.start + offset, current.end + offset, commandId, value.value === current.value ? 100 : 60));
  }
}

function normalizeRequest(value: CommandCompletionRequest): Required<CommandCompletionRequest> {
  const request = cloneJson(value) as Required<CommandCompletionRequest>;
  request.cursor = Math.max(0, Math.min(request.cursor, request.input.length));
  request.workspacePaths = request.workspacePaths ?? [];
  request.dynamicValues = request.dynamicValues ?? {};
  request.includeHidden = request.includeHidden ?? false;
  request.maximumResults = Math.max(1, Math.min(request.maximumResults ?? 50, 500));
  request.metadata = request.metadata ?? {};
  return request;
}

function tokenizeWithOffsets(value: string): { value: string; start: number; end: number }[] {
  const output: { value: string; start: number; end: number }[] = [];
  let current = "";
  let start = 0;
  let quote: string | null = null;
  let escaping = false;
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if (!current && !quote && !/\s/.test(char)) start = index;
    if (escaping) { current += char; escaping = false; }
    else if (char === "\\") escaping = true;
    else if (quote) { if (char === quote) quote = null; else current += char; }
    else if (char === "\"" || char === "'") quote = char;
    else if (/\s/.test(char)) { if (current) { output.push({ value: current, start, end: index }); current = ""; } }
    else current += char;
  }
  if (current || value.endsWith(" ")) output.push({ value: current, start: current ? start : value.length, end: value.length });
  return output;
}

function optionAwaitingValue(descriptor: CommandDescriptor, tokens: { value: string }[]): CommandOption | null {
  const last = tokens.at(-1)?.value ?? "";
  const prior = tokens.at(-2)?.value ?? "";
  if (last.startsWith("--") && last.includes("=")) return descriptor.options.find((option) => option.name === last.slice(2).split("=")[0]) ?? null;
  if (prior.startsWith("--")) return descriptor.options.find((option) => option.name === prior.slice(2)) ?? null;
  if (/^-[A-Za-z0-9]$/.test(prior)) return descriptor.options.find((option) => option.short === prior.slice(1)) ?? null;
  return null;
}

function item(value: string, display: string, description: string, kind: CommandCompletionItem["kind"], replacementStart: number, replacementEnd: number, commandId: string | null, score: number): CommandCompletionItem {
  const base = { value, display, description, kind, replacementStart, replacementEnd, commandId, score, metadata: {} };
  return { itemId: deterministicId("command-completion-item", base, 32), ...base };
}
