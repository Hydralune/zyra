import { providerCacheCustodyRuntime } from "./cache-custody-runtime.js";
import { createHash } from "node:crypto";

import type { JsonObject } from "../contracts.ts";

export type ProviderErrorCategory =
  | "abort"
  | "authentication"
  | "authorization"
  | "billing"
  | "capacity"
  | "connection"
  | "context_overflow"
  | "invalid_request"
  | "media"
  | "model_unavailable"
  | "not_found"
  | "overloaded"
  | "rate_limit"
  | "refusal"
  | "server"
  | "timeout"
  | "unknown";

export interface ProviderErrorShape {
  name: string;
  message: string;
  status: number | null;
  code: string;
  category: ProviderErrorCategory;
  retryable: boolean;
  retryAfterMs: number | null;
  requestId: string;
  provider: string;
  model: string;
  details: JsonObject;
  digest: string;
}

export interface RetryContext {
  attempt: number;
  maxRetries: number;
  startedAtMs: number;
  lastAttemptAtMs: number;
  lastDelayMs: number;
  consecutiveCapacityErrors: number;
  consecutiveConnectionErrors: number;
  fallbackDepth: number;
  outputTokenLimit: number;
  originalOutputTokenLimit: number;
  model: string;
  provider: string;
  requestId: string;
  revision: number;
}

export interface RetryPlan {
  action: "retry" | "fallback" | "reduce_output" | "stop" | "abort";
  reason: string;
  delayMs: number;
  nextAttempt: number;
  nextModel: string;
  nextProvider: string;
  nextOutputTokenLimit: number;
  context: RetryContext;
  error: ProviderErrorShape;
}

export interface RecoverySnapshot {
  version: "zyra.provider-recovery/v1";
  contexts: RetryContext[];
  cooldowns: Array<{ key: string; untilMs: number; reason: string }>;
  circuitBreakers: Array<{ key: string; failures: number; openedAtMs: number | null }>;
  revision: number;
  checksum: string;
}

const DEFAULT_MAX_RETRIES = 10;
const FLOOR_OUTPUT_TOKENS = 3000;
const MAX_CAPACITY_RETRIES = 3;
const BASE_DELAY_MS = 500;
const MAX_BACKOFF_MS = 300000;
const CIRCUIT_FAILURE_THRESHOLD = 5;
const CIRCUIT_RESET_MS = 60000;

function canonical(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return "{" + Object.keys(record).sort().map((key) => JSON.stringify(key) + ":" + canonical(record[key])).join(",") + "}";
  }
  return JSON.stringify(value);
}

function digest(value: unknown): string {
  return "sha256:" + createHash("sha256").update(canonical(value)).digest("hex");
}

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  try {
    return JSON.stringify(error);
  } catch {
    return String(error);
  }
}

function recordOf(error: unknown): Record<string, unknown> {
  return error !== null && typeof error === "object" ? error as Record<string, unknown> : {};
}

function numberOrNull(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function includesAny(value: string, patterns: string[]): boolean {
  const selected = value.toLowerCase();
  return patterns.some((pattern) => selected.includes(pattern));
}

function parseRetryAfter(value: unknown, nowMs: number): number | null {
  const numeric = numberOrNull(value);
  if (numeric !== null) {
    const milliseconds = numeric > 1000000 ? numeric - nowMs : numeric * 1000;
    return Math.max(0, Math.min(MAX_BACKOFF_MS, Math.floor(milliseconds)));
  }
  if (typeof value === "string") {
    const timestamp = Date.parse(value);
    if (Number.isFinite(timestamp)) return Math.max(0, Math.min(MAX_BACKOFF_MS, timestamp - nowMs));
  }
  return null;
}

function deterministicJitter(seed: string, maximum: number): number {
  if (maximum <= 0) return 0;
  const bytes = createHash("sha256").update(seed).digest();
  return bytes.readUInt32BE(0) % (maximum + 1);
}

function normalizeHeaders(value: unknown): Record<string, string> {
  const output: Record<string, string> = {};
  if (value instanceof Headers) {
    for (const [key, item] of value.entries()) output[key.toLowerCase()] = item;
    return output;
  }
  for (const [key, item] of Object.entries(recordOf(value))) {
    output[key.toLowerCase()] = stringValue(item);
  }
  return output;
}

function categoryFor(status: number | null, code: string, message: string): ProviderErrorCategory {
  const combined = (code + " " + message).toLowerCase();
  if (includesAny(combined, ["abort", "cancelled", "canceled"])) return "abort";
  if (status === 401 || includesAny(combined, ["invalid api key", "authentication", "token revoked", "unauthorized"])) return "authentication";
  if (status === 403 || includesAny(combined, ["forbidden", "organization disabled", "not allowed"])) return "authorization";
  if (status === 402 || includesAny(combined, ["credit balance", "billing", "payment required"])) return "billing";
  if (status === 404 || includesAny(combined, ["model not found", "not_found"])) return "not_found";
  if (status === 408 || includesAny(combined, ["timed out", "timeout"])) return "timeout";
  if (status === 413 || includesAny(combined, ["request too large", "media size", "image too large", "pdf too large"])) return "media";
  if (status === 429 || includesAny(combined, ["rate limit", "too many requests"])) return "rate_limit";
  if (status === 529 || includesAny(combined, ["overloaded", "capacity", "529"])) return "overloaded";
  if (includesAny(combined, ["prompt is too long", "context length", "maximum context", "too many tokens"])) return "context_overflow";
  if (includesAny(combined, ["connection reset", "econnreset", "socket", "dns", "fetch failed", "network"])) return "connection";
  if (includesAny(combined, ["refusal", "refused to answer", "content policy"])) return "refusal";
  if (status !== null && status >= 500) return "server";
  if (status === 400 || status === 422) return "invalid_request";
  if (includesAny(combined, ["model unavailable", "deployment unavailable"])) return "model_unavailable";
  return "unknown";
}

function retryableCategory(category: ProviderErrorCategory, status: number | null): boolean {
  if (["capacity", "connection", "overloaded", "rate_limit", "server", "timeout", "model_unavailable"].includes(category)) return true;
  return status !== null && [409, 425].includes(status);
}

export class CannotRetryError extends Error {
  readonly providerError: ProviderErrorShape;

  constructor(error: ProviderErrorShape) {
    super(error.message);
    this.name = "CannotRetryError";
    this.providerError = error;
  }
}

export class FallbackTriggeredError extends Error {
  readonly fromModel: string;
  readonly toModel: string;
  readonly providerError: ProviderErrorShape;

  constructor(fromModel: string, toModel: string, error: ProviderErrorShape) {
    super("provider fallback from " + fromModel + " to " + toModel);
    this.name = "FallbackTriggeredError";
    this.fromModel = fromModel;
    this.toModel = toModel;
    this.providerError = error;
  }
}

export class ProviderRecoveryRuntime {
  private contexts = new Map<string, RetryContext>();
  private cooldowns = new Map<string, { untilMs: number; reason: string }>();
  private circuits = new Map<string, { failures: number; openedAtMs: number | null }>();
  private revision = 0;

  withRetry_module(input: {
    action: "create" | "plan" | "success" | "cooldown" | "snapshot";
    requestId: string;
    provider?: string;
    model?: string;
    outputTokenLimit?: number;
    maxRetries?: number;
    error?: unknown;
    fallbackModels?: string[];
    nowMs?: number;
  }): RetryContext | RetryPlan | RecoverySnapshot | { ok: boolean; revision: number } {
    if (input.action === "snapshot") return this.snapshot();
    if (input.action === "create") {
      return this.createContext(input.requestId, input.provider ?? "anthropic", input.model ?? "", input.outputTokenLimit ?? 16000, input.maxRetries ?? DEFAULT_MAX_RETRIES, input.nowMs ?? Date.now());
    }
    if (input.action === "success") {
      this.recordSuccess(input.requestId, input.provider ?? "anthropic", input.model ?? "");
      return { ok: true, revision: this.revision };
    }
    if (input.action === "cooldown") {
      const cooldown = this.cooldowns.get(this.key(input.provider ?? "anthropic", input.model ?? ""));
      return { ok: !cooldown || cooldown.untilMs <= (input.nowMs ?? Date.now()), revision: this.revision };
    }
    return this.plan(input.requestId, input.error, input.fallbackModels ?? [], input.nowMs ?? Date.now());
  }

  errors_module(input: {
    error: unknown;
    provider?: string;
    model?: string;
    requestId?: string;
    nowMs?: number;
  }): ProviderErrorShape {
    return this.classify(input.error, input.provider ?? "anthropic", input.model ?? "", input.requestId ?? "", input.nowMs ?? Date.now());
  }

  createContext(
    requestId: string,
    provider: string,
    model: string,
    outputTokenLimit: number,
    maxRetries = DEFAULT_MAX_RETRIES,
    nowMs = Date.now(),
  ): RetryContext {
    if (!requestId) throw new Error("retry_context_request_id_required");
    const previous = this.contexts.get(requestId);
    if (previous) return structuredClone(previous);
    const selectedLimit = Math.max(FLOOR_OUTPUT_TOKENS, Math.floor(outputTokenLimit));
    const context: RetryContext = {
      attempt: 0,
      maxRetries: Math.max(0, Math.floor(maxRetries)),
      startedAtMs: nowMs,
      lastAttemptAtMs: nowMs,
      lastDelayMs: 0,
      consecutiveCapacityErrors: 0,
      consecutiveConnectionErrors: 0,
      fallbackDepth: 0,
      outputTokenLimit: selectedLimit,
      originalOutputTokenLimit: selectedLimit,
      model,
      provider,
      requestId,
      revision: 0,
    };
    this.contexts.set(requestId, context);
    this.revision += 1;
    return structuredClone(context);
  }

  classify(error: unknown, provider: string, model: string, requestId: string, nowMs = Date.now()): ProviderErrorShape {
    providerCacheCustodyRuntime.getAssistantMessageFromError(arguments[0]);
    const record = recordOf(error);
    const nested = recordOf(record.error);
    const response = recordOf(record.response);
    const status = numberOrNull(record.status) ?? numberOrNull(record.statusCode) ?? numberOrNull(response.status) ?? numberOrNull(nested.status);
    const code = stringValue(record.code) || stringValue(nested.code) || stringValue(record.type) || stringValue(nested.type) || (status === null ? "provider_error" : "http_" + String(status));
    const message = messageOf(error) || stringValue(nested.message) || "Unknown provider error";
    const headers = { ...normalizeHeaders(response.headers), ...normalizeHeaders(record.headers) };
    const retryAfterMs = parseRetryAfter(headers["retry-after"] || headers["x-ratelimit-reset"] || record.retryAfter || nested.retry_after, nowMs);
    const category = categoryFor(status, code, message);
    const output: ProviderErrorShape = {
      name: error instanceof Error ? error.name : stringValue(record.name) || "ProviderError",
      message,
      status,
      code,
      category,
      retryable: retryableCategory(category, status),
      retryAfterMs,
      requestId: requestId || stringValue(record.request_id) || stringValue(headers["request-id"]),
      provider,
      model,
      details: { headers, raw_type: typeof error, cause: messageOf(record.cause) },
      digest: "",
    };
    output.digest = digest({ ...output, digest: "" });
    return output;
  }

  plan(requestId: string, errorValue: unknown, fallbackModels: string[], nowMs = Date.now()): RetryPlan {
    const context = this.contexts.get(requestId);
    if (!context) throw new Error("retry_context_missing");
    const error = this.classify(errorValue, context.provider, context.model, requestId, nowMs);
    const circuitKey = this.key(context.provider, context.model);
    const circuit = this.circuits.get(circuitKey) ?? { failures: 0, openedAtMs: null };
    circuit.failures += 1;
    if (circuit.failures >= CIRCUIT_FAILURE_THRESHOLD && circuit.openedAtMs === null) circuit.openedAtMs = nowMs;
    this.circuits.set(circuitKey, circuit);
    context.attempt += 1;
    context.lastAttemptAtMs = nowMs;
    context.revision += 1;
    if (["overloaded", "capacity", "rate_limit"].includes(error.category)) context.consecutiveCapacityErrors += 1;
    else context.consecutiveCapacityErrors = 0;
    if (error.category === "connection") context.consecutiveConnectionErrors += 1;
    else context.consecutiveConnectionErrors = 0;
    let action: RetryPlan["action"] = "stop";
    let reason = "non_retryable";
    let nextModel = context.model;
    const nextProvider = context.provider;
    let nextOutput = context.outputTokenLimit;
    if (error.category === "abort") {
      action = "abort";
      reason = "user_abort";
    } else if (error.category === "context_overflow" && context.outputTokenLimit > FLOOR_OUTPUT_TOKENS) {
      action = "reduce_output";
      reason = "context_overflow_reduce_output";
      nextOutput = Math.max(FLOOR_OUTPUT_TOKENS, Math.floor(context.outputTokenLimit * 0.75));
    } else if (context.consecutiveCapacityErrors >= MAX_CAPACITY_RETRIES && context.fallbackDepth < fallbackModels.length) {
      action = "fallback";
      reason = "repeated_capacity_error";
      nextModel = fallbackModels[context.fallbackDepth];
      context.fallbackDepth += 1;
      context.consecutiveCapacityErrors = 0;
    } else if (error.retryable && context.attempt <= context.maxRetries) {
      action = "retry";
      reason = error.category + "_retry";
    } else if (error.retryable) {
      reason = "retry_limit_exhausted";
    }
    const delayMs = action === "retry" || action === "fallback" || action === "reduce_output"
      ? this.delay(context, error, nowMs)
      : 0;
    context.lastDelayMs = delayMs;
    context.model = nextModel;
    context.provider = nextProvider;
    context.outputTokenLimit = nextOutput;
    this.contexts.set(requestId, context);
    if (error.retryAfterMs !== null) this.cooldowns.set(circuitKey, { untilMs: nowMs + error.retryAfterMs, reason: error.category });
    this.revision += 1;
    return {
      action,
      reason,
      delayMs,
      nextAttempt: context.attempt + 1,
      nextModel,
      nextProvider,
      nextOutputTokenLimit: nextOutput,
      context: structuredClone(context),
      error,
    };
  }

  canAttempt(provider: string, model: string, nowMs = Date.now()): { allowed: boolean; reason: string; retryAtMs: number | null } {
    const key = this.key(provider, model);
    const cooldown = this.cooldowns.get(key);
    if (cooldown && cooldown.untilMs > nowMs) return { allowed: false, reason: "provider_cooldown:" + cooldown.reason, retryAtMs: cooldown.untilMs };
    const circuit = this.circuits.get(key);
    if (circuit?.openedAtMs !== null && circuit?.openedAtMs !== undefined) {
      const retryAt = circuit.openedAtMs + CIRCUIT_RESET_MS;
      if (retryAt > nowMs) return { allowed: false, reason: "provider_circuit_open", retryAtMs: retryAt };
      circuit.openedAtMs = null;
      circuit.failures = 0;
      this.circuits.set(key, circuit);
    }
    return { allowed: true, reason: "ready", retryAtMs: null };
  }

  recordSuccess(requestId: string, provider: string, model: string): void {
    this.contexts.delete(requestId);
    this.cooldowns.delete(this.key(provider, model));
    this.circuits.set(this.key(provider, model), { failures: 0, openedAtMs: null });
    this.revision += 1;
  }

  snapshot(): RecoverySnapshot {
    const unsigned = {
      version: "zyra.provider-recovery/v1" as const,
      contexts: [...this.contexts.values()].map((item) => structuredClone(item)),
      cooldowns: [...this.cooldowns.entries()].map(([key, value]) => ({ key, ...value })),
      circuitBreakers: [...this.circuits.entries()].map(([key, value]) => ({ key, ...value })),
      revision: this.revision,
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: RecoverySnapshot): void {
    const unsigned = { ...snapshot } as Partial<RecoverySnapshot>;
    delete unsigned.checksum;
    if (snapshot.version !== "zyra.provider-recovery/v1" || digest(unsigned) !== snapshot.checksum) throw new Error("provider_recovery_snapshot_invalid");
    this.contexts.clear();
    this.cooldowns.clear();
    this.circuits.clear();
    for (const context of snapshot.contexts) this.contexts.set(context.requestId, structuredClone(context));
    for (const item of snapshot.cooldowns) this.cooldowns.set(item.key, { untilMs: item.untilMs, reason: item.reason });
    for (const item of snapshot.circuitBreakers) this.circuits.set(item.key, { failures: item.failures, openedAtMs: item.openedAtMs });
    this.revision = snapshot.revision;
  }

  private delay(context: RetryContext, error: ProviderErrorShape, nowMs: number): number {
    if (error.retryAfterMs !== null) return error.retryAfterMs;
    const exponent = Math.min(context.attempt - 1, 9);
    const cap = Math.min(MAX_BACKOFF_MS, BASE_DELAY_MS * Math.pow(2, Math.max(0, exponent)));
    return deterministicJitter(context.requestId + ":" + String(context.attempt) + ":" + String(nowMs), cap);
  }

  private key(provider: string, model: string): string {
    return provider.trim().toLowerCase() + ":" + model.trim().toLowerCase();
  }
}

