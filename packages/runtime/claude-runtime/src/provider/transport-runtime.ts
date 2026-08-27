import { createHash, randomUUID } from "node:crypto";

import { asBoolean, asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";
import type {
  ProviderTransport,
  ProviderTransportRequest,
  ProviderTransportResponse,
} from "./model-runtime.ts";

export const PROVIDER_TRANSPORT_SNAPSHOT_VERSION = "zyra.provider-transport/v1";

export type TransportRequestState = "queued" | "connecting" | "sending" | "receiving" | "completed" | "failed" | "cancelled";
export type EndpointHealth = "healthy" | "degraded" | "unavailable" | "probing";

export interface TransportEndpoint {
  endpointId: string;
  origin: string;
  weight: number;
  maximumConcurrency: number;
  activeRequests: number;
  queuedRequests: number;
  health: EndpointHealth;
  consecutiveFailures: number;
  consecutiveSuccesses: number;
  lastFailureAt: string | null;
  lastSuccessAt: string | null;
  unavailableUntil: string | null;
  averageLatencyMs: number;
  revision: number;
}

export interface TransportRequestRecord {
  requestId: string;
  endpointId: string;
  method: string;
  url: string;
  requestHeadersDigest: string;
  requestBodyDigest: string;
  state: TransportRequestState;
  attempt: number;
  status: number | null;
  responseHeadersDigest: string | null;
  responseBytes: number;
  errorCode: string | null;
  errorMessage: string | null;
  queuedAt: string;
  startedAt: string | null;
  firstByteAt: string | null;
  completedAt: string | null;
  revision: number;
}

export interface RateLimitWindow {
  key: string;
  requestLimit: number | null;
  requestRemaining: number | null;
  tokenLimit: number | null;
  tokenRemaining: number | null;
  resetAt: string | null;
  retryAfterMs: number | null;
  observedAt: string;
}

export interface ProviderTransportSnapshot {
  version: typeof PROVIDER_TRANSPORT_SNAPSHOT_VERSION;
  revision: number;
  endpoints: TransportEndpoint[];
  requests: TransportRequestRecord[];
  rateLimits: RateLimitWindow[];
  checksum: string;
}

export type FetchLike = (
  input: string | URL,
  init?: RequestInit,
) => Promise<Response>;

interface QueueWaiter {
  waiterId: string;
  endpointId: string;
  priority: number;
  enqueuedAt: number;
  resolve: () => void;
  reject: (error: Error) => void;
  signal: AbortSignal | null;
}

export class ProviderTransportRuntime implements ProviderTransport {
  private readonly fetcher: FetchLike;
  private readonly endpoints = new Map<string, TransportEndpoint>();
  private readonly requests = new Map<string, TransportRequestRecord>();
  private readonly rateLimits = new Map<string, RateLimitWindow>();
  private readonly waiters: QueueWaiter[] = [];
  private revision = 0;

  constructor(fetcher: FetchLike = fetch) {
    this.fetcher = fetcher;
  }

  transport_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "register_endpoint") return endpointToJson(this.registerEndpoint({
      origin: asString(value.origin),
      weight: number(value.weight, 1),
      maximumConcurrency: positive(value.maximum_concurrency, 16),
    }));
    if (action === "rate_limit") return rateLimitToJson(this.recordRateLimit(asString(value.key), stringRecord(asObject(value.headers))));
    if (action === "select") return endpointToJson(this.selectEndpoint(asString(value.origin) || null));
    return {
      endpoints: [...this.endpoints.values()].map(endpointToJson),
      active_requests: [...this.endpoints.values()].reduce((sum, endpoint) => sum + endpoint.activeRequests, 0),
      queued_requests: this.waiters.length,
      revision: this.revision,
    };
  }

  registerEndpoint(input: { origin: string; weight?: number; maximumConcurrency?: number }): TransportEndpoint {
    const origin = normalizeOrigin(input.origin);
    const existing = [...this.endpoints.values()].find((item) => item.origin === origin);
    if (existing) {
      existing.weight = boundedNumber(input.weight ?? existing.weight, 0.01, 1_000);
      existing.maximumConcurrency = boundedInteger(input.maximumConcurrency ?? existing.maximumConcurrency, 1, 1_024);
      existing.revision += 1;
      this.revision += 1;
      return structuredClone(existing);
    }
    const endpoint: TransportEndpoint = {
      endpointId: randomUUID(),
      origin,
      weight: boundedNumber(input.weight ?? 1, 0.01, 1_000),
      maximumConcurrency: boundedInteger(input.maximumConcurrency ?? 16, 1, 1_024),
      activeRequests: 0,
      queuedRequests: 0,
      health: "healthy",
      consecutiveFailures: 0,
      consecutiveSuccesses: 0,
      lastFailureAt: null,
      lastSuccessAt: null,
      unavailableUntil: null,
      averageLatencyMs: 0,
      revision: 1,
    };
    this.endpoints.set(endpoint.endpointId, endpoint);
    this.revision += 1;
    return structuredClone(endpoint);
  }

  removeEndpoint(endpointId: string): void {
    const endpoint = this.requireEndpoint(endpointId);
    if (endpoint.activeRequests > 0 || endpoint.queuedRequests > 0) throw new Error("cannot remove endpoint with active or queued requests");
    this.endpoints.delete(endpointId);
    this.revision += 1;
  }

  selectEndpoint(origin: string | null = null): TransportEndpoint {
    const now = Date.now();
    const candidates = [...this.endpoints.values()].filter((endpoint) => {
      if (origin && endpoint.origin !== normalizeOrigin(origin)) return false;
      if (endpoint.health === "unavailable" && endpoint.unavailableUntil && Date.parse(endpoint.unavailableUntil) > now) return false;
      if (endpoint.health === "unavailable") {
        endpoint.health = "probing";
        endpoint.revision += 1;
      }
      return true;
    });
    if (candidates.length === 0) {
      if (origin) return this.registerEndpoint({ origin });
      throw new Error("no provider transport endpoint is available");
    }
    candidates.sort((left, right) => endpointScore(right) - endpointScore(left) || left.endpointId.localeCompare(right.endpointId));
    return structuredClone(candidates[0]);
  }

  async execute(request: ProviderTransportRequest): Promise<ProviderTransportResponse> {
    const url = new URL(request.url);
    let endpoint = [...this.endpoints.values()].find((item) => item.origin === url.origin);
    if (!endpoint) {
      const registered = this.registerEndpoint({ origin: url.origin });
      endpoint = this.requireEndpoint(registered.endpointId);
    }
    const record: TransportRequestRecord = {
      requestId: header(request.headers, "x-client-request-id") ?? randomUUID(),
      endpointId: endpoint.endpointId,
      method: request.method,
      url: redactUrl(request.url),
      requestHeadersDigest: digest(redactHeaders(request.headers)),
      requestBodyDigest: digest(request.body),
      state: "queued",
      attempt: 1,
      status: null,
      responseHeadersDigest: null,
      responseBytes: 0,
      errorCode: null,
      errorMessage: null,
      queuedAt: new Date().toISOString(),
      startedAt: null,
      firstByteAt: null,
      completedAt: null,
      revision: 1,
    };
    if (this.requests.has(record.requestId)) throw new Error(`provider transport request already exists: ${record.requestId}`);
    this.requests.set(record.requestId, record);
    await this.acquireSlot(endpoint, request.signal ?? null, 0);
    record.state = "connecting";
    record.startedAt = new Date().toISOString();
    record.revision += 1;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(new Error("provider_transport_timeout")), Math.max(1, request.timeoutMs));
    const detach = relayAbort(request.signal, controller);
    let streamOwnsCleanup = false;
    let streamSettle: ((success: boolean) => void) | null = null;
    const cleanup = (): void => {
      clearTimeout(timeout);
      detach();
      controller.signal.removeEventListener("abort", settleAbortedStream);
    };
    const settleAbortedStream = (): void => streamSettle?.(false);
    controller.signal.addEventListener("abort", settleAbortedStream);
    try {
      record.state = "sending";
      record.revision += 1;
      const response = await this.fetcher(request.url, {
        method: request.method,
        headers: request.headers,
        body: request.body,
        signal: controller.signal,
        redirect: "manual",
      });
      record.state = "receiving";
      record.status = response.status;
      record.firstByteAt = new Date().toISOString();
      record.responseHeadersDigest = digest(headersToRecord(response.headers));
      record.revision += 1;
      const headers = headersToRecord(response.headers);
      this.recordRateLimit(url.origin, headers);
      const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
      if (contentType.includes("text/event-stream") && response.body) {
        let settled = false;
        const settle = (success: boolean): void => {
          if (settled) return;
          settled = true;
          if (controller.signal.aborted) {
            this.cancelRequest(record, endpoint!, controller.signal.reason);
          } else {
            this.finishRequest(record, endpoint!, success && response.ok);
          }
          cleanup();
        };
        streamSettle = settle;
        streamOwnsCleanup = true;
        const stream = readableStreamToAsyncIterable(response.body, (bytes) => {
          record.responseBytes += bytes;
          record.revision += 1;
        }, (completed) => settle(completed && response.ok), controller.signal);
        if (controller.signal.aborted) settle(false);
        return { status: response.status, headers, stream, settle };
      }
      const bytes = new Uint8Array(await response.arrayBuffer());
      record.responseBytes = bytes.byteLength;
      const body = parseBody(bytes, contentType);
      this.finishRequest(record, endpoint, response.ok);
      return { status: response.status, headers, body };
    } catch (error) {
      record.state = controller.signal.aborted ? "cancelled" : "failed";
      record.errorCode = controller.signal.aborted ? "transport_cancelled" : classifyTransportError(error);
      record.errorMessage = error instanceof Error ? error.message.slice(0, 8_192) : String(error).slice(0, 8_192);
      record.completedAt = new Date().toISOString();
      record.revision += 1;
      this.recordFailure(endpoint, record.errorCode);
      this.releaseSlot(endpoint);
      this.revision += 1;
      throw error;
    } finally {
      if (!streamOwnsCleanup) cleanup();
    }
  }

  recordRateLimit(key: string, headers: Readonly<Record<string, string>>): RateLimitWindow {
    const normalized = key.trim().toLowerCase();
    const observedAt = new Date().toISOString();
    const window: RateLimitWindow = {
      key: normalized,
      requestLimit: headerInteger(headers, "anthropic-ratelimit-requests-limit"),
      requestRemaining: headerInteger(headers, "anthropic-ratelimit-requests-remaining"),
      tokenLimit: headerInteger(headers, "anthropic-ratelimit-tokens-limit"),
      tokenRemaining: headerInteger(headers, "anthropic-ratelimit-tokens-remaining"),
      resetAt: normalizeResetHeader(header(headers, "anthropic-ratelimit-requests-reset") ?? header(headers, "x-ratelimit-reset")),
      retryAfterMs: retryAfterMs(header(headers, "retry-after")),
      observedAt,
    };
    this.rateLimits.set(normalized, window);
    this.revision += 1;
    return structuredClone(window);
  }

  rateLimitDelay(key: string, now = Date.now()): number {
    const window = this.rateLimits.get(key.trim().toLowerCase());
    if (!window) return 0;
    if (window.retryAfterMs !== null) return window.retryAfterMs;
    if (window.requestRemaining === 0 && window.resetAt) return Math.max(0, Date.parse(window.resetAt) - now);
    if (window.tokenRemaining === 0 && window.resetAt) return Math.max(0, Date.parse(window.resetAt) - now);
    return 0;
  }

  request(requestId: string): TransportRequestRecord {
    const record = this.requests.get(requestId);
    if (!record) throw new Error(`provider transport request not found: ${requestId}`);
    return structuredClone(record);
  }

  snapshot(): ProviderTransportSnapshot {
    if ([...this.endpoints.values()].some((endpoint) => endpoint.activeRequests > 0) || this.waiters.length > 0) {
      throw new Error("cannot snapshot provider transport with active or queued requests");
    }
    const unsigned: Omit<ProviderTransportSnapshot, "checksum"> = {
      version: PROVIDER_TRANSPORT_SNAPSHOT_VERSION,
      revision: this.revision,
      endpoints: structuredClone([...this.endpoints.values()]),
      requests: structuredClone([...this.requests.values()]),
      rateLimits: structuredClone([...this.rateLimits.values()]),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: ProviderTransportSnapshot): void {
    if (snapshot.version !== PROVIDER_TRANSPORT_SNAPSHOT_VERSION) throw new Error("unsupported provider transport snapshot version");
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("provider transport snapshot checksum mismatch");
    if (snapshot.endpoints.some((endpoint) => endpoint.activeRequests !== 0 || endpoint.queuedRequests !== 0)) {
      throw new Error("provider transport snapshot contains active work");
    }
    this.endpoints.clear();
    for (const endpoint of snapshot.endpoints) this.endpoints.set(endpoint.endpointId, structuredClone(endpoint));
    this.requests.clear();
    for (const request of snapshot.requests) this.requests.set(request.requestId, structuredClone(request));
    this.rateLimits.clear();
    for (const window of snapshot.rateLimits) this.rateLimits.set(window.key, structuredClone(window));
    this.waiters.splice(0, this.waiters.length);
    this.revision = snapshot.revision;
  }

  private async acquireSlot(endpoint: TransportEndpoint, signal: AbortSignal | null, priority: number): Promise<void> {
    if (endpoint.activeRequests < endpoint.maximumConcurrency) {
      endpoint.activeRequests += 1;
      endpoint.revision += 1;
      this.revision += 1;
      return;
    }
    endpoint.queuedRequests += 1;
    endpoint.revision += 1;
    this.revision += 1;
    await new Promise<void>((resolve, reject) => {
      const waiter: QueueWaiter = {
        waiterId: randomUUID(),
        endpointId: endpoint.endpointId,
        priority,
        enqueuedAt: Date.now(),
        resolve,
        reject,
        signal,
      };
      this.waiters.push(waiter);
      this.waiters.sort((left, right) => right.priority - left.priority || left.enqueuedAt - right.enqueuedAt);
      const abort = () => {
        const index = this.waiters.findIndex((item) => item.waiterId === waiter.waiterId);
        if (index >= 0) {
          this.waiters.splice(index, 1);
          endpoint.queuedRequests = Math.max(0, endpoint.queuedRequests - 1);
          endpoint.revision += 1;
          reject(new Error("provider transport queue aborted"));
        }
      };
      signal?.addEventListener("abort", abort, { once: true });
    });
  }

  private releaseSlot(endpoint: TransportEndpoint): void {
    endpoint.activeRequests = Math.max(0, endpoint.activeRequests - 1);
    const waiterIndex = this.waiters.findIndex((item) => item.endpointId === endpoint.endpointId && !item.signal?.aborted);
    if (waiterIndex >= 0) {
      const [waiter] = this.waiters.splice(waiterIndex, 1);
      endpoint.queuedRequests = Math.max(0, endpoint.queuedRequests - 1);
      endpoint.activeRequests += 1;
      waiter.resolve();
    }
    endpoint.revision += 1;
    this.revision += 1;
  }

  private finishRequest(record: TransportRequestRecord, endpoint: TransportEndpoint, success: boolean): void {
    if (record.state === "completed" || record.state === "failed" || record.state === "cancelled") return;
    record.state = success ? "completed" : "failed";
    record.completedAt = new Date().toISOString();
    record.errorCode = success ? null : `http_${record.status ?? 0}`;
    record.revision += 1;
    const latency = record.startedAt ? Math.max(0, Date.parse(record.completedAt) - Date.parse(record.startedAt)) : 0;
    if (success) this.recordSuccess(endpoint, latency);
    else this.recordFailure(endpoint, record.errorCode ?? "http_error");
    this.releaseSlot(endpoint);
    this.revision += 1;
  }

  private cancelRequest(record: TransportRequestRecord, endpoint: TransportEndpoint, reason: unknown): void {
    if (record.state === "completed" || record.state === "failed" || record.state === "cancelled") return;
    record.state = "cancelled";
    record.completedAt = new Date().toISOString();
    record.errorCode = "transport_cancelled";
    record.errorMessage = reason instanceof Error
      ? reason.message.slice(0, 8_192)
      : String(reason ?? "provider transport cancelled").slice(0, 8_192);
    record.revision += 1;
    this.recordFailure(endpoint, record.errorCode);
    this.releaseSlot(endpoint);
    this.revision += 1;
  }

  private recordSuccess(endpoint: TransportEndpoint, latencyMs: number): void {
    endpoint.consecutiveSuccesses += 1;
    endpoint.consecutiveFailures = 0;
    endpoint.lastSuccessAt = new Date().toISOString();
    endpoint.averageLatencyMs = endpoint.averageLatencyMs === 0
      ? latencyMs
      : Math.round(endpoint.averageLatencyMs * 0.8 + latencyMs * 0.2);
    if (endpoint.consecutiveSuccesses >= 2) {
      endpoint.health = "healthy";
      endpoint.unavailableUntil = null;
    }
    endpoint.revision += 1;
  }

  private recordFailure(endpoint: TransportEndpoint, code: string): void {
    endpoint.consecutiveFailures += 1;
    endpoint.consecutiveSuccesses = 0;
    endpoint.lastFailureAt = new Date().toISOString();
    if (endpoint.consecutiveFailures >= 5) {
      endpoint.health = "unavailable";
      endpoint.unavailableUntil = new Date(Date.now() + Math.min(300_000, 1_000 * 2 ** Math.min(8, endpoint.consecutiveFailures))).toISOString();
    } else if (endpoint.consecutiveFailures >= 2) endpoint.health = "degraded";
    if (/auth|permission|invalid_api_key/.test(code)) {
      endpoint.health = "unavailable";
      endpoint.unavailableUntil = new Date(Date.now() + 300_000).toISOString();
    }
    endpoint.revision += 1;
  }

  private requireEndpoint(endpointId: string): TransportEndpoint {
    const endpoint = this.endpoints.get(endpointId);
    if (!endpoint) throw new Error(`provider endpoint not found: ${endpointId}`);
    return endpoint;
  }
}

async function* readableStreamToAsyncIterable(
  stream: ReadableStream<Uint8Array>,
  onBytes: (bytes: number) => void,
  onClose: (completed: boolean) => void,
  signal: AbortSignal,
): AsyncIterable<Uint8Array> {
  const reader = stream.getReader();
  let closed = false;
  let completed = false;
  const close = (): void => {
    if (closed) return;
    closed = true;
    onClose(completed);
  };
  let rejectAbort: (reason: unknown) => void = () => {};
  const aborted = new Promise<never>((_resolve, reject) => {
    rejectAbort = reject;
  });
  const abort = (): void => {
    const reason = signal.reason ?? new DOMException("provider transport aborted", "AbortError");
    rejectAbort(reason);
    void reader.cancel(reason).catch(() => {});
  };
  if (signal.aborted) abort();
  else signal.addEventListener("abort", abort, { once: true });
  try {
    while (true) {
      const result = await Promise.race([reader.read(), aborted]);
      if (result.done) {
        completed = true;
        break;
      }
      onBytes(result.value.byteLength);
      yield result.value;
    }
  } finally {
    signal.removeEventListener("abort", abort);
    try {
      if (!completed) await reader.cancel(signal.reason).catch(() => {});
    } finally {
      try {
        reader.releaseLock();
      } finally {
        close();
      }
    }
  }
}

function relayAbort(source: AbortSignal | undefined, target: AbortController): () => void {
  if (!source) return () => {};
  const abort = () => target.abort(source.reason);
  if (source.aborted) abort();
  else source.addEventListener("abort", abort, { once: true });
  return () => source.removeEventListener("abort", abort);
}

function parseBody(bytes: Uint8Array, contentType: string): JsonValue {
  if (bytes.byteLength === 0) return null;
  const text = new TextDecoder().decode(bytes);
  if (contentType.includes("json")) {
    try {
      return JSON.parse(text) as JsonValue;
    } catch {
      return { malformed_json: true, text: text.slice(0, 65_536) };
    }
  }
  return { text: text.slice(0, 1_000_000), truncated: text.length > 1_000_000 };
}

function classifyTransportError(error: unknown): string {
  if (error instanceof DOMException && error.name === "AbortError") return "aborted";
  const message = error instanceof Error ? `${error.name}:${error.message}`.toLowerCase() : String(error).toLowerCase();
  if (/dns|enotfound|name.*resolve/.test(message)) return "dns_error";
  if (/certificate|tls|ssl/.test(message)) return "tls_error";
  if (/timeout|timed out|etimedout/.test(message)) return "timeout";
  if (/reset|econnreset|socket closed/.test(message)) return "connection_reset";
  if (/refused|econnrefused/.test(message)) return "connection_refused";
  if (/proxy/.test(message)) return "proxy_error";
  return "network_error";
}

function endpointScore(value: TransportEndpoint): number {
  const health = value.health === "healthy" ? 1 : value.health === "probing" ? 0.5 : value.health === "degraded" ? 0.25 : 0;
  const capacity = Math.max(0, value.maximumConcurrency - value.activeRequests) / value.maximumConcurrency;
  const latency = 1 / Math.max(1, value.averageLatencyMs || 1);
  return value.weight * health * (capacity + latency) - value.queuedRequests * 0.01;
}

function normalizeOrigin(value: string): string {
  const url = new URL(required(value, "endpoint origin"));
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error("endpoint origin must use HTTP or HTTPS");
  return url.origin;
}

function redactUrl(value: string): string {
  const url = new URL(value);
  for (const key of [...url.searchParams.keys()]) {
    if (/token|key|secret|signature|credential/i.test(key)) url.searchParams.set(key, "[redacted]");
  }
  return url.toString();
}

function headersToRecord(value: Headers): Readonly<Record<string, string>> {
  const result: Record<string, string> = {};
  value.forEach((item, key) => { result[key.toLowerCase()] = item; });
  return Object.freeze(result);
}

function redactHeaders(value: Readonly<Record<string, string>>): JsonObject {
  const result: JsonObject = {};
  for (const [key, item] of Object.entries(value)) result[key.toLowerCase()] = /authorization|api-key|token|cookie|signature/i.test(key) ? "[redacted]" : item;
  return result;
}

function stringRecord(value: JsonObject): Readonly<Record<string, string>> {
  const result: Record<string, string> = {};
  for (const [key, item] of Object.entries(value)) if (typeof item === "string") result[key] = item;
  return result;
}

function header(value: Readonly<Record<string, string>>, name: string): string | null {
  const target = name.toLowerCase();
  for (const [key, item] of Object.entries(value)) if (key.toLowerCase() === target) return item;
  return null;
}

function headerInteger(value: Readonly<Record<string, string>>, name: string): number | null {
  const raw = header(value, name);
  if (!raw) return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? Math.max(0, Math.floor(parsed)) : null;
}

function retryAfterMs(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, Math.floor(seconds * 1_000));
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? Math.max(0, timestamp - Date.now()) : null;
}

function normalizeResetHeader(value: string | null): string | null {
  if (!value) return null;
  const numeric = Number(value);
  if (Number.isFinite(numeric)) {
    const milliseconds = numeric > 10_000_000_000 ? numeric : numeric * 1_000;
    return new Date(milliseconds).toISOString();
  }
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null;
}

function endpointToJson(value: TransportEndpoint): JsonObject {
  return {
    endpoint_id: value.endpointId,
    origin: value.origin,
    weight: value.weight,
    maximum_concurrency: value.maximumConcurrency,
    active_requests: value.activeRequests,
    queued_requests: value.queuedRequests,
    health: value.health,
    consecutive_failures: value.consecutiveFailures,
    consecutive_successes: value.consecutiveSuccesses,
    last_failure_at: value.lastFailureAt,
    last_success_at: value.lastSuccessAt,
    unavailable_until: value.unavailableUntil,
    average_latency_ms: value.averageLatencyMs,
    revision: value.revision,
  };
}

function rateLimitToJson(value: RateLimitWindow): JsonObject {
  return {
    key: value.key,
    request_limit: value.requestLimit,
    request_remaining: value.requestRemaining,
    token_limit: value.tokenLimit,
    token_remaining: value.tokenRemaining,
    reset_at: value.resetAt,
    retry_after_ms: value.retryAfterMs,
    observed_at: value.observedAt,
  };
}

function required(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${name} is required`);
  return normalized;
}

function positive(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function number(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function boundedInteger(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, Math.floor(value)));
}

function boundedNumber(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value));
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function digest(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}
