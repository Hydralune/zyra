import type { JsonObject } from "../contracts.ts";
import type { McpRetryPolicy } from "../config/config-store.ts";
import { canonicalJson, cloneJson, deterministicMcpId, monotonicNow, sha256 } from "../core/canonical.ts";
import { McpRuntimeError, normalizeFailure, type McpFailureRecord } from "../core/failure.ts";

export interface McpRetryContext {
  serverId: string;
  requestId: string;
  operation: string;
  idempotent: boolean;
  idempotencyKey: string | null;
  connectionEpoch: number;
  policy: McpRetryPolicy;
  metadata: JsonObject;
}

export interface McpRetryAttempt {
  attemptId: string;
  serverId: string;
  requestId: string;
  operation: string;
  attempt: number;
  connectionEpoch: number;
  startedAt: string;
  completedAt: string | null;
  outcome: "pending" | "success" | "failure" | "cancelled";
  failure: McpFailureRecord | null;
  delayBeforeMs: number;
  retryScheduled: boolean;
  metadata: JsonObject;
}

export interface McpRetryResult<T> {
  value: T;
  attempts: McpRetryAttempt[];
  retries: number;
  totalDelayMs: number;
}

export class McpRetryRuntime {
  private readonly now: () => Date;
  private readonly attempts: McpRetryAttempt[] = [];
  private readonly maximumHistory: number;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumHistory?: number } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumHistory = options.maximumHistory ?? 20_000;
  }

  async execute<T>(
    context: McpRetryContext,
    operation: (attempt: number, signal?: AbortSignal) => Promise<T>,
    options: {
      signal?: AbortSignal;
      reconnect?: (failure: McpFailureRecord, attempt: number) => Promise<number>;
      refresh?: (failure: McpFailureRecord, attempt: number) => Promise<void>;
    } = {},
  ): Promise<McpRetryResult<T>> {
    if (!context.idempotent && context.policy.maximumAttempts > 1 && !context.idempotencyKey) {
      throw retryError(context.serverId, "unsafe_retry_policy", `non-idempotent operation ${context.operation} requires an idempotency key for retries`);
    }
    const attempts: McpRetryAttempt[] = [];
    let totalDelayMs = 0;
    let connectionEpoch = context.connectionEpoch;
    let priorFailure: McpFailureRecord | null = null;
    for (let attempt = 1; attempt <= context.policy.maximumAttempts; attempt += 1) {
      if (options.signal?.aborted) throw retryError(context.serverId, "retry_cancelled", `retry for ${context.operation} was cancelled`);
      const delay = attempt === 1 ? 0 : retryDelay(context, attempt, priorFailure);
      if (delay > 0) {
        await wait(delay, options.signal);
        totalDelayMs += delay;
      }
      const record: McpRetryAttempt = {
        attemptId: deterministicMcpId("mcp-retry-attempt", {
          server_id: context.serverId,
          request_id: context.requestId,
          operation: context.operation,
          attempt,
          connection_epoch: connectionEpoch,
        }),
        serverId: context.serverId,
        requestId: context.requestId,
        operation: context.operation,
        attempt,
        connectionEpoch,
        startedAt: this.timestamp(),
        completedAt: null,
        outcome: "pending",
        failure: null,
        delayBeforeMs: delay,
        retryScheduled: false,
        metadata: cloneJson(context.metadata),
      };
      attempts.push(record);
      this.remember(record);
      try {
        const value = await operation(attempt, options.signal);
        record.outcome = "success";
        record.completedAt = this.timestamp();
        return { value, attempts: attempts.map(cloneJson), retries: attempt - 1, totalDelayMs };
      } catch (error) {
        const failure = normalizeFailure(error, {
          failureId: deterministicMcpId("mcp-retry-failure", {
            server_id: context.serverId,
            request_id: context.requestId,
            operation: context.operation,
            attempt,
            connection_epoch: connectionEpoch,
          }),
          serverId: context.serverId,
          requestId: context.requestId,
          operation: context.operation,
        });
        priorFailure = failure;
        record.failure = failure;
        record.outcome = options.signal?.aborted ? "cancelled" : "failure";
        record.completedAt = this.timestamp();
        const retryable = this.shouldRetry(context, failure, attempt);
        record.retryScheduled = retryable;
        if (!retryable) throw error;
        if (failure.disposition === "refresh_then_retry" && options.refresh) await options.refresh(failure, attempt);
        if (failure.disposition === "reconnect_then_retry" && options.reconnect) connectionEpoch = await options.reconnect(failure, attempt);
      }
    }
    throw new McpRuntimeError({
      failureId: deterministicMcpId("mcp-retry-exhausted", { server_id: context.serverId, request_id: context.requestId }),
      category: priorFailure?.category ?? "remote",
      code: "retry_attempts_exhausted",
      message: `MCP operation ${context.operation} exhausted ${context.policy.maximumAttempts} attempts`,
      serverId: context.serverId,
      operation: context.operation,
      requestId: context.requestId,
      retryable: false,
      disposition: "terminal",
      causeChain: priorFailure ? [priorFailure] : [],
      details: { attempts: canonicalJson(attempts), total_delay_ms: totalDelayMs },
    });
  }

  history(serverId?: string): McpRetryAttempt[] {
    return this.attempts.filter((attempt) => !serverId || attempt.serverId === serverId).map(cloneJson);
  }

  private shouldRetry(context: McpRetryContext, failure: McpFailureRecord, attempt: number): boolean {
    if (attempt >= context.policy.maximumAttempts || !failure.retryable) return false;
    if (!context.idempotent && !context.idempotencyKey) return false;
    if (failure.status_code !== null && !context.policy.retryStatusCodes.includes(failure.status_code)) return false;
    if (failure.json_rpc_code !== null && !context.policy.retryJsonRpcCodes.includes(failure.json_rpc_code)) return false;
    return failure.disposition === "retry_same_connection"
      || failure.disposition === "reconnect_then_retry"
      || failure.disposition === "refresh_then_retry";
  }

  private remember(record: McpRetryAttempt): void {
    this.attempts.push(record);
    if (this.attempts.length > this.maximumHistory) this.attempts.splice(0, this.attempts.length - this.maximumHistory);
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function retryDelay(context: McpRetryContext, attempt: number, failure: McpFailureRecord | null): number {
  if (failure?.retry_after_ms !== null && failure?.retry_after_ms !== undefined) return Math.min(failure.retry_after_ms, context.policy.maximumDelayMs);
  const base = Math.min(context.policy.maximumDelayMs, context.policy.initialDelayMs * context.policy.multiplier ** Math.max(0, attempt - 2));
  const seed = Number.parseInt(sha256({ request_id: context.requestId, attempt, idempotency_key: context.idempotencyKey }).slice(0, 8), 16) / 0xffffffff;
  const jitter = (seed * 2 - 1) * context.policy.jitterRatio;
  return Math.max(0, Math.round(base * (1 + jitter)));
}

function wait(delayMs: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(Object.assign(new Error("retry wait cancelled"), { name: "AbortError" })); return; }
    const timer = setTimeout(() => { cleanup(); resolve(); }, delayMs);
    const onAbort = (): void => { clearTimeout(timer); cleanup(); reject(Object.assign(new Error("retry wait cancelled"), { name: "AbortError" })); };
    const cleanup = (): void => signal?.removeEventListener("abort", onAbort);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function retryError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-retry", { server_id: serverId, code, message }),
    category: "policy",
    code,
    message,
    serverId,
    retryable: false,
    disposition: "terminal",
  });
}
