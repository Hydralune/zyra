import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicMcpId, monotonicNow, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";

export interface McpRateLimitConfig {
  maximumConcurrent: number;
  requestsPerMinute: number;
  burst: number;
  queueCapacity: number;
  queueTimeoutMs: number;
}

export interface McpRateLimitLease {
  leaseId: string;
  serverId: string;
  operation: string;
  requestId: string;
  acquiredAt: string;
  queuedAt: string | null;
  queueDurationMs: number;
  tokensRemaining: number;
  concurrent: number;
  release(): void;
}

interface ServerBucket {
  config: McpRateLimitConfig;
  tokens: number;
  lastRefillMs: number;
  concurrent: number;
  queue: QueueEntry[];
}

interface QueueEntry {
  requestId: string;
  operation: string;
  queuedAtMs: number;
  resolve(lease: McpRateLimitLease): void;
  reject(error: Error): void;
  timer: NodeJS.Timeout;
  signal: AbortSignal | undefined;
  abort: (() => void) | null;
}

export interface McpRateLimitSnapshot {
  version: "zyra.mcp-rate-limit/v1";
  buckets: {
    serverId: string;
    config: McpRateLimitConfig;
    tokens: number;
    lastRefillMs: number;
    concurrent: number;
    queued: number;
  }[];
  digest: string;
  capturedAt: string;
}

export class McpRateLimitRuntime {
  private readonly buckets = new Map<string, ServerBucket>();
  private readonly now: () => Date;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date } = {}) {
    this.now = options.now ?? (() => new Date());
  }

  configure(serverId: string, configValue: McpRateLimitConfig): void {
    const config = validateConfig(configValue);
    const existing = this.buckets.get(serverId);
    if (existing) {
      existing.config = config;
      existing.tokens = Math.min(existing.tokens, config.burst);
      this.drain(serverId, existing);
    } else {
      this.buckets.set(serverId, {
        config,
        tokens: config.burst,
        lastRefillMs: this.now().getTime(),
        concurrent: 0,
        queue: [],
      });
    }
  }

  async acquire(
    serverId: string,
    operation: string,
    requestId: string,
    signal?: AbortSignal,
  ): Promise<McpRateLimitLease> {
    if (signal?.aborted) throw rateError(serverId, "rate_limit_cancelled", "MCP rate-limit acquisition was cancelled");
    const bucket = this.buckets.get(serverId);
    if (!bucket) throw rateError(serverId, "rate_limit_not_configured", `rate limit for ${serverId} is not configured`);
    this.refill(bucket);
    const immediate = this.tryAcquire(serverId, operation, requestId, bucket, null);
    if (immediate) return immediate;
    if (bucket.queue.length >= bucket.config.queueCapacity) throw rateError(serverId, "rate_limit_queue_full", `MCP rate-limit queue for ${serverId} is full`);
    return new Promise<McpRateLimitLease>((resolve, reject) => {
      const queuedAtMs = this.now().getTime();
      const entry: QueueEntry = {
        requestId,
        operation,
        queuedAtMs,
        resolve,
        reject,
        timer: setTimeout(() => {
          const index = bucket.queue.indexOf(entry);
          if (index >= 0) bucket.queue.splice(index, 1);
          entry.abort?.();
          reject(rateError(serverId, "rate_limit_queue_timeout", `MCP request ${requestId} exceeded queue timeout`));
        }, bucket.config.queueTimeoutMs),
        signal,
        abort: null,
      };
      if (signal) {
        const onAbort = (): void => {
          const index = bucket.queue.indexOf(entry);
          if (index >= 0) bucket.queue.splice(index, 1);
          clearTimeout(entry.timer);
          reject(rateError(serverId, "rate_limit_cancelled", "MCP rate-limit acquisition was cancelled"));
        };
        signal.addEventListener("abort", onAbort, { once: true });
        entry.abort = () => signal.removeEventListener("abort", onAbort);
      }
      bucket.queue.push(entry);
      this.scheduleDrain(serverId, bucket);
    });
  }

  snapshot(): McpRateLimitSnapshot {
    const buckets = [...this.buckets.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([serverId, bucket]) => ({
      serverId,
      config: cloneJson(bucket.config),
      tokens: bucket.tokens,
      lastRefillMs: bucket.lastRefillMs,
      concurrent: bucket.concurrent,
      queued: bucket.queue.length,
    }));
    const withoutDigest = {
      version: "zyra.mcp-rate-limit/v1" as const,
      buckets,
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  private tryAcquire(
    serverId: string,
    operation: string,
    requestId: string,
    bucket: ServerBucket,
    queuedAtMs: number | null,
  ): McpRateLimitLease | null {
    this.refill(bucket);
    if (bucket.concurrent >= bucket.config.maximumConcurrent || bucket.tokens < 1) return null;
    bucket.tokens -= 1;
    bucket.concurrent += 1;
    const acquiredAt = this.timestamp();
    let released = false;
    return {
      leaseId: deterministicMcpId("mcp-rate-lease", { server_id: serverId, request_id: requestId, operation, acquired_at: acquiredAt }),
      serverId,
      operation,
      requestId,
      acquiredAt,
      queuedAt: queuedAtMs === null ? null : new Date(queuedAtMs).toISOString(),
      queueDurationMs: queuedAtMs === null ? 0 : Math.max(0, this.now().getTime() - queuedAtMs),
      tokensRemaining: bucket.tokens,
      concurrent: bucket.concurrent,
      release: () => {
        if (released) return;
        released = true;
        bucket.concurrent = Math.max(0, bucket.concurrent - 1);
        this.drain(serverId, bucket);
      },
    };
  }

  private refill(bucket: ServerBucket): void {
    const now = this.now().getTime();
    const elapsed = Math.max(0, now - bucket.lastRefillMs);
    bucket.tokens = Math.min(bucket.config.burst, bucket.tokens + elapsed * bucket.config.requestsPerMinute / 60_000);
    bucket.lastRefillMs = now;
  }

  private drain(serverId: string, bucket: ServerBucket): void {
    this.refill(bucket);
    while (bucket.queue.length) {
      const entry = bucket.queue[0];
      if (entry.signal?.aborted) {
        bucket.queue.shift();
        clearTimeout(entry.timer);
        entry.abort?.();
        continue;
      }
      const lease = this.tryAcquire(serverId, entry.operation, entry.requestId, bucket, entry.queuedAtMs);
      if (!lease) break;
      bucket.queue.shift();
      clearTimeout(entry.timer);
      entry.abort?.();
      entry.resolve(lease);
    }
    if (bucket.queue.length) this.scheduleDrain(serverId, bucket);
  }

  private scheduleDrain(serverId: string, bucket: ServerBucket): void {
    const tokensNeeded = Math.max(0, 1 - bucket.tokens);
    const delay = tokensNeeded === 0 ? 1 : Math.ceil(tokensNeeded * 60_000 / bucket.config.requestsPerMinute);
    setTimeout(() => this.drain(serverId, bucket), Math.max(1, delay));
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function validateConfig(value: McpRateLimitConfig): McpRateLimitConfig {
  for (const [key, child] of Object.entries(value)) if (!Number.isSafeInteger(child) || child < 0) throw new Error(`MCP rate-limit ${key} must be non-negative integer`);
  if (value.maximumConcurrent < 1 || value.requestsPerMinute < 1 || value.burst < 1) throw new Error("MCP rate-limit concurrency, rate, and burst must be positive");
  return cloneJson(value);
}

function rateError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-rate-limit", { server_id: serverId, code, message }),
    category: "policy",
    code,
    message,
    serverId,
    retryable: code.includes("timeout") || code.includes("full"),
    disposition: code.includes("timeout") || code.includes("full") ? "retry_same_connection" : "terminal",
  });
}
