import type { JsonObject, JsonValue } from "../contracts.ts";
import { canonicalize, cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type { CommandDescriptor, CommandInvocationRequest } from "./contracts.ts";

export interface LocalCommandContext {
  invocationId: string;
  descriptor: CommandDescriptor;
  request: CommandInvocationRequest;
  arguments: JsonObject;
  signal?: AbortSignal;
  emitArtifact(artifact: JsonObject): void;
  emitEvent(event: JsonObject): void;
}

export type LocalCommandHandler = (context: LocalCommandContext) => Promise<{
  output: JsonValue;
  artifacts?: JsonObject[];
  metadata?: JsonObject;
}>;

export interface LocalCommandExecution {
  invocationId: string;
  handlerId: string;
  commandId: string;
  status: "completed" | "failed" | "cancelled";
  output: JsonValue;
  artifacts: JsonObject[];
  events: JsonObject[];
  startedAt: string;
  completedAt: string;
  durationMs: number;
  failure: JsonObject | null;
  metadata: JsonObject;
}

export class LocalCommandRuntime {
  private readonly handlers = new Map<string, LocalCommandHandler>();
  private readonly results = new Map<string, LocalCommandExecution>();
  private readonly now: () => Date;
  private readonly maximumResults: number;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumResults?: number } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumResults = options.maximumResults ?? 10_000;
  }

  register(handlerId: string, handler: LocalCommandHandler, replace = false): void {
    if (this.handlers.has(handlerId) && !replace) throw new Error(`local command handler ${handlerId} is already registered`);
    this.handlers.set(handlerId, handler);
  }

  unregister(handlerId: string): boolean {
    return this.handlers.delete(handlerId);
  }

  async execute(
    descriptor: CommandDescriptor,
    request: CommandInvocationRequest,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ): Promise<LocalCommandExecution> {
    const handler = this.handlers.get(descriptor.handler.handlerId);
    if (!handler) throw new Error(`local command handler ${descriptor.handler.handlerId} is not registered`);
    const invocationId = deterministicId("local-command", {
      command_id: descriptor.commandId,
      descriptor_digest: descriptor.descriptorDigest,
      run_id: request.identity.runId,
      task_id: request.identity.taskId,
      session_id: request.identity.sessionId,
      session_revision: request.identity.sessionRevision,
      command_call_id: request.identity.commandCallId,
      arguments_digest: digest(argumentsValue),
    }, 32);
    const existing = this.results.get(invocationId);
    if (existing) return cloneJson(existing);
    if (signal?.aborted) throw Object.assign(new Error("local command cancelled"), { name: "AbortError" });
    const artifacts: JsonObject[] = [];
    const events: JsonObject[] = [];
    const startedAt = this.timestamp();
    const started = performance.now();
    let result: LocalCommandExecution;
    try {
      const response = await handler({
        invocationId,
        descriptor: cloneJson(descriptor),
        request: cloneJson(request),
        arguments: cloneJson(argumentsValue),
        signal,
        emitArtifact: (artifact) => artifacts.push(cloneJson(artifact)),
        emitEvent: (event) => events.push(cloneJson(event)),
      });
      result = {
        invocationId,
        handlerId: descriptor.handler.handlerId,
        commandId: descriptor.commandId,
        status: "completed",
        output: canonicalize(response.output),
        artifacts: [...artifacts, ...(response.artifacts ?? []).map(cloneJson)],
        events,
        startedAt,
        completedAt: this.timestamp(),
        durationMs: Math.max(0, performance.now() - started),
        failure: null,
        metadata: cloneJson(response.metadata ?? {}),
      };
    } catch (error) {
      result = {
        invocationId,
        handlerId: descriptor.handler.handlerId,
        commandId: descriptor.commandId,
        status: signal?.aborted ? "cancelled" : "failed",
        output: null,
        artifacts,
        events,
        startedAt,
        completedAt: this.timestamp(),
        durationMs: Math.max(0, performance.now() - started),
        failure: {
          name: error instanceof Error ? error.name : "Error",
          message: error instanceof Error ? error.message : String(error),
        },
        metadata: {},
      };
      this.remember(result);
      throw error;
    }
    this.remember(result);
    return cloneJson(result);
  }

  get(invocationId: string): LocalCommandExecution | null {
    const value = this.results.get(invocationId);
    return value ? cloneJson(value) : null;
  }

  private remember(result: LocalCommandExecution): void {
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
