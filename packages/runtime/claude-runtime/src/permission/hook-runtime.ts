import type { JsonObject } from "../contracts.ts";
import {
  canonicalize,
  cloneJson,
  deterministicId,
  digest,
  optionalObject,
  wildcardMatches,
} from "../e02/canonical.ts";
import type {
  E02Clock,
  PermissionEffect,
  PermissionHookAudit,
  PermissionRiskAssessment,
} from "../e02/contracts.ts";
import { PermissionIdentity, type PermissionIdentityRecord } from "./model.ts";

export interface PermissionHookContext {
  identity: PermissionIdentityRecord;
  risk: PermissionRiskAssessment;
  signal: AbortSignal;
  now: string;
}
export interface PermissionHookResult {
  effect?: PermissionEffect | "passthrough";
  arguments?: JsonObject;
  reason?: string;
  metadata?: JsonObject;
}

export type PermissionHookHandler = (
  context: PermissionHookContext,
) => PermissionHookResult | Promise<PermissionHookResult>;

export interface PermissionHookDescriptor {
  hookId: string;
  pluginId: string | null;
  enabled: boolean;
  order: number;
  toolPattern: string;
  namespacePattern: string;
  serverPattern: string;
  operationPattern: string;
  timeoutMs: number;
  canMutateArguments: boolean;
  failClosed: boolean;
  handler: PermissionHookHandler;
  metadata: JsonObject;
}

export interface PermissionHookPipelineResult {
  identity: PermissionIdentityRecord;
  audits: PermissionHookAudit[];
  forcedEffect: PermissionEffect | null;
  forcedReason: string | null;
  failedClosed: boolean;
}

export class PermissionHookRuntime {
  private readonly hooks = new Map<string, PermissionHookDescriptor>();
  private readonly clock: E02Clock;

  constructor(clock: E02Clock = () => new Date().toISOString()) {
    this.clock = clock;
  }

  register(input: Omit<PermissionHookDescriptor, "hookId"> & { hookId?: string }): string {
    const descriptor = normalizeDescriptor(input);
    const hookId = input.hookId || deterministicId("permission-hook", {
      pluginId: descriptor.pluginId,
      order: descriptor.order,
      toolPattern: descriptor.toolPattern,
      namespacePattern: descriptor.namespacePattern,
      serverPattern: descriptor.serverPattern,
      operationPattern: descriptor.operationPattern,
    }, 32);
    const existing = this.hooks.get(hookId);
    if (existing && digest(descriptorSnapshot(existing)) !== digest(descriptorSnapshot({ ...descriptor, hookId }))) {
      throw new Error(`permission hook ${hookId} is already registered with different semantics`);
    }
    this.hooks.set(hookId, { ...descriptor, hookId });
    return hookId;
  }

  unregister(hookId: string): boolean {
    return this.hooks.delete(hookId);
  }

  enable(hookId: string, enabled: boolean): void {
    const descriptor = this.hooks.get(hookId);
    if (!descriptor) throw new Error(`unknown permission hook ${hookId}`);
    descriptor.enabled = enabled;
  }

  async runBeforeTool(
    identity: PermissionIdentityRecord,
    risk: PermissionRiskAssessment,
    outerSignal?: AbortSignal,
  ): Promise<PermissionHookPipelineResult> {
    let current = cloneJson(identity);
    const audits: PermissionHookAudit[] = [];
    let forcedEffect: PermissionEffect | null = null;
    let forcedReason: string | null = null;
    let failedClosed = false;
    for (const descriptor of this.matching(current)) {
      const beforeDigest = current.argumentsDigest;
      const started = performance.now();
      const controller = new AbortController();
      const abort = (): void => controller.abort(outerSignal?.reason);
      outerSignal?.addEventListener("abort", abort, { once: true });
      let timer: ReturnType<typeof setTimeout> | null = null;
      try {
        if (outerSignal?.aborted) throw abortError(outerSignal.reason);
        const timeout = new Promise<never>((_, reject) => {
          timer = setTimeout(() => {
            controller.abort("permission_hook_timeout");
            reject(new Error(`permission hook ${descriptor.hookId} timed out after ${descriptor.timeoutMs}ms`));
          }, descriptor.timeoutMs);
        });
        const result = await Promise.race([
          Promise.resolve(descriptor.handler({
            identity: cloneJson(current),
            risk: cloneJson(risk),
            signal: controller.signal,
            now: this.clock(),
          })),
          timeout,
        ]);
        const normalized = normalizeResult(result);
        if (normalized.arguments) {
          if (!descriptor.canMutateArguments) throw new Error(`permission hook ${descriptor.hookId} returned forbidden argument mutation`);
          const rebound = this.validateMutation(current, normalized.arguments);
          current = rebound;
        }
        if (normalized.effect && normalized.effect !== "passthrough") {
          forcedEffect = combineEffects(forcedEffect, normalized.effect);
          forcedReason = normalized.reason || `permission hook ${descriptor.hookId} returned ${normalized.effect}`;
        }
        const changed = beforeDigest !== current.argumentsDigest;
        audits.push({
          hookId: descriptor.hookId,
          pluginId: descriptor.pluginId,
          order: descriptor.order,
          beforeDigest,
          afterDigest: current.argumentsDigest,
          changed,
          outcome: normalized.effect === "deny" || normalized.effect === "ask"
            ? normalized.effect
            : changed
              ? "mutate"
              : "pass",
          reason: normalized.reason || "hook completed",
          durationMs: Math.max(0, Math.round(performance.now() - started)),
          errorCode: null,
          errorMessage: null,
          outputMetadata: optionalObject(normalized.metadata),
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        const failClosed = descriptor.failClosed;
        audits.push({
          hookId: descriptor.hookId,
          pluginId: descriptor.pluginId,
          order: descriptor.order,
          beforeDigest,
          afterDigest: current.argumentsDigest,
          changed: false,
          outcome: "error",
          reason: failClosed ? "hook error failed closed" : "hook error ignored by explicit non-critical policy",
          durationMs: Math.max(0, Math.round(performance.now() - started)),
          errorCode: controller.signal.aborted ? "hook_timeout_or_abort" : "hook_error",
          errorMessage: message,
          outputMetadata: {},
        });
        if (failClosed) {
          failedClosed = true;
          forcedEffect = "deny";
          forcedReason = `permission hook ${descriptor.hookId} failed closed: ${message}`;
          break;
        }
      } finally {
        if (timer) clearTimeout(timer);
        outerSignal?.removeEventListener("abort", abort);
      }
      if (forcedEffect === "deny") break;
    }
    return { identity: current, audits, forcedEffect, forcedReason, failedClosed };
  }

  runBeforeToolSync(
    identity: PermissionIdentityRecord,
    risk: PermissionRiskAssessment,
  ): PermissionHookPipelineResult {
    let current = cloneJson(identity);
    const audits: PermissionHookAudit[] = [];
    let forcedEffect: PermissionEffect | null = null;
    let forcedReason: string | null = null;
    let failedClosed = false;
    for (const descriptor of this.matching(current)) {
      const beforeDigest = current.argumentsDigest;
      const started = performance.now();
      try {
        const result = descriptor.handler({
          identity: cloneJson(current),
          risk: cloneJson(risk),
          signal: new AbortController().signal,
          now: this.clock(),
        });
        if (result instanceof Promise) throw new Error(`permission hook ${descriptor.hookId} is asynchronous in a synchronous evaluation`);
        const normalized = normalizeResult(result);
        if (normalized.arguments) {
          if (!descriptor.canMutateArguments) throw new Error(`permission hook ${descriptor.hookId} returned forbidden argument mutation`);
          current = this.validateMutation(current, normalized.arguments);
        }
        if (normalized.effect && normalized.effect !== "passthrough") {
          forcedEffect = combineEffects(forcedEffect, normalized.effect);
          forcedReason = normalized.reason || `permission hook ${descriptor.hookId} returned ${normalized.effect}`;
        }
        const changed = beforeDigest !== current.argumentsDigest;
        audits.push({
          hookId: descriptor.hookId,
          pluginId: descriptor.pluginId,
          order: descriptor.order,
          beforeDigest,
          afterDigest: current.argumentsDigest,
          changed,
          outcome: normalized.effect === "deny" || normalized.effect === "ask"
            ? normalized.effect
            : changed
              ? "mutate"
              : "pass",
          reason: normalized.reason || "hook completed",
          durationMs: Math.max(0, Math.round(performance.now() - started)),
          errorCode: null,
          errorMessage: null,
          outputMetadata: optionalObject(normalized.metadata),
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        audits.push({
          hookId: descriptor.hookId,
          pluginId: descriptor.pluginId,
          order: descriptor.order,
          beforeDigest,
          afterDigest: current.argumentsDigest,
          changed: false,
          outcome: "error",
          reason: descriptor.failClosed ? "hook error failed closed" : "hook error ignored",
          durationMs: Math.max(0, Math.round(performance.now() - started)),
          errorCode: "hook_error",
          errorMessage: message,
          outputMetadata: {},
        });
        if (descriptor.failClosed) {
          forcedEffect = "deny";
          forcedReason = `permission hook ${descriptor.hookId} failed closed: ${message}`;
          failedClosed = true;
          break;
        }
      }
      if (forcedEffect === "deny") break;
    }
    return { identity: current, audits, forcedEffect, forcedReason, failedClosed };
  }

  validateMutation(
    before: PermissionIdentityRecord,
    argumentsValue: JsonObject,
  ): PermissionIdentityRecord {
    const canonicalArguments = canonicalize(argumentsValue) as JsonObject;
    const rebound = PermissionIdentity.rebindArguments(before, canonicalArguments);
    const invariantKeys: Array<keyof PermissionIdentityRecord["context"]> = [
      "runId",
      "taskId",
      "sessionId",
      "sessionRevision",
      "workerRequestId",
      "toolCallId",
      "toolName",
      "namespace",
      "serverId",
      "commandName",
      "resourceUri",
      "operation",
      "workspaceRoot",
    ];
    for (const key of invariantKeys) {
      if (before.context[key] !== rebound.context[key]) throw new Error(`permission hook mutation changed immutable ${String(key)}`);
    }
    return rebound;
  }

  list(): JsonObject[] {
    return [...this.hooks.values()]
      .map(descriptorSnapshot)
      .sort((left, right) => Number(left.order) - Number(right.order) || String(left.hook_id).localeCompare(String(right.hook_id)));
  }

  snapshot(): JsonObject {
    const base: JsonObject = {
      version: "zyra.e02-permission-hooks/v1",
      hooks: this.list(),
    };
    return { ...base, snapshot_hash: digest(base) };
  }

  private matching(identity: PermissionIdentityRecord): PermissionHookDescriptor[] {
    const context = identity.context;
    return [...this.hooks.values()]
      .filter((hook) => hook.enabled)
      .filter((hook) => wildcardMatches(hook.toolPattern, context.toolName))
      .filter((hook) => wildcardMatches(hook.namespacePattern, context.namespace))
      .filter((hook) => wildcardMatches(hook.serverPattern, context.serverId))
      .filter((hook) => wildcardMatches(hook.operationPattern, context.operation))
      .sort((left, right) => left.order - right.order || left.hookId.localeCompare(right.hookId));
  }
}

function normalizeDescriptor(
  input: Omit<PermissionHookDescriptor, "hookId"> & { hookId?: string },
): PermissionHookDescriptor {
  if (typeof input.handler !== "function") throw new Error("permission hook handler is required");
  if (!Number.isSafeInteger(input.order)) throw new Error("permission hook order must be an integer");
  if (!Number.isSafeInteger(input.timeoutMs) || input.timeoutMs < 1 || input.timeoutMs > 120_000) {
    throw new Error("permission hook timeout must be between 1 and 120000ms");
  }
  return {
    hookId: input.hookId ?? "",
    pluginId: input.pluginId || null,
    enabled: input.enabled,
    order: input.order,
    toolPattern: input.toolPattern || "*",
    namespacePattern: input.namespacePattern || "*",
    serverPattern: input.serverPattern || "*",
    operationPattern: input.operationPattern || "*",
    timeoutMs: input.timeoutMs,
    canMutateArguments: input.canMutateArguments,
    failClosed: input.failClosed,
    handler: input.handler,
    metadata: canonicalize(input.metadata ?? {}) as JsonObject,
  };
}

function normalizeResult(value: PermissionHookResult | null | undefined): PermissionHookResult {
  if (!value) return {};
  if (value.effect && !new Set(["allow", "deny", "ask", "passthrough"]).has(value.effect)) {
    throw new Error(`permission hook returned unsupported effect ${value.effect}`);
  }
  return {
    effect: value.effect,
    arguments: value.arguments ? canonicalize(value.arguments) as JsonObject : undefined,
    reason: value.reason,
    metadata: optionalObject(value.metadata),
  };
}

function combineEffects(current: PermissionEffect | null, next: PermissionEffect): PermissionEffect {
  const rank: Record<PermissionEffect, number> = { allow: 1, ask: 2, deny: 3 };
  return !current || rank[next] > rank[current] ? next : current;
}

function descriptorSnapshot(descriptor: PermissionHookDescriptor): JsonObject {
  return {
    hook_id: descriptor.hookId,
    plugin_id: descriptor.pluginId,
    enabled: descriptor.enabled,
    order: descriptor.order,
    tool_pattern: descriptor.toolPattern,
    namespace_pattern: descriptor.namespacePattern,
    server_pattern: descriptor.serverPattern,
    operation_pattern: descriptor.operationPattern,
    timeout_ms: descriptor.timeoutMs,
    can_mutate_arguments: descriptor.canMutateArguments,
    fail_closed: descriptor.failClosed,
    metadata: descriptor.metadata,
  };
}

function abortError(reason: unknown): Error {
  const error = new Error(`permission hook aborted${reason ? `: ${String(reason)}` : ""}`);
  error.name = "AbortError";
  return error;
}
