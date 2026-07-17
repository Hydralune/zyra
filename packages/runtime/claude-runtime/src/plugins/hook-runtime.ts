import type { JsonObject } from "../contracts.ts";
import {
  cloneJson,
  deterministicId,
  digest,
  monotonicNow,
  wildcardMatches,
} from "../e02/index.ts";
import type {
  PluginHookContext,
  PluginHookDescriptor,
  PluginHookExecutor,
  PluginHookResult,
  PluginManifest,
} from "./contracts.ts";

export interface PluginHookRegistration {
  pluginId: string;
  pluginRevision: number;
  manifestDigest: string;
  manifest: PluginManifest;
  hook: PluginHookDescriptor;
}

export interface PluginHookPipelineResult {
  effect: "continue" | "deny" | "ask";
  reason: string;
  originalArgumentsDigest: string;
  finalArgumentsDigest: string;
  arguments: JsonObject;
  mutated: boolean;
  results: PluginHookResult[];
  failedClosed: boolean;
  revision: number;
  digest: string;
}

export class PluginHookRuntime {
  private readonly executor: PluginHookExecutor;
  private readonly now: () => Date;
  private readonly registrations = new Map<string, PluginHookRegistration>();
  private readonly results: PluginHookResult[] = [];
  private readonly maximumResults: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { executor: PluginHookExecutor; now?: () => Date; maximumResults?: number }) {
    this.executor = options.executor;
    this.now = options.now ?? (() => new Date());
    this.maximumResults = options.maximumResults ?? 20_000;
  }

  replace(registrationsValue: PluginHookRegistration[], expectedRevision = this.revision): number {
    if (expectedRevision !== this.revision) throw hookError("hook_revision_conflict", `plugin hook revision ${expectedRevision} does not match ${this.revision}`);
    const next = new Map<string, PluginHookRegistration>();
    for (const registrationValue of registrationsValue) {
      const registration = cloneJson(registrationValue);
      if (registration.pluginId !== registration.manifest.pluginId) throw hookError("hook_plugin_mismatch", `hook ${registration.hook.hookId} plugin id mismatch`);
      if (registration.manifestDigest !== registration.manifest.manifestDigest) throw hookError("hook_manifest_mismatch", `hook ${registration.hook.hookId} manifest digest mismatch`);
      const key = registrationKey(registration.pluginId, registration.hook.hookId);
      if (next.has(key)) throw hookError("duplicate_plugin_hook", `duplicate plugin hook ${key}`);
      next.set(key, registration);
    }
    this.registrations.clear();
    for (const [key, registration] of next) this.registrations.set(key, registration);
    this.revision += 1;
    return this.revision;
  }

  async beforeTool(contextValue: PluginHookContext, signal?: AbortSignal): Promise<PluginHookPipelineResult> {
    const context = cloneJson(contextValue);
    if (context.argumentsDigest !== digest(context.arguments)) throw hookError("hook_argument_digest_mismatch", "plugin hook context arguments digest mismatch");
    const originalArgumentsDigest = context.argumentsDigest;
    let currentArguments = cloneJson(context.arguments);
    let currentDigest = originalArgumentsDigest;
    let effect: PluginHookPipelineResult["effect"] = "continue";
    let reason = "";
    let failedClosed = false;
    const output: PluginHookResult[] = [];
    const registrations = [...this.registrations.values()]
      .filter((registration) => registration.manifest.enabled)
      .filter((registration) => registration.hook.event === "before_tool")
      .filter((registration) => registration.hook.toolPatterns.some((pattern) => wildcardMatches(pattern, context.toolName)))
      .sort((left, right) => right.hook.priority - left.hook.priority || left.pluginId.localeCompare(right.pluginId) || left.hook.hookId.localeCompare(right.hook.hookId));
    for (const registration of registrations) {
      const result = await this.execute(registration, {
        ...context,
        arguments: currentArguments,
        argumentsDigest: currentDigest,
      }, signal);
      output.push(result);
      if (result.mutated) {
        if (!registration.hook.canMutate) {
          effect = "deny";
          reason = `plugin hook ${registration.pluginId}/${registration.hook.hookId} attempted unauthorized mutation`;
          failedClosed = true;
          break;
        }
        if (result.originalArgumentsDigest !== currentDigest || result.finalArgumentsDigest !== digest(result.arguments)) {
          effect = "deny";
          reason = `plugin hook ${registration.pluginId}/${registration.hook.hookId} returned invalid mutation digest`;
          failedClosed = true;
          break;
        }
        currentArguments = cloneJson(result.arguments);
        currentDigest = result.finalArgumentsDigest;
      }
      if (result.effect === "deny") {
        effect = "deny";
        reason = result.reason || `plugin hook ${registration.pluginId}/${registration.hook.hookId} denied tool use`;
        if (result.failure && registration.hook.failClosed) failedClosed = true;
        break;
      }
      if (result.effect === "ask" && effect === "continue") {
        effect = "ask";
        reason = result.reason || `plugin hook ${registration.pluginId}/${registration.hook.hookId} requires approval`;
      }
    }
    const resultBase = {
      effect,
      reason,
      originalArgumentsDigest,
      finalArgumentsDigest: currentDigest,
      arguments: currentArguments,
      mutated: originalArgumentsDigest !== currentDigest,
      results: output,
      failedClosed,
      revision: this.revision,
    };
    return { ...resultBase, digest: digest(resultBase) };
  }

  listResults(): PluginHookResult[] {
    return this.results.map(cloneJson);
  }

  private async execute(
    registration: PluginHookRegistration,
    context: PluginHookContext,
    parentSignal?: AbortSignal,
  ): Promise<PluginHookResult> {
    const started = performance.now();
    const controller = new AbortController();
    const onAbort = (): void => controller.abort(parentSignal?.reason);
    parentSignal?.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => controller.abort(Object.assign(new Error("plugin hook timeout"), { name: "TimeoutError" })), registration.hook.timeoutMs);
    const hookExecutionId = deterministicId("plugin-hook-execution", {
      plugin_id: registration.pluginId,
      plugin_revision: registration.pluginRevision,
      hook_id: registration.hook.hookId,
      run_id: context.runId,
      task_id: context.taskId,
      session_id: context.sessionId,
      tool_call_id: context.toolCallId,
      arguments_digest: context.argumentsDigest,
    }, 32);
    let result: PluginHookResult;
    try {
      const response = await this.executor({
        manifest: cloneJson(registration.manifest),
        hook: cloneJson(registration.hook),
        context: cloneJson(context),
        signal: controller.signal,
      });
      const argumentsValue = response.arguments ? cloneJson(response.arguments) : cloneJson(context.arguments);
      const finalDigest = digest(argumentsValue);
      result = {
        hookExecutionId,
        pluginId: registration.pluginId,
        hookId: registration.hook.hookId,
        event: registration.hook.event,
        effect: response.effect ?? "continue",
        reason: response.reason ?? "",
        originalArgumentsDigest: context.argumentsDigest,
        finalArgumentsDigest: finalDigest,
        arguments: argumentsValue,
        mutated: finalDigest !== context.argumentsDigest,
        durationMs: Math.max(0, performance.now() - started),
        failure: null,
        metadata: cloneJson(response.metadata ?? {}),
      };
    } catch (error) {
      result = {
        hookExecutionId,
        pluginId: registration.pluginId,
        hookId: registration.hook.hookId,
        event: registration.hook.event,
        effect: registration.hook.failClosed ? "deny" : "continue",
        reason: registration.hook.failClosed ? "plugin hook failed closed" : "plugin hook failure ignored",
        originalArgumentsDigest: context.argumentsDigest,
        finalArgumentsDigest: context.argumentsDigest,
        arguments: cloneJson(context.arguments),
        mutated: false,
        durationMs: Math.max(0, performance.now() - started),
        failure: {
          name: error instanceof Error ? error.name : "Error",
          message: error instanceof Error ? error.message : String(error),
        },
        metadata: {},
      };
    } finally {
      clearTimeout(timer);
      parentSignal?.removeEventListener("abort", onAbort);
    }
    this.results.push(result);
    if (this.results.length > this.maximumResults) this.results.splice(0, this.results.length - this.maximumResults);
    return cloneJson(result);
  }
}

function registrationKey(pluginId: string, hookId: string): string {
  return `${pluginId}\0${hookId}`;
}

function hookError(code: string, message: string): Error {
  const error = new Error(message);
  error.name = "PluginHookError";
  Object.assign(error, { code });
  return error;
}
