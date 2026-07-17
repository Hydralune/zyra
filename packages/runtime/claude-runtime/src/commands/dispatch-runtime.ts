import type { JsonObject, JsonValue } from "../contracts.ts";
import { canonicalize, cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type {
  CommandArgument,
  CommandDescriptor,
  CommandInvocationRequest,
  CommandInvocationResult,
  CommandOption,
  CommandPermissionDecision,
} from "./contracts.ts";
import { LocalCommandRuntime } from "./local-command-runtime.ts";
import { CommandRegistryRuntime } from "./registry-runtime.ts";

export type CommandPermissionEvaluator = (
  descriptor: CommandDescriptor,
  request: CommandInvocationRequest,
  argumentsValue: JsonObject,
) => Promise<CommandPermissionDecision>;

export interface CommandExternalDispatchers {
  skill(descriptor: CommandDescriptor, request: CommandInvocationRequest, argumentsValue: JsonObject, signal?: AbortSignal): Promise<JsonValue>;
  mcpPrompt(descriptor: CommandDescriptor, request: CommandInvocationRequest, argumentsValue: JsonObject, signal?: AbortSignal): Promise<JsonValue>;
  plugin(descriptor: CommandDescriptor, request: CommandInvocationRequest, argumentsValue: JsonObject, signal?: AbortSignal): Promise<JsonValue>;
  control(descriptor: CommandDescriptor, request: CommandInvocationRequest, argumentsValue: JsonObject, signal?: AbortSignal): Promise<JsonValue>;
  builtin(descriptor: CommandDescriptor, request: CommandInvocationRequest, argumentsValue: JsonObject, signal?: AbortSignal): Promise<JsonValue>;
}

export class CommandDispatchRuntime {
  private readonly registry: CommandRegistryRuntime;
  private readonly local: LocalCommandRuntime;
  private readonly permission: CommandPermissionEvaluator;
  private readonly dispatchers: CommandExternalDispatchers;
  private readonly now: () => Date;
  private readonly results = new Map<string, CommandInvocationResult>();
  private readonly maximumResults: number;
  private lastTimestamp: string | null = null;

  constructor(options: {
    registry: CommandRegistryRuntime;
    local: LocalCommandRuntime;
    permission: CommandPermissionEvaluator;
    dispatchers: CommandExternalDispatchers;
    now?: () => Date;
    maximumResults?: number;
  }) {
    this.registry = options.registry;
    this.local = options.local;
    this.permission = options.permission;
    this.dispatchers = options.dispatchers;
    this.now = options.now ?? (() => new Date());
    this.maximumResults = options.maximumResults ?? 10_000;
  }

  async dispatch(requestValue: CommandInvocationRequest, signal?: AbortSignal): Promise<CommandInvocationResult> {
    const request = normalizeRequest(requestValue);
    const parsed = request.commandName ? { name: request.commandName, argv: request.arguments, options: request.options } : parseCommandLine(request.input);
    const descriptor = this.registry.resolve(parsed.name, request.registryRevision);
    const argumentsValue = bindArguments(descriptor, parsed.argv, parsed.options, request.workspaceRoot);
    const invocationId = deterministicId("command-invocation", {
      run_id: request.identity.runId,
      task_id: request.identity.taskId,
      session_id: request.identity.sessionId,
      session_revision: request.identity.sessionRevision,
      worker_request_id: request.identity.workerRequestId,
      command_call_id: request.identity.commandCallId,
      command_id: descriptor.commandId,
      descriptor_digest: descriptor.descriptorDigest,
      registry_revision: request.registryRevision,
      arguments_digest: digest(argumentsValue),
    }, 32);
    const existing = this.results.get(invocationId);
    if (existing) return cloneJson(existing);
    const release = this.registry.pin(request.registryRevision);
    const startedAt = this.timestamp();
    try {
      const decision = await this.requirePermission(descriptor, request, argumentsValue);
      if (decision.effect === "deny" || decision.effect === "ask") {
        const result: CommandInvocationResult = {
          invocationId,
          commandId: descriptor.commandId,
          commandName: descriptor.name,
          registryRevision: request.registryRevision,
          status: decision.effect === "deny" ? "denied" : "pending_approval",
          output: decision.recoveryInput,
          artifacts: [],
          permission: decision,
          startedAt,
          completedAt: decision.effect === "deny" ? this.timestamp() : null,
          failure: null,
          metadata: {},
        };
        this.remember(result);
        return cloneJson(result);
      }
      const authorizedArguments = authorizedCommandArguments(decision, argumentsValue);
      let output: JsonValue;
      if (descriptor.handler.kind === "local") {
        const local = await this.local.execute(descriptor, request, authorizedArguments, signal);
        output = local.output;
      } else if (descriptor.handler.kind === "skill") {
        output = await this.dispatchers.skill(descriptor, request, authorizedArguments, signal);
      } else if (descriptor.handler.kind === "mcp_prompt") {
        output = await this.dispatchers.mcpPrompt(descriptor, request, authorizedArguments, signal);
      } else if (descriptor.handler.kind === "plugin") {
        output = await this.dispatchers.plugin(descriptor, request, authorizedArguments, signal);
      } else if (descriptor.handler.kind === "control") {
        output = await this.dispatchers.control(descriptor, request, authorizedArguments, signal);
      } else {
        output = await this.dispatchers.builtin(descriptor, request, authorizedArguments, signal);
      }
      const result: CommandInvocationResult = {
        invocationId,
        commandId: descriptor.commandId,
        commandName: descriptor.name,
        registryRevision: request.registryRevision,
        status: "completed",
        output: canonicalize(output),
        artifacts: [],
        permission: decision,
        startedAt,
        completedAt: this.timestamp(),
        failure: null,
        metadata: { handler_kind: descriptor.handler.kind },
      };
      this.remember(result);
      return cloneJson(result);
    } catch (error) {
      const fallbackPermission: CommandPermissionDecision = {
        effect: "deny",
        decisionId: deterministicId("command-failure-decision", { invocation_id: invocationId }),
        reasonCode: "command_execution_failed",
        reason: error instanceof Error ? error.message : String(error),
        requestDigest: digest(request),
        continuationId: null,
        replanRequired: true,
        recoveryInput: { kind: "command_failure", replan_required: true, command_id: descriptor.commandId },
        metadata: {},
      };
      const result: CommandInvocationResult = {
        invocationId,
        commandId: descriptor.commandId,
        commandName: descriptor.name,
        registryRevision: request.registryRevision,
        status: signal?.aborted ? "cancelled" : "failed",
        output: null,
        artifacts: [],
        permission: fallbackPermission,
        startedAt,
        completedAt: this.timestamp(),
        failure: { name: error instanceof Error ? error.name : "Error", message: error instanceof Error ? error.message : String(error) },
        metadata: {},
      };
      this.remember(result);
      throw error;
    } finally {
      release();
    }
  }

  async requirePermission(
    descriptor: CommandDescriptor,
    request: CommandInvocationRequest,
    argumentsValue: JsonObject,
  ): Promise<CommandPermissionDecision> {
    const decision = await this.permission(descriptor, request, argumentsValue);
    const expectedDigest = digest({
      command_id: descriptor.commandId,
      descriptor_digest: descriptor.descriptorDigest,
      request_identity: request.identity,
      arguments: argumentsValue,
      permission: descriptor.permission,
    });
    if (decision.requestDigest !== expectedDigest) throw new Error("command permission decision request digest mismatch");
    if (request.sealedAutonomous && decision.effect === "ask") {
      return {
        ...decision,
        effect: "deny",
        reasonCode: "sealed_command_ask_denied",
        reason: "sealed autonomous command cannot pause for approval",
        continuationId: null,
        replanRequired: true,
        recoveryInput: {
          kind: "command_permission_denial",
          command_id: descriptor.commandId,
          reason_code: "sealed_command_ask_denied",
          replan_required: true,
          e02_plans_or_routes: false,
        },
      };
    }
    return cloneJson(decision);
  }

  get(invocationId: string): CommandInvocationResult | null {
    const value = this.results.get(invocationId);
    return value ? cloneJson(value) : null;
  }

  private remember(result: CommandInvocationResult): void {
    this.results.set(result.invocationId, cloneJson(result));
    while (this.results.size > this.maximumResults) {
      const first = this.results.keys().next().value as string | undefined;
      if (!first) break;
      this.results.delete(first);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function authorizedCommandArguments(
  decision: CommandPermissionDecision,
  original: JsonObject,
): JsonObject {
  const value = decision.metadata.final_arguments;
  if (value === undefined || value === null) return cloneJson(original);
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new Error("command permission final arguments must be an object");
  }
  const normalized = canonicalize(value) as JsonObject;
  const expectedDigest = decision.metadata.final_arguments_digest;
  if (typeof expectedDigest !== "string" || expectedDigest !== digest(normalized)) {
    throw new Error("command permission final arguments digest mismatch");
  }
  return normalized;
}

function normalizeRequest(value: CommandInvocationRequest): CommandInvocationRequest {
  const request = cloneJson(value);
  if (!request.identity.runId || !request.identity.sessionId || !request.identity.commandCallId) throw new Error("command invocation identity is incomplete");
  if (!Number.isSafeInteger(request.identity.sessionRevision) || request.identity.sessionRevision < 0) throw new Error("command session revision is invalid");
  return request;
}

function parseCommandLine(input: string): { name: string; argv: JsonValue[]; options: JsonObject } {
  const tokens = tokenize(input.trim().replace(/^\//, ""));
  if (!tokens.length) throw new Error("command input is empty");
  const name = tokens.shift()!;
  const argv: JsonValue[] = [];
  const options: JsonObject = {};
  while (tokens.length) {
    const token = tokens.shift()!;
    if (token.startsWith("--")) {
      const equals = token.indexOf("=");
      const key = equals >= 0 ? token.slice(2, equals) : token.slice(2);
      const inline = equals >= 0 ? token.slice(equals + 1) : null;
      const next = inline ?? (tokens[0] && !tokens[0].startsWith("-") ? tokens.shift()! : "true");
      options[key] = parseLooseValue(next);
    } else if (/^-[A-Za-z0-9]$/.test(token)) {
      const key = token.slice(1);
      const next = tokens[0] && !tokens[0].startsWith("-") ? tokens.shift()! : "true";
      options[key] = parseLooseValue(next);
    } else {
      argv.push(parseLooseValue(token));
    }
  }
  return { name, argv, options };
}

function tokenize(value: string): string[] {
  const output: string[] = [];
  let current = "";
  let quote: string | null = null;
  let escaping = false;
  for (const char of value) {
    if (escaping) {
      current += char;
      escaping = false;
    } else if (char === "\\") {
      escaping = true;
    } else if (quote) {
      if (char === quote) quote = null;
      else current += char;
    } else if (char === "\"" || char === "'") {
      quote = char;
    } else if (/\s/.test(char)) {
      if (current) output.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  if (quote) throw new Error("unterminated command quote");
  if (escaping) current += "\\";
  if (current) output.push(current);
  return output;
}

function bindArguments(
  descriptor: CommandDescriptor,
  argv: JsonValue[],
  optionsValue: JsonObject,
  workspaceRoot: string,
): JsonObject {
  const output: JsonObject = {};
  let offset = 0;
  for (const argument of descriptor.arguments.filter((value) => value.positional)) {
    const raw = argument.rest ? argv.slice(offset) : argv[offset++];
    const value = raw === undefined || (Array.isArray(raw) && !raw.length) ? argument.defaultValue : raw;
    if ((value === undefined || value === null) && argument.required) throw new Error(`command ${descriptor.name} requires ${argument.name}`);
    if (value !== undefined && value !== null) output[argument.name] = validateValue(argument, value, workspaceRoot);
  }
  if (offset < argv.length && !descriptor.arguments.some((argument) => argument.rest)) throw new Error(`command ${descriptor.name} received too many arguments`);
  for (const option of descriptor.options) {
    const shortValue = option.short ? optionsValue[option.short] : undefined;
    const raw = optionsValue[option.name] ?? shortValue ?? option.defaultValue;
    if ((raw === undefined || raw === null) && option.required) throw new Error(`command ${descriptor.name} requires --${option.name}`);
    if (raw !== undefined && raw !== null) output[option.name] = validateOptionValue(option, raw, workspaceRoot);
  }
  const known = new Set(descriptor.options.flatMap((option) => [option.name, ...(option.short ? [option.short] : [])]));
  for (const key of Object.keys(optionsValue)) if (!known.has(key)) throw new Error(`command ${descriptor.name} does not support option ${key}`);
  return output;
}

function validateValue(argument: CommandArgument, value: JsonValue, workspaceRoot: string): JsonValue {
  if (argument.rest && Array.isArray(value)) return value.map((item) => validateScalar(argument.type, argument.enumValues, item, workspaceRoot));
  return validateScalar(argument.type, argument.enumValues, value, workspaceRoot);
}

function validateOptionValue(option: CommandOption, value: JsonValue, workspaceRoot: string): JsonValue {
  if (option.repeatable && Array.isArray(value)) return value.map((item) => validateScalar(option.type, option.enumValues, item, workspaceRoot));
  return validateScalar(option.type, option.enumValues, value, workspaceRoot);
}

function validateScalar(type: CommandArgument["type"], enumValues: JsonValue[], value: JsonValue, _workspaceRoot: string): JsonValue {
  if (type === "string" || type === "path") {
    if (typeof value !== "string") value = String(value);
    if (type === "path" && (value.startsWith("/") || value.replace(/\\/g, "/").includes("../"))) throw new Error("command path must be workspace-relative");
  } else if (type === "number" || type === "integer") {
    if (typeof value === "string" && /^-?\d+(?:\.\d+)?$/.test(value)) value = Number(value);
    if (typeof value !== "number" || !Number.isFinite(value) || (type === "integer" && !Number.isSafeInteger(value))) throw new Error(`command value must be ${type}`);
  } else if (type === "boolean") {
    if (value === "true") value = true;
    if (value === "false") value = false;
    if (typeof value !== "boolean") throw new Error("command value must be boolean");
  } else if (type === "json" && typeof value === "string") {
    value = JSON.parse(value) as JsonValue;
  }
  if (type === "enum" && !enumValues.some((candidate) => digest(candidate) === digest(value))) throw new Error("command value is outside enum");
  return canonicalize(value);
}

function parseLooseValue(value: string): JsonValue {
  if (value === "true") return true;
  if (value === "false") return false;
  if (value === "null") return null;
  if (/^-?\d+(?:\.\d+)?$/.test(value)) return Number(value);
  if (value.startsWith("{") || value.startsWith("[")) {
    try { return canonicalize(JSON.parse(value)); } catch { return value; }
  }
  return value;
}
