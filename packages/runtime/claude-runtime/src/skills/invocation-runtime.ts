import type { JsonObject, JsonValue } from "../contracts.ts";
import {
  canonicalize,
  cloneJson,
  deterministicId,
  digest,
  monotonicNow,
  wildcardMatches,
} from "../e02/index.ts";
import type {
  SkillArgumentDescriptor,
  SkillDescriptor,
  SkillInvocationPlan,
  SkillInvocationRequest,
  SkillInvocationResult,
  SkillResourceContent,
  SkillToolScope,
} from "./contracts-v2.ts";
import { SkillRegistryRuntime } from "./registry-runtime.ts";
import { SkillResourceRuntime } from "./resource-runtime.ts";

export interface SkillExecutorContext {
  plan: SkillInvocationPlan;
  signal?: AbortSignal;
  recordToolCall(toolName: string, argumentsValue: JsonObject): void;
  assertToolAllowed(toolName: string, namespace: string, serverId: string, readOnly: boolean): void;
}

export type SkillExecutor = (context: SkillExecutorContext) => Promise<{
  output: JsonValue;
  artifacts?: JsonObject[];
  inputTokens?: number;
  outputTokens?: number;
  costMicros?: number;
  metadata?: JsonObject;
}>;

export interface SkillInvocationRuntimeOptions {
  registry: SkillRegistryRuntime;
  resources: SkillResourceRuntime;
  executor: SkillExecutor;
  now?: () => Date;
  maximumConcurrentInvocations?: number;
  maximumRecordedResults?: number;
}

interface InFlightSkill {
  plan: SkillInvocationPlan;
  promise: Promise<SkillInvocationResult>;
  releaseRevision: () => void;
}

export class SkillInvocationRuntime {
  private readonly registry: SkillRegistryRuntime;
  private readonly resources: SkillResourceRuntime;
  private readonly executor: SkillExecutor;
  private readonly now: () => Date;
  private readonly maximumConcurrentInvocations: number;
  private readonly maximumRecordedResults: number;
  private readonly inFlight = new Map<string, InFlightSkill>();
  private readonly results = new Map<string, SkillInvocationResult>();
  private lastTimestamp: string | null = null;

  constructor(options: SkillInvocationRuntimeOptions) {
    this.registry = options.registry;
    this.resources = options.resources;
    this.executor = options.executor;
    this.now = options.now ?? (() => new Date());
    this.maximumConcurrentInvocations = options.maximumConcurrentInvocations ?? 64;
    this.maximumRecordedResults = options.maximumRecordedResults ?? 10_000;
  }

  async invoke(requestValue: SkillInvocationRequest, signal?: AbortSignal): Promise<SkillInvocationResult> {
    const request = cloneJson(requestValue);
    validateIdentity(request);
    const expectedInvocationId = deterministicId("skill-invocation", {
      run_id: request.identity.runId,
      task_id: request.identity.taskId,
      session_id: request.identity.sessionId,
      session_revision: request.identity.sessionRevision,
      worker_request_id: request.identity.workerRequestId,
      tool_call_id: request.identity.toolCallId,
      skill_name: request.skillName,
      registry_revision: request.registryRevision,
      arguments_digest: digest(request.arguments),
    }, 32);
    if (request.identity.invocationId && request.identity.invocationId !== expectedInvocationId) {
      throw invocationError("invocation_id_mismatch", `skill invocation id ${request.identity.invocationId} does not match canonical ${expectedInvocationId}`);
    }
    request.identity.invocationId = expectedInvocationId;
    const priorResult = this.results.get(expectedInvocationId);
    if (priorResult) return cloneJson(priorResult);
    const existing = this.inFlight.get(expectedInvocationId);
    if (existing) return existing.promise;
    if (this.inFlight.size >= this.maximumConcurrentInvocations) throw invocationError("skill_concurrency_exceeded", `skill runtime already has ${this.inFlight.size} invocations`);
    const resolution = this.registry.resolve(request.skillName, request.registryRevision);
    const releaseRevision = this.registry.pin(request.registryRevision);
    try {
      const plan = await this.createPlan(request, resolution.descriptor);
      const promise = this.executePlan(plan, signal);
      this.inFlight.set(expectedInvocationId, { plan, promise, releaseRevision });
      try {
        return await promise;
      } finally {
        this.inFlight.delete(expectedInvocationId);
        releaseRevision();
      }
    } catch (error) {
      releaseRevision();
      throw error;
    }
  }

  applyContext(
    descriptor: SkillDescriptor,
    parentContext: JsonObject,
    resources: SkillResourceContent[],
    argumentsValue: JsonObject,
  ): { context: JsonObject; inputTokenEstimate: number; resourceTokenEstimate: number } {
    const policy = descriptor.context;
    const output: JsonObject = {
      version: "zyra.skill-context/v2",
      skill_id: descriptor.skillId,
      skill_name: descriptor.name,
      descriptor_digest: descriptor.descriptorDigest,
      arguments: cloneJson(argumentsValue),
    };
    if (policy.inheritConversation && parentContext.conversation !== undefined) output.conversation = cloneJson(parentContext.conversation);
    if (policy.inheritSystem && parentContext.system !== undefined) output.system = cloneJson(parentContext.system);
    if (policy.inheritMemory && parentContext.memory !== undefined) output.memory = cloneJson(parentContext.memory);
    if (policy.includeWorkspaceInstructions && parentContext.workspace_instructions !== undefined) output.workspace_instructions = cloneJson(parentContext.workspace_instructions);
    if (policy.includeMcpInstructions && parentContext.mcp_instructions !== undefined) output.mcp_instructions = cloneJson(parentContext.mcp_instructions);
    const selectedResources = resources.map((resource) => ({
      resource_id: resource.resourceId,
      path: resource.path,
      kind: resource.kind,
      media_type: resource.mediaType,
      digest: resource.digest,
      text: resource.text,
      bytes_base64: resource.bytesBase64,
      token_estimate: resource.tokenEstimate,
    }));
    let resourceTokenEstimate = resources.reduce((total, resource) => total + resource.tokenEstimate, 0);
    if (resourceTokenEstimate > policy.maximumResourceTokens) {
      if (policy.compactionStrategy === "reject") throw invocationError("skill_resource_budget_exceeded", `skill resources require ${resourceTokenEstimate} tokens, limit is ${policy.maximumResourceTokens}`);
      if (policy.compactionStrategy === "compact_parent") {
        output.compaction_required = true;
        output.compaction_reason = "skill_resource_budget";
      }
      while (resourceTokenEstimate > policy.maximumResourceTokens && selectedResources.length) {
        const removed = selectedResources.pop()!;
        resourceTokenEstimate -= Number(removed.token_estimate ?? 0);
      }
      output.resource_truncation = {
        original_count: resources.length,
        retained_count: selectedResources.length,
        budget: policy.maximumResourceTokens,
      };
    }
    output.resources = canonicalize(selectedResources);
    let inputTokenEstimate = estimateTokens(JSON.stringify(output));
    if (inputTokenEstimate > policy.maximumInputTokens) {
      if (policy.compactionStrategy === "compact_parent") {
        delete output.conversation;
        delete output.memory;
        output.parent_context_compacted = true;
        inputTokenEstimate = estimateTokens(JSON.stringify(output));
      }
      if (inputTokenEstimate > policy.maximumInputTokens) throw invocationError("skill_input_budget_exceeded", `skill input requires ${inputTokenEstimate} tokens, limit is ${policy.maximumInputTokens}`);
    }
    return { context: output, inputTokenEstimate, resourceTokenEstimate };
  }

  applyToolScope(parent: SkillToolScope, child: SkillToolScope): SkillToolScope {
    const allowed = child.inheritParent
      ? intersectPatterns(parent.allowed, child.allowed)
      : [...child.allowed];
    const denied = [...new Set([...(child.inheritParent ? parent.denied : []), ...child.denied])];
    const namespaces = child.inheritParent
      ? intersectPatterns(parent.namespaces, child.namespaces)
      : [...child.namespaces];
    const mcpServers = child.inheritParent
      ? intersectPatterns(parent.mcpServers, child.mcpServers)
      : [...child.mcpServers];
    const parentCalls = parent.maximumCalls ?? Number.MAX_SAFE_INTEGER;
    const childCalls = child.maximumCalls ?? Number.MAX_SAFE_INTEGER;
    const maximumCalls = Math.min(parentCalls, childCalls);
    return {
      allowed: allowed.length ? allowed : [],
      denied,
      namespaces: namespaces.length ? namespaces : [],
      mcpServers: mcpServers.length ? mcpServers : [],
      readOnly: parent.readOnly || child.readOnly,
      inheritParent: false,
      maximumCalls: maximumCalls === Number.MAX_SAFE_INTEGER ? null : maximumCalls,
      maximumParallel: Math.min(parent.maximumParallel, child.maximumParallel),
      requireApproval: [...new Set([...parent.requireApproval, ...child.requireApproval])],
    };
  }

  assertToolAllowed(
    scope: SkillToolScope,
    calls: number,
    toolName: string,
    namespace: string,
    serverId: string,
    readOnly: boolean,
  ): void {
    if (scope.maximumCalls !== null && calls >= scope.maximumCalls) throw invocationError("skill_tool_call_budget_exceeded", `skill tool call budget ${scope.maximumCalls} is exhausted`);
    if (scope.readOnly && !readOnly) throw invocationError("skill_read_only_violation", `skill tool scope permits only read-only tools, rejected ${toolName}`);
    if (scope.denied.some((pattern) => wildcardMatches(pattern, toolName))) throw invocationError("skill_tool_denied", `skill tool scope denies ${toolName}`);
    if (!scope.allowed.some((pattern) => wildcardMatches(pattern, toolName))) throw invocationError("skill_tool_not_allowed", `skill tool scope does not allow ${toolName}`);
    if (!scope.namespaces.some((pattern) => wildcardMatches(pattern, namespace))) throw invocationError("skill_namespace_not_allowed", `skill tool namespace ${namespace} is not allowed`);
    if (namespace === "mcp" && !scope.mcpServers.some((pattern) => wildcardMatches(pattern, serverId))) throw invocationError("skill_mcp_server_not_allowed", `skill MCP server ${serverId} is not allowed`);
  }

  getResult(invocationId: string): SkillInvocationResult | null {
    const value = this.results.get(invocationId);
    return value ? cloneJson(value) : null;
  }

  inFlightPlans(): SkillInvocationPlan[] {
    return [...this.inFlight.values()].map((value) => cloneJson(value.plan));
  }

  private async createPlan(request: SkillInvocationRequest, descriptor: SkillDescriptor): Promise<SkillInvocationPlan> {
    const argumentsValue = validateArguments(descriptor, request.arguments, request.workspaceRoot);
    const resources = await this.resources.load(descriptor);
    const renderedBody = renderBody(descriptor.body, argumentsValue);
    const effectiveToolScope = this.applyToolScope(request.parentToolScope, descriptor.toolScope);
    const contextResult = this.applyContext(descriptor, request.parentContext, resources, argumentsValue);
    const createdAt = this.timestamp();
    return {
      invocationId: request.identity.invocationId,
      skillId: descriptor.skillId,
      skillName: descriptor.name,
      registryRevision: request.registryRevision,
      descriptorDigest: descriptor.descriptorDigest,
      identity: cloneJson(request.identity),
      arguments: argumentsValue,
      renderedBody,
      resources,
      context: contextResult.context,
      effectiveToolScope,
      execution: cloneJson(descriptor.execution),
      inputTokenEstimate: contextResult.inputTokenEstimate + estimateTokens(renderedBody),
      resourceTokenEstimate: contextResult.resourceTokenEstimate,
      createdAt,
      metadata: {
        ...cloneJson(request.metadata),
        maximum_output_tokens: descriptor.context.maximumOutputTokens,
      },
    };
  }

  private async executePlan(plan: SkillInvocationPlan, signal?: AbortSignal): Promise<SkillInvocationResult> {
    const startedAt = this.timestamp();
    let calls = 0;
    try {
      const response = await this.executor({
        plan: cloneJson(plan),
        signal,
        recordToolCall: (_toolName, _argumentsValue) => {
          calls += 1;
          if (plan.effectiveToolScope.maximumCalls !== null && calls > plan.effectiveToolScope.maximumCalls) throw invocationError("skill_tool_call_budget_exceeded", "skill executor exceeded tool call budget");
        },
        assertToolAllowed: (toolName, namespace, serverId, readOnly) => {
          this.assertToolAllowed(plan.effectiveToolScope, calls, toolName, namespace, serverId, readOnly);
        },
      });
      const result: SkillInvocationResult = {
        invocationId: plan.invocationId,
        skillId: plan.skillId,
        status: plan.execution.mode === "background" ? "background" : "completed",
        output: canonicalize(response.output),
        artifacts: (response.artifacts ?? []).map(cloneJson),
        toolCalls: calls,
        inputTokens: response.inputTokens ?? plan.inputTokenEstimate,
        outputTokens: response.outputTokens ?? estimateTokens(JSON.stringify(response.output)),
        costMicros: response.costMicros ?? 0,
        startedAt,
        completedAt: plan.execution.mode === "background" ? null : this.timestamp(),
        failure: null,
        metadata: cloneJson(response.metadata ?? {}),
      };
      const maximumOutputTokens = typeof plan.metadata.maximum_output_tokens === "number"
        ? plan.metadata.maximum_output_tokens
        : 8_000;
      if (result.outputTokens > maximumOutputTokens) {
        throw invocationError("skill_output_budget_exceeded", "skill executor output exceeded configured budget");
      }
      if (plan.execution.maximumCostMicros !== null && result.costMicros > plan.execution.maximumCostMicros) {
        throw invocationError("skill_cost_budget_exceeded", `skill cost ${result.costMicros} exceeds ${plan.execution.maximumCostMicros}`);
      }
      this.remember(result);
      return cloneJson(result);
    } catch (error) {
      const result: SkillInvocationResult = {
        invocationId: plan.invocationId,
        skillId: plan.skillId,
        status: signal?.aborted ? "cancelled" : "failed",
        output: null,
        artifacts: [],
        toolCalls: calls,
        inputTokens: plan.inputTokenEstimate,
        outputTokens: 0,
        costMicros: 0,
        startedAt,
        completedAt: this.timestamp(),
        failure: {
          name: error instanceof Error ? error.name : "Error",
          message: error instanceof Error ? error.message : String(error),
        },
        metadata: {},
      };
      this.remember(result);
      throw error;
    }
  }

  private remember(result: SkillInvocationResult): void {
    this.results.set(result.invocationId, cloneJson(result));
    while (this.results.size > this.maximumRecordedResults) {
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

function validateIdentity(request: SkillInvocationRequest): void {
  for (const [key, value] of Object.entries(request.identity)) {
    if (key === "sessionRevision") {
      if (!Number.isSafeInteger(value) || (value as number) < 0) throw invocationError("invalid_skill_identity", "sessionRevision must be non-negative integer");
    } else if (key !== "invocationId" && (typeof value !== "string" || !value)) {
      throw invocationError("invalid_skill_identity", `${key} is required`);
    }
  }
}

function validateArguments(descriptor: SkillDescriptor, input: JsonObject, workspaceRoot: string): JsonObject {
  const output: JsonObject = {};
  const allowed = new Set(descriptor.arguments.map((argument) => argument.name));
  for (const key of Object.keys(input)) if (!allowed.has(key)) throw invocationError("unknown_skill_argument", `skill ${descriptor.name} does not accept ${key}`);
  for (const argument of descriptor.arguments) {
    const raw = input[argument.name] ?? argument.defaultValue;
    if ((raw === undefined || raw === null) && argument.required) throw invocationError("required_skill_argument_missing", `skill ${descriptor.name} requires ${argument.name}`);
    if (raw === undefined || raw === null) continue;
    output[argument.name] = validateArgument(argument, raw, workspaceRoot);
  }
  return output;
}

function validateArgument(argument: SkillArgumentDescriptor, value: JsonValue, workspaceRoot: string): JsonValue {
  if (argument.type === "string" || argument.type === "path") {
    if (typeof value !== "string") throw invocationError("skill_argument_type_mismatch", `${argument.name} must be string`);
    if (argument.pattern && !new RegExp(argument.pattern).test(value)) throw invocationError("skill_argument_pattern_mismatch", `${argument.name} does not match its pattern`);
    if (argument.type === "path") {
      const normalized = value.replace(/\\/g, "/");
      if (normalized.includes("../") || normalized.startsWith("/")) throw invocationError("skill_argument_path_escape", `${argument.name} must be workspace-relative`);
      void workspaceRoot;
    }
  } else if (argument.type === "number" || argument.type === "integer") {
    if (typeof value !== "number" || !Number.isFinite(value)) throw invocationError("skill_argument_type_mismatch", `${argument.name} must be number`);
    if (argument.type === "integer" && !Number.isSafeInteger(value)) throw invocationError("skill_argument_type_mismatch", `${argument.name} must be integer`);
    if (argument.minimum !== null && value < argument.minimum) throw invocationError("skill_argument_minimum", `${argument.name} is below ${argument.minimum}`);
    if (argument.maximum !== null && value > argument.maximum) throw invocationError("skill_argument_maximum", `${argument.name} is above ${argument.maximum}`);
  } else if (argument.type === "boolean" && typeof value !== "boolean") {
    throw invocationError("skill_argument_type_mismatch", `${argument.name} must be boolean`);
  }
  if (argument.type === "enum" && !argument.enumValues.some((candidate) => digest(candidate) === digest(value))) {
    throw invocationError("skill_argument_enum", `${argument.name} is not an allowed value`);
  }
  return canonicalize(value);
}

function renderBody(body: string, argumentsValue: JsonObject): string {
  return body.replace(/\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}/g, (_match, name: string) => {
    const value = argumentsValue[name];
    if (value === undefined || value === null) return "";
    return typeof value === "string" ? value : JSON.stringify(value);
  });
}

function intersectPatterns(parent: string[], child: string[]): string[] {
  if (parent.includes("*")) return [...child];
  if (child.includes("*")) return [...parent];
  const output = new Set<string>();
  for (const left of parent) for (const right of child) {
    if (left === right) output.add(left);
    else if (!left.includes("*") && wildcardMatches(right, left)) output.add(left);
    else if (!right.includes("*") && wildcardMatches(left, right)) output.add(right);
    else if (isSimplePatternSubset(left, right)) output.add(left);
    else if (isSimplePatternSubset(right, left)) output.add(right);
  }
  return [...output].sort();
}

/**
 * Proves inclusion for the common prefix/suffix glob forms used by tool and
 * MCP namespaces. Returning false is intentionally conservative: an
 * unprovable intersection must never widen a child skill's authority.
 */
function isSimplePatternSubset(candidate: string, container: string): boolean {
  if (candidate === container || container === "*") return true;
  if (candidate.includes("?") || container.includes("?")) return false;
  const candidateStars = [...candidate].filter((value) => value === "*").length;
  const containerStars = [...container].filter((value) => value === "*").length;
  if (candidateStars !== 1 || containerStars !== 1) return false;
  if (candidate.endsWith("*") && container.endsWith("*")) {
    return candidate.slice(0, -1).toLowerCase().startsWith(container.slice(0, -1).toLowerCase());
  }
  if (candidate.startsWith("*") && container.startsWith("*")) {
    return candidate.slice(1).toLowerCase().endsWith(container.slice(1).toLowerCase());
  }
  return false;
}

function estimateTokens(value: string): number {
  const parts = value.match(/[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu) ?? [];
  return parts.reduce((total, part) => total + Math.max(1, Math.ceil(part.length / 4)), 0);
}

function invocationError(code: string, message: string): Error {
  const error = new Error(message);
  error.name = "SkillInvocationError";
  Object.assign(error, { code });
  return error;
}
