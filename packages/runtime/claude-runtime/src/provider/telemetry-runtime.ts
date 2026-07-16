import { createHash, randomUUID } from "node:crypto";

import { asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";
import type { ModelDescriptor, ProviderUsage } from "./model-runtime.ts";

export const PROVIDER_TELEMETRY_SNAPSHOT_VERSION = "zyra.provider-telemetry/v1";
export const CACHE_TTL_1HOUR_MS = 60 * 60 * 1_000;
export const CACHE_TTL_5MIN_MS = 5 * 60 * 1_000;

export type TelemetryLevel = "debug" | "info" | "warn" | "error";
export type TelemetryOutcome = "started" | "succeeded" | "failed" | "cancelled";
export type KnownGateway = "vercel" | "cloudflare" | "kong" | "braintrust" | "databricks";
export type CacheBreakKind =
  | "system_changed"
  | "tools_changed"
  | "messages_rewritten"
  | "model_changed"
  | "ttl_expired"
  | "compacted"
  | "manual_invalidation"
  | "none";

export interface UsageSample {
  sampleId: string;
  requestId: string;
  sessionId: string;
  runId: string;
  taskId: string;
  model: string;
  provider: string;
  usage: ProviderUsage;
  inputCostUsd: number;
  outputCostUsd: number;
  cacheReadCostUsd: number;
  cacheWriteCostUsd: number;
  totalCostUsd: number;
  durationMs: number;
  firstTokenMs: number | null;
  success: boolean;
  stopReason: string | null;
  errorCode: string | null;
  createdAt: string;
}

export interface SessionCostState {
  sessionId: string;
  runIds: string[];
  requestCount: number;
  successfulRequests: number;
  failedRequests: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadInputTokens: number;
  cacheCreationInputTokens: number;
  serverToolUseTokens: number;
  inputCostUsd: number;
  outputCostUsd: number;
  cacheReadCostUsd: number;
  cacheWriteCostUsd: number;
  totalCostUsd: number;
  firstSeenAt: string;
  updatedAt: string;
}

export interface PromptStateSnapshot {
  key: string;
  sessionId: string;
  agentId: string | null;
  model: string;
  requestId: string;
  systemHash: string;
  toolsHash: string;
  messagesHash: string;
  systemChars: number;
  toolsChars: number;
  messagesChars: number;
  toolHashes: Readonly<Record<string, string>>;
  cacheControlCount: number;
  compactGeneration: number;
  recordedAt: string;
  expiresAt: string;
}

export interface PromptCacheBreak {
  breakId: string;
  key: string;
  previousRequestId: string;
  currentRequestId: string;
  kind: CacheBreakKind;
  changedTools: string[];
  removedTools: string[];
  addedTools: string[];
  systemCharDelta: number;
  toolsCharDelta: number;
  messagesCharDelta: number;
  previousDigest: string;
  currentDigest: string;
  compactGeneration: number;
  detectedAt: string;
}

export interface TelemetryEvent {
  eventId: string;
  sequence: number;
  level: TelemetryLevel;
  name: string;
  outcome: TelemetryOutcome | null;
  sessionId: string | null;
  runId: string | null;
  taskId: string | null;
  requestId: string | null;
  parentEventId: string | null;
  durationMs: number | null;
  summary: string;
  attributes: JsonObject;
  createdAt: string;
}

export interface TelemetrySpan {
  spanId: string;
  name: string;
  sessionId: string | null;
  runId: string | null;
  taskId: string | null;
  requestId: string | null;
  parentSpanId: string | null;
  startedAt: number;
  firstTokenAt: number | null;
  attributes: JsonObject;
  closed: boolean;
}

export interface MetricHistogram {
  name: string;
  boundaries: number[];
  buckets: number[];
  count: number;
  sum: number;
  minimum: number | null;
  maximum: number | null;
}

export interface ProviderTelemetrySnapshot {
  version: typeof PROVIDER_TELEMETRY_SNAPSHOT_VERSION;
  revision: number;
  sequence: number;
  compactGeneration: number;
  samples: UsageSample[];
  sessions: SessionCostState[];
  prompts: PromptStateSnapshot[];
  breaks: PromptCacheBreak[];
  events: TelemetryEvent[];
  histograms: MetricHistogram[];
  checksum: string;
}

interface LogInput {
  level: TelemetryLevel;
  name: string;
  outcome?: TelemetryOutcome;
  sessionId?: string;
  runId?: string;
  taskId?: string;
  requestId?: string;
  parentEventId?: string;
  durationMs?: number;
  summary?: string;
  attributes?: JsonObject;
}

const DEFAULT_BOUNDARIES = [1, 5, 10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 30_000, 60_000];

export class ProviderTelemetryRuntime {
  private readonly samples: UsageSample[] = [];
  private readonly sessions = new Map<string, SessionCostState>();
  private readonly prompts = new Map<string, PromptStateSnapshot>();
  private readonly breaks: PromptCacheBreak[] = [];
  private readonly events: TelemetryEvent[] = [];
  private readonly spans = new Map<string, TelemetrySpan>();
  private readonly histograms = new Map<string, MetricHistogram>();
  private readonly maximumEvents: number;
  private readonly maximumSamples: number;
  private readonly maximumBreaks: number;
  private revision = 0;
  private sequence = 0;
  private compactGeneration = 0;

  constructor(options: {
    maximumEvents?: number;
    maximumSamples?: number;
    maximumBreaks?: number;
  } = {}) {
    this.maximumEvents = boundedInteger(options.maximumEvents, 100, 100_000, 10_000);
    this.maximumSamples = boundedInteger(options.maximumSamples, 10, 100_000, 5_000);
    this.maximumBreaks = boundedInteger(options.maximumBreaks, 10, 20_000, 1_000);
    this.registerHistogram("provider.request.duration_ms", DEFAULT_BOUNDARIES);
    this.registerHistogram("provider.request.first_token_ms", DEFAULT_BOUNDARIES);
    this.registerHistogram("provider.request.input_tokens", tokenBoundaries());
    this.registerHistogram("provider.request.output_tokens", tokenBoundaries());
    this.registerHistogram("provider.request.cost_usd_micros", costBoundaries());
  }

  cost_tracker_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "summary");
    if (action === "record") {
      const sample = this.recordUsage(usageInputFromJson(value));
      return sampleToJson(sample);
    }
    if (action === "session") return sessionToJson(this.sessionCost(asString(value.session_id)));
    if (action === "format") return { text: this.formatTotalCost(asString(value.session_id)) };
    return this.costSummary(asString(value.session_id, ""));
  }

  promptCacheBreakDetection_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "record") {
      const result = this.recordPromptState(promptInputFromJson(value));
      return {
        snapshot: promptToJson(result.snapshot),
        cache_break: result.cacheBreak ? breakToJson(result.cacheBreak) : null,
      };
    }
    if (action === "compact") {
      this.notifyCompaction(asString(value.session_id));
      return { compact_generation: this.compactGeneration };
    }
    if (action === "delete") {
      const removed = this.notifyCacheDeletion(asString(value.session_id), asString(value.agent_id, ""));
      return { removed };
    }
    return {
      prompt_count: this.prompts.size,
      break_count: this.breaks.length,
      compact_generation: this.compactGeneration,
    };
  }

  detectGateway(input: {
    headers?: Readonly<Record<string, string>>;
    baseUrl?: string | null;
  }): KnownGateway | null {
    const fingerprints: Record<Exclude<KnownGateway, "databricks">, string[]> = {
      vercel: ["x-vercel-"],
      cloudflare: ["cf-", "x-cloudflare-"],
      kong: ["x-kong-"],
      braintrust: ["x-bt-"],
    };
    const headerNames = Object.keys(input.headers ?? {}).map((name) => name.toLowerCase());
    for (const [gateway, prefixes] of Object.entries(fingerprints)) {
      if (prefixes.some((prefix) => headerNames.some((header) => header.startsWith(prefix)))) {
        return gateway as KnownGateway;
      }
    }
    if (input.baseUrl) {
      try {
        const host = new URL(input.baseUrl).hostname.toLowerCase();
        if ([".cloud.databricks.com", ".azuredatabricks.net", ".gcp.databricks.com"]
          .some((suffix) => host.endsWith(suffix))) return "databricks";
      } catch {
        return null;
      }
    }
    return null;
  }

  logging_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "log");
    if (action === "start_span") {
      const span = this.startSpan({
        name: asString(value.name, "provider.request"),
        sessionId: nullableString(value.session_id),
        runId: nullableString(value.run_id),
        taskId: nullableString(value.task_id),
        requestId: nullableString(value.request_id),
        parentSpanId: nullableString(value.parent_span_id),
        attributes: asObject(value.attributes),
      });
      return spanToJson(span);
    }
    if (action === "first_token") {
      this.markFirstToken(asString(value.span_id));
      return { accepted: true };
    }
    if (action === "finish_span") {
      const event = this.finishSpan(
        asString(value.span_id),
        outcome(asString(value.outcome, "succeeded")),
        asObject(value.attributes),
      );
      return eventToJson(event);
    }
    const event = this.log({
      level: level(asString(value.level, "info")),
      name: asString(value.name, "provider.event"),
      outcome: value.outcome ? outcome(asString(value.outcome)) : undefined,
      sessionId: nullableString(value.session_id) ?? undefined,
      runId: nullableString(value.run_id) ?? undefined,
      taskId: nullableString(value.task_id) ?? undefined,
      requestId: nullableString(value.request_id) ?? undefined,
      summary: asString(value.summary),
      attributes: asObject(value.attributes),
    });
    return eventToJson(event);
  }

  recordUsage(input: {
    requestId: string;
    sessionId: string;
    runId: string;
    taskId: string;
    model: ModelDescriptor;
    usage: ProviderUsage;
    durationMs: number;
    firstTokenMs: number | null;
    success: boolean;
    stopReason: string | null;
    errorCode: string | null;
    createdAt?: string;
  }): UsageSample {
    if (this.samples.some((sample) => sample.requestId === input.requestId)) {
      throw new Error(`provider usage already recorded for request ${input.requestId}`);
    }
    const cost = calculateUsageCost(input.model, input.usage);
    const sample: UsageSample = {
      sampleId: randomUUID(),
      requestId: required(input.requestId, "request id"),
      sessionId: required(input.sessionId, "session id"),
      runId: required(input.runId, "run id"),
      taskId: required(input.taskId, "task id"),
      model: input.model.id,
      provider: input.model.provider,
      usage: normalizeUsage(input.usage),
      inputCostUsd: cost.input,
      outputCostUsd: cost.output,
      cacheReadCostUsd: cost.cacheRead,
      cacheWriteCostUsd: cost.cacheWrite,
      totalCostUsd: cost.total,
      durationMs: nonnegative(input.durationMs),
      firstTokenMs: input.firstTokenMs === null ? null : nonnegative(input.firstTokenMs),
      success: input.success,
      stopReason: input.stopReason,
      errorCode: input.errorCode,
      createdAt: normalizeTimestamp(input.createdAt),
    };
    this.samples.push(sample);
    trimHead(this.samples, this.maximumSamples);
    this.mergeSession(sample);
    this.observe("provider.request.duration_ms", sample.durationMs);
    if (sample.firstTokenMs !== null) this.observe("provider.request.first_token_ms", sample.firstTokenMs);
    this.observe("provider.request.input_tokens", sample.usage.inputTokens);
    this.observe("provider.request.output_tokens", sample.usage.outputTokens);
    this.observe("provider.request.cost_usd_micros", sample.totalCostUsd * 1_000_000);
    this.revision += 1;
    this.log({
      level: sample.success ? "info" : "error",
      name: sample.success ? "provider.request.succeeded" : "provider.request.failed",
      outcome: sample.success ? "succeeded" : "failed",
      sessionId: sample.sessionId,
      runId: sample.runId,
      taskId: sample.taskId,
      requestId: sample.requestId,
      durationMs: sample.durationMs,
      summary: sample.success ? "provider request completed" : "provider request failed",
      attributes: {
        model: sample.model,
        provider: sample.provider,
        stop_reason: sample.stopReason,
        error_code: sample.errorCode,
        usage: usageToJson(sample.usage),
        total_cost_usd: sample.totalCostUsd,
      },
    });
    return structuredClone(sample);
  }

  sessionCost(sessionId: string): SessionCostState {
    const state = this.sessions.get(sessionId);
    if (!state) return emptySession(sessionId);
    return structuredClone(state);
  }

  costSummary(sessionId = ""): JsonObject {
    const selected = sessionId
      ? [this.sessionCost(sessionId)]
      : [...this.sessions.values()].map((item) => structuredClone(item));
    const totals = selected.reduce((accumulator, item) => ({
      requests: accumulator.requests + item.requestCount,
      inputTokens: accumulator.inputTokens + item.inputTokens,
      outputTokens: accumulator.outputTokens + item.outputTokens,
      cacheReadTokens: accumulator.cacheReadTokens + item.cacheReadInputTokens,
      cacheWriteTokens: accumulator.cacheWriteTokens + item.cacheCreationInputTokens,
      totalCost: accumulator.totalCost + item.totalCostUsd,
    }), { requests: 0, inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, totalCost: 0 });
    return {
      session_id: sessionId || null,
      request_count: totals.requests,
      input_tokens: totals.inputTokens,
      output_tokens: totals.outputTokens,
      cache_read_input_tokens: totals.cacheReadTokens,
      cache_creation_input_tokens: totals.cacheWriteTokens,
      total_cost_usd: roundCurrency(totals.totalCost),
    };
  }

  formatTotalCost(sessionId = ""): string {
    const summary = this.costSummary(sessionId);
    const dollars = Number(summary.total_cost_usd ?? 0);
    const input = Number(summary.input_tokens ?? 0);
    const output = Number(summary.output_tokens ?? 0);
    const requests = Number(summary.request_count ?? 0);
    return `$${formatCost(dollars)} (${formatCount(input)} input, ${formatCount(output)} output, ${requests} requests)`;
  }

  recordPromptState(input: {
    sessionId: string;
    agentId: string | null;
    requestId: string;
    model: string;
    system: JsonValue;
    tools: JsonValue;
    messages: JsonValue;
    cacheTtlMs?: number;
    recordedAt?: string;
  }): { snapshot: PromptStateSnapshot; cacheBreak: PromptCacheBreak | null } {
    const recordedAt = normalizeTimestamp(input.recordedAt);
    const key = trackingKey(input.sessionId, input.agentId);
    const tools = normalizeToolArray(input.tools);
    const snapshot: PromptStateSnapshot = {
      key,
      sessionId: required(input.sessionId, "session id"),
      agentId: input.agentId,
      model: required(input.model, "model"),
      requestId: required(input.requestId, "request id"),
      systemHash: digest(stripCacheControl(input.system)),
      toolsHash: digest(stripCacheControl(tools)),
      messagesHash: digest(stripCacheControl(input.messages)),
      systemChars: jsonChars(input.system),
      toolsChars: jsonChars(tools),
      messagesChars: jsonChars(input.messages),
      toolHashes: computeToolHashes(tools),
      cacheControlCount: countCacheControls([input.system, tools, input.messages]),
      compactGeneration: this.compactGeneration,
      recordedAt,
      expiresAt: new Date(Date.parse(recordedAt) + boundedInteger(input.cacheTtlMs, 1_000, CACHE_TTL_1HOUR_MS, CACHE_TTL_5MIN_MS)).toISOString(),
    };
    const previous = this.prompts.get(key);
    const cacheBreak = previous ? detectPromptBreak(previous, snapshot) : null;
    this.prompts.set(key, snapshot);
    if (cacheBreak && cacheBreak.kind !== "none") {
      this.breaks.push(cacheBreak);
      trimHead(this.breaks, this.maximumBreaks);
      this.log({
        level: "warn",
        name: "provider.prompt_cache.break",
        outcome: "succeeded",
        sessionId: snapshot.sessionId,
        requestId: snapshot.requestId,
        summary: `prompt cache break detected: ${cacheBreak.kind}`,
        attributes: breakToJson(cacheBreak),
      });
    }
    this.revision += 1;
    return {
      snapshot: structuredClone(snapshot),
      cacheBreak: cacheBreak ? structuredClone(cacheBreak) : null,
    };
  }

  notifyCompaction(sessionId: string): void {
    this.compactGeneration += 1;
    for (const [key, snapshot] of this.prompts) {
      if (snapshot.sessionId !== sessionId) continue;
      this.prompts.set(key, { ...snapshot, compactGeneration: this.compactGeneration });
    }
    this.log({
      level: "info",
      name: "provider.prompt_cache.compacted",
      outcome: "succeeded",
      sessionId,
      summary: "prompt cache generation advanced after compaction",
      attributes: { compact_generation: this.compactGeneration },
    });
    this.revision += 1;
  }

  notifyCacheDeletion(sessionId: string, agentId = ""): number {
    let removed = 0;
    for (const [key, snapshot] of this.prompts) {
      if (snapshot.sessionId !== sessionId) continue;
      if (agentId && snapshot.agentId !== agentId) continue;
      this.prompts.delete(key);
      removed += 1;
    }
    if (removed > 0) {
      this.log({
        level: "info",
        name: "provider.prompt_cache.deleted",
        outcome: "succeeded",
        sessionId,
        summary: "prompt cache tracking state deleted",
        attributes: { agent_id: agentId || null, removed },
      });
      this.revision += 1;
    }
    return removed;
  }

  cleanupAgentTracking(sessionId: string, agentId: string): number {
    return this.notifyCacheDeletion(sessionId, agentId);
  }

  startSpan(input: {
    name: string;
    sessionId?: string | null;
    runId?: string | null;
    taskId?: string | null;
    requestId?: string | null;
    parentSpanId?: string | null;
    attributes?: JsonObject;
  }): TelemetrySpan {
    const span: TelemetrySpan = {
      spanId: randomUUID(),
      name: required(input.name, "span name"),
      sessionId: input.sessionId ?? null,
      runId: input.runId ?? null,
      taskId: input.taskId ?? null,
      requestId: input.requestId ?? null,
      parentSpanId: input.parentSpanId ?? null,
      startedAt: Date.now(),
      firstTokenAt: null,
      attributes: sanitizeAttributes(input.attributes ?? {}),
      closed: false,
    };
    if (span.parentSpanId) {
      const parent = this.spans.get(span.parentSpanId);
      if (!parent || parent.closed) throw new Error(`active parent span not found: ${span.parentSpanId}`);
    }
    this.spans.set(span.spanId, span);
    this.log({
      level: "debug",
      name: `${span.name}.started`,
      outcome: "started",
      sessionId: span.sessionId ?? undefined,
      runId: span.runId ?? undefined,
      taskId: span.taskId ?? undefined,
      requestId: span.requestId ?? undefined,
      summary: `${span.name} started`,
      attributes: { ...span.attributes, span_id: span.spanId, parent_span_id: span.parentSpanId },
    });
    return structuredClone(span);
  }

  markFirstToken(spanId: string): void {
    const span = this.requireSpan(spanId);
    if (span.closed) throw new Error(`span already closed: ${spanId}`);
    if (span.firstTokenAt === null) {
      span.firstTokenAt = Date.now();
      this.observe("provider.request.first_token_ms", span.firstTokenAt - span.startedAt);
      this.revision += 1;
    }
  }

  finishSpan(spanId: string, result: TelemetryOutcome, attributes: JsonObject = {}): TelemetryEvent {
    const span = this.requireSpan(spanId);
    if (span.closed) throw new Error(`span already closed: ${spanId}`);
    const duration = Math.max(0, Date.now() - span.startedAt);
    span.closed = true;
    span.attributes = { ...span.attributes, ...sanitizeAttributes(attributes) };
    this.observe("provider.request.duration_ms", duration);
    const event = this.log({
      level: result === "failed" ? "error" : result === "cancelled" ? "warn" : "info",
      name: `${span.name}.${result}`,
      outcome: result,
      sessionId: span.sessionId ?? undefined,
      runId: span.runId ?? undefined,
      taskId: span.taskId ?? undefined,
      requestId: span.requestId ?? undefined,
      durationMs: duration,
      summary: `${span.name} ${result}`,
      attributes: {
        ...span.attributes,
        span_id: span.spanId,
        parent_span_id: span.parentSpanId,
        first_token_ms: span.firstTokenAt === null ? null : span.firstTokenAt - span.startedAt,
      },
    });
    this.spans.delete(spanId);
    this.revision += 1;
    return event;
  }

  log(input: LogInput): TelemetryEvent {
    this.sequence += 1;
    const event: TelemetryEvent = {
      eventId: randomUUID(),
      sequence: this.sequence,
      level: input.level,
      name: required(input.name, "event name"),
      outcome: input.outcome ?? null,
      sessionId: input.sessionId ?? null,
      runId: input.runId ?? null,
      taskId: input.taskId ?? null,
      requestId: input.requestId ?? null,
      parentEventId: input.parentEventId ?? null,
      durationMs: input.durationMs === undefined ? null : nonnegative(input.durationMs),
      summary: (input.summary ?? input.name).trim().slice(0, 2_048),
      attributes: sanitizeAttributes(input.attributes ?? {}),
      createdAt: new Date().toISOString(),
    };
    this.events.push(event);
    trimHead(this.events, this.maximumEvents);
    this.revision += 1;
    return structuredClone(event);
  }

  registerHistogram(name: string, boundaries: readonly number[]): void {
    const normalized = [...new Set(boundaries.map(nonnegative))].sort((left, right) => left - right);
    if (normalized.length === 0) throw new Error("histogram requires a boundary");
    this.histograms.set(name, {
      name,
      boundaries: normalized,
      buckets: new Array(normalized.length + 1).fill(0),
      count: 0,
      sum: 0,
      minimum: null,
      maximum: null,
    });
  }

  observe(name: string, rawValue: number): void {
    const histogram = this.histograms.get(name);
    if (!histogram) throw new Error(`histogram is not registered: ${name}`);
    const value = nonnegative(rawValue);
    let index = histogram.boundaries.findIndex((boundary) => value <= boundary);
    if (index < 0) index = histogram.boundaries.length;
    histogram.buckets[index] += 1;
    histogram.count += 1;
    histogram.sum += value;
    histogram.minimum = histogram.minimum === null ? value : Math.min(histogram.minimum, value);
    histogram.maximum = histogram.maximum === null ? value : Math.max(histogram.maximum, value);
  }

  histogram(name: string): MetricHistogram {
    const value = this.histograms.get(name);
    if (!value) throw new Error(`histogram is not registered: ${name}`);
    return structuredClone(value);
  }

  snapshot(): ProviderTelemetrySnapshot {
    if (this.spans.size > 0) {
      throw new Error("cannot snapshot provider telemetry while spans are active");
    }
    const unsigned: Omit<ProviderTelemetrySnapshot, "checksum"> = {
      version: PROVIDER_TELEMETRY_SNAPSHOT_VERSION,
      revision: this.revision,
      sequence: this.sequence,
      compactGeneration: this.compactGeneration,
      samples: structuredClone(this.samples),
      sessions: structuredClone([...this.sessions.values()]),
      prompts: structuredClone([...this.prompts.values()]),
      breaks: structuredClone(this.breaks),
      events: structuredClone(this.events),
      histograms: structuredClone([...this.histograms.values()]),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: ProviderTelemetrySnapshot): void {
    if (snapshot.version !== PROVIDER_TELEMETRY_SNAPSHOT_VERSION) {
      throw new Error("unsupported provider telemetry snapshot version");
    }
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("provider telemetry snapshot checksum mismatch");
    this.samples.splice(0, this.samples.length, ...structuredClone(snapshot.samples));
    this.sessions.clear();
    for (const item of snapshot.sessions) this.sessions.set(item.sessionId, structuredClone(item));
    this.prompts.clear();
    for (const item of snapshot.prompts) this.prompts.set(item.key, structuredClone(item));
    this.breaks.splice(0, this.breaks.length, ...structuredClone(snapshot.breaks));
    this.events.splice(0, this.events.length, ...structuredClone(snapshot.events));
    this.histograms.clear();
    for (const item of snapshot.histograms) this.histograms.set(item.name, structuredClone(item));
    this.spans.clear();
    this.revision = snapshot.revision;
    this.sequence = snapshot.sequence;
    this.compactGeneration = snapshot.compactGeneration;
  }

  private mergeSession(sample: UsageSample): void {
    const existing = this.sessions.get(sample.sessionId) ?? emptySession(sample.sessionId, sample.createdAt);
    if (!existing.runIds.includes(sample.runId)) existing.runIds.push(sample.runId);
    existing.requestCount += 1;
    existing.successfulRequests += sample.success ? 1 : 0;
    existing.failedRequests += sample.success ? 0 : 1;
    existing.inputTokens += sample.usage.inputTokens;
    existing.outputTokens += sample.usage.outputTokens;
    existing.cacheReadInputTokens += sample.usage.cacheReadInputTokens;
    existing.cacheCreationInputTokens += sample.usage.cacheCreationInputTokens;
    existing.serverToolUseTokens += sample.usage.serverToolUseTokens;
    existing.inputCostUsd = roundCurrency(existing.inputCostUsd + sample.inputCostUsd);
    existing.outputCostUsd = roundCurrency(existing.outputCostUsd + sample.outputCostUsd);
    existing.cacheReadCostUsd = roundCurrency(existing.cacheReadCostUsd + sample.cacheReadCostUsd);
    existing.cacheWriteCostUsd = roundCurrency(existing.cacheWriteCostUsd + sample.cacheWriteCostUsd);
    existing.totalCostUsd = roundCurrency(existing.totalCostUsd + sample.totalCostUsd);
    existing.updatedAt = sample.createdAt;
    this.sessions.set(sample.sessionId, existing);
  }

  private requireSpan(spanId: string): TelemetrySpan {
    const span = this.spans.get(spanId);
    if (!span) throw new Error(`active span not found: ${spanId}`);
    return span;
  }
}

export function calculateUsageCost(
  model: ModelDescriptor,
  usage: ProviderUsage,
): { input: number; output: number; cacheRead: number; cacheWrite: number; total: number } {
  const input = costForTokens(usage.inputTokens, model.inputPricePerMillion);
  const output = costForTokens(usage.outputTokens, model.outputPricePerMillion);
  const cacheRead = costForTokens(usage.cacheReadInputTokens, model.cacheReadPricePerMillion);
  const cacheWrite = costForTokens(usage.cacheCreationInputTokens, model.cacheWritePricePerMillion);
  return {
    input,
    output,
    cacheRead,
    cacheWrite,
    total: roundCurrency(input + output + cacheRead + cacheWrite),
  };
}

function detectPromptBreak(
  previous: PromptStateSnapshot,
  current: PromptStateSnapshot,
): PromptCacheBreak | null {
  let kind: CacheBreakKind = "none";
  if (Date.parse(current.recordedAt) >= Date.parse(previous.expiresAt)) kind = "ttl_expired";
  else if (current.compactGeneration !== previous.compactGeneration) kind = "compacted";
  else if (current.model !== previous.model) kind = "model_changed";
  else if (current.systemHash !== previous.systemHash) kind = "system_changed";
  else if (current.toolsHash !== previous.toolsHash) kind = "tools_changed";
  else if (current.messagesHash !== previous.messagesHash) kind = "messages_rewritten";
  if (kind === "none") return null;
  const previousTools = new Set(Object.keys(previous.toolHashes));
  const currentTools = new Set(Object.keys(current.toolHashes));
  const changedTools = [...currentTools].filter((name) => previousTools.has(name) && previous.toolHashes[name] !== current.toolHashes[name]);
  const removedTools = [...previousTools].filter((name) => !currentTools.has(name));
  const addedTools = [...currentTools].filter((name) => !previousTools.has(name));
  return {
    breakId: randomUUID(),
    key: current.key,
    previousRequestId: previous.requestId,
    currentRequestId: current.requestId,
    kind,
    changedTools: changedTools.sort(),
    removedTools: removedTools.sort(),
    addedTools: addedTools.sort(),
    systemCharDelta: current.systemChars - previous.systemChars,
    toolsCharDelta: current.toolsChars - previous.toolsChars,
    messagesCharDelta: current.messagesChars - previous.messagesChars,
    previousDigest: digest(promptToJson(previous)),
    currentDigest: digest(promptToJson(current)),
    compactGeneration: current.compactGeneration,
    detectedAt: current.recordedAt,
  };
}

function stripCacheControl(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map(stripCacheControl);
  if (value && typeof value === "object") {
    const result: JsonObject = {};
    for (const [key, item] of Object.entries(value)) {
      if (key === "cache_control" || key === "cacheControl") continue;
      result[key] = stripCacheControl(item);
    }
    return result;
  }
  return value;
}

function normalizeToolArray(value: JsonValue): JsonValue[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => stripCacheControl(item))
    .sort((left, right) => toolName(left).localeCompare(toolName(right)));
}

function computeToolHashes(value: readonly JsonValue[]): Readonly<Record<string, string>> {
  const hashes: Record<string, string> = {};
  for (const item of value) {
    const name = toolName(item);
    if (!name) continue;
    hashes[name] = digest(item);
  }
  return Object.freeze(hashes);
}

function toolName(value: JsonValue): string {
  return asString(asObject(value).name);
}

function countCacheControls(value: JsonValue): number {
  if (Array.isArray(value)) return value.reduce<number>((sum, item) => sum + countCacheControls(item), 0);
  if (value && typeof value === "object") {
    let count = 0;
    for (const [key, item] of Object.entries(value)) {
      if (key === "cache_control" || key === "cacheControl") count += 1;
      count += countCacheControls(item);
    }
    return count;
  }
  return 0;
}

function sanitizeAttributes(value: JsonObject): JsonObject {
  const result: JsonObject = {};
  for (const [key, item] of Object.entries(value)) {
    const normalizedKey = key.trim().slice(0, 128);
    if (!normalizedKey) continue;
    if (/password|secret|api.?key|authorization|access.?token|cookie/i.test(normalizedKey)) {
      result[normalizedKey] = "[redacted]";
      continue;
    }
    result[normalizedKey] = sanitizeValue(item, 0);
  }
  return result;
}

function sanitizeValue(value: JsonValue, depth: number): JsonValue {
  if (depth >= 8) return "[depth-limited]";
  if (typeof value === "string") return value.length > 8_192 ? `${value.slice(0, 8_192)}...[truncated]` : value;
  if (Array.isArray(value)) return value.slice(0, 256).map((item) => sanitizeValue(item, depth + 1));
  if (value && typeof value === "object") {
    const result: JsonObject = {};
    for (const [key, item] of Object.entries(value).slice(0, 256)) {
      if (/password|secret|api.?key|authorization|access.?token|cookie/i.test(key)) result[key] = "[redacted]";
      else result[key] = sanitizeValue(item, depth + 1);
    }
    return result;
  }
  return value;
}

function usageInputFromJson(value: JsonObject): Parameters<ProviderTelemetryRuntime["recordUsage"]>[0] {
  const model = asObject(value.model);
  const usage = asObject(value.usage);
  return {
    requestId: asString(value.request_id),
    sessionId: asString(value.session_id),
    runId: asString(value.run_id),
    taskId: asString(value.task_id),
    model: {
      id: asString(model.id),
      canonicalName: asString(model.canonical_name, asString(model.id)),
      provider: provider(asString(model.provider, "anthropic")),
      contextWindow: integer(model.context_window, 200_000),
      maxOutputTokens: integer(model.max_output_tokens, 8_192),
      inputPricePerMillion: number(model.input_price_per_million, 0),
      outputPricePerMillion: number(model.output_price_per_million, 0),
      cacheReadPricePerMillion: number(model.cache_read_price_per_million, 0),
      cacheWritePricePerMillion: number(model.cache_write_price_per_million, 0),
      capabilities: ["text"],
      aliases: [],
      deprecated: false,
      replacement: null,
    },
    usage: usageFromJson(usage),
    durationMs: number(value.duration_ms, 0),
    firstTokenMs: value.first_token_ms === null || value.first_token_ms === undefined ? null : number(value.first_token_ms, 0),
    success: value.success !== false,
    stopReason: nullableString(value.stop_reason),
    errorCode: nullableString(value.error_code),
  };
}

function promptInputFromJson(value: JsonObject): Parameters<ProviderTelemetryRuntime["recordPromptState"]>[0] {
  return {
    sessionId: asString(value.session_id),
    agentId: nullableString(value.agent_id),
    requestId: asString(value.request_id),
    model: asString(value.model),
    system: value.system ?? [],
    tools: value.tools ?? [],
    messages: value.messages ?? [],
    cacheTtlMs: integer(value.cache_ttl_ms, CACHE_TTL_5MIN_MS),
  };
}

function emptySession(sessionId: string, createdAt = new Date().toISOString()): SessionCostState {
  return {
    sessionId,
    runIds: [],
    requestCount: 0,
    successfulRequests: 0,
    failedRequests: 0,
    inputTokens: 0,
    outputTokens: 0,
    cacheReadInputTokens: 0,
    cacheCreationInputTokens: 0,
    serverToolUseTokens: 0,
    inputCostUsd: 0,
    outputCostUsd: 0,
    cacheReadCostUsd: 0,
    cacheWriteCostUsd: 0,
    totalCostUsd: 0,
    firstSeenAt: createdAt,
    updatedAt: createdAt,
  };
}

function normalizeUsage(value: ProviderUsage): ProviderUsage {
  return {
    inputTokens: nonnegative(value.inputTokens),
    outputTokens: nonnegative(value.outputTokens),
    cacheReadInputTokens: nonnegative(value.cacheReadInputTokens),
    cacheCreationInputTokens: nonnegative(value.cacheCreationInputTokens),
    serverToolUseTokens: nonnegative(value.serverToolUseTokens),
  };
}

function usageFromJson(value: JsonObject): ProviderUsage {
  return {
    inputTokens: integer(value.input_tokens, 0),
    outputTokens: integer(value.output_tokens, 0),
    cacheReadInputTokens: integer(value.cache_read_input_tokens, 0),
    cacheCreationInputTokens: integer(value.cache_creation_input_tokens, 0),
    serverToolUseTokens: integer(value.server_tool_use_tokens, 0),
  };
}

function usageToJson(value: ProviderUsage): JsonObject {
  return {
    input_tokens: value.inputTokens,
    output_tokens: value.outputTokens,
    cache_read_input_tokens: value.cacheReadInputTokens,
    cache_creation_input_tokens: value.cacheCreationInputTokens,
    server_tool_use_tokens: value.serverToolUseTokens,
  };
}

function sampleToJson(value: UsageSample): JsonObject {
  return {
    sample_id: value.sampleId,
    request_id: value.requestId,
    session_id: value.sessionId,
    run_id: value.runId,
    task_id: value.taskId,
    model: value.model,
    provider: value.provider,
    usage: usageToJson(value.usage),
    input_cost_usd: value.inputCostUsd,
    output_cost_usd: value.outputCostUsd,
    cache_read_cost_usd: value.cacheReadCostUsd,
    cache_write_cost_usd: value.cacheWriteCostUsd,
    total_cost_usd: value.totalCostUsd,
    duration_ms: value.durationMs,
    first_token_ms: value.firstTokenMs,
    success: value.success,
    stop_reason: value.stopReason,
    error_code: value.errorCode,
    created_at: value.createdAt,
  };
}

function sessionToJson(value: SessionCostState): JsonObject {
  return {
    session_id: value.sessionId,
    run_ids: value.runIds,
    request_count: value.requestCount,
    successful_requests: value.successfulRequests,
    failed_requests: value.failedRequests,
    input_tokens: value.inputTokens,
    output_tokens: value.outputTokens,
    cache_read_input_tokens: value.cacheReadInputTokens,
    cache_creation_input_tokens: value.cacheCreationInputTokens,
    server_tool_use_tokens: value.serverToolUseTokens,
    input_cost_usd: value.inputCostUsd,
    output_cost_usd: value.outputCostUsd,
    cache_read_cost_usd: value.cacheReadCostUsd,
    cache_write_cost_usd: value.cacheWriteCostUsd,
    total_cost_usd: value.totalCostUsd,
    first_seen_at: value.firstSeenAt,
    updated_at: value.updatedAt,
  };
}

function promptToJson(value: PromptStateSnapshot): JsonObject {
  return {
    key: value.key,
    session_id: value.sessionId,
    agent_id: value.agentId,
    model: value.model,
    request_id: value.requestId,
    system_hash: value.systemHash,
    tools_hash: value.toolsHash,
    messages_hash: value.messagesHash,
    system_chars: value.systemChars,
    tools_chars: value.toolsChars,
    messages_chars: value.messagesChars,
    tool_hashes: value.toolHashes,
    cache_control_count: value.cacheControlCount,
    compact_generation: value.compactGeneration,
    recorded_at: value.recordedAt,
    expires_at: value.expiresAt,
  };
}

function breakToJson(value: PromptCacheBreak): JsonObject {
  return {
    break_id: value.breakId,
    key: value.key,
    previous_request_id: value.previousRequestId,
    current_request_id: value.currentRequestId,
    kind: value.kind,
    changed_tools: value.changedTools,
    removed_tools: value.removedTools,
    added_tools: value.addedTools,
    system_char_delta: value.systemCharDelta,
    tools_char_delta: value.toolsCharDelta,
    messages_char_delta: value.messagesCharDelta,
    previous_digest: value.previousDigest,
    current_digest: value.currentDigest,
    compact_generation: value.compactGeneration,
    detected_at: value.detectedAt,
  };
}

function eventToJson(value: TelemetryEvent): JsonObject {
  return {
    event_id: value.eventId,
    sequence: value.sequence,
    level: value.level,
    name: value.name,
    outcome: value.outcome,
    session_id: value.sessionId,
    run_id: value.runId,
    task_id: value.taskId,
    request_id: value.requestId,
    parent_event_id: value.parentEventId,
    duration_ms: value.durationMs,
    summary: value.summary,
    attributes: value.attributes,
    created_at: value.createdAt,
  };
}

function spanToJson(value: TelemetrySpan): JsonObject {
  return {
    span_id: value.spanId,
    name: value.name,
    session_id: value.sessionId,
    run_id: value.runId,
    task_id: value.taskId,
    request_id: value.requestId,
    parent_span_id: value.parentSpanId,
    started_at_ms: value.startedAt,
    first_token_at_ms: value.firstTokenAt,
    attributes: value.attributes,
    closed: value.closed,
  };
}

function trackingKey(sessionId: string, agentId: string | null): string {
  return `${required(sessionId, "session id")}:${agentId ?? "main"}`;
}

function provider(value: string): ModelDescriptor["provider"] {
  if (value === "bedrock" || value === "vertex" || value === "compatible" || value === "local") return value;
  return "anthropic";
}

function level(value: string): TelemetryLevel {
  if (value === "debug" || value === "warn" || value === "error") return value;
  return "info";
}

function outcome(value: string): TelemetryOutcome {
  if (value === "started" || value === "failed" || value === "cancelled") return value;
  return "succeeded";
}

function tokenBoundaries(): number[] {
  return [1, 8, 32, 128, 512, 1_024, 2_048, 4_096, 8_192, 16_384, 32_768, 65_536, 131_072, 262_144];
}

function costBoundaries(): number[] {
  return [1, 10, 100, 1_000, 10_000, 100_000, 500_000, 1_000_000, 5_000_000, 10_000_000];
}

function costForTokens(tokens: number, pricePerMillion: number): number {
  return roundCurrency((nonnegative(tokens) / 1_000_000) * Math.max(0, pricePerMillion));
}

function formatCost(value: number): string {
  if (value === 0) return "0.00";
  if (value < 0.0001) return value.toFixed(6);
  if (value < 0.01) return value.toFixed(4);
  return value.toFixed(2);
}

function formatCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}m`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

function roundCurrency(value: number): number {
  return Math.round((value + Number.EPSILON) * 100_000_000) / 100_000_000;
}

function jsonChars(value: JsonValue): number {
  return canonicalJson(value).length;
}

function required(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${name} is required`);
  return normalized;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function normalizeTimestamp(value?: string): string {
  if (!value) return new Date().toISOString();
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error(`invalid timestamp: ${value}`);
  return new Date(timestamp).toISOString();
}

function integer(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : fallback;
}

function number(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function nonnegative(value: number): number {
  return Number.isFinite(value) ? Math.max(0, value) : 0;
}

function boundedInteger(
  value: number | undefined,
  minimum: number,
  maximum: number,
  fallback: number,
): number {
  if (value === undefined || !Number.isFinite(value)) return fallback;
  return Math.max(minimum, Math.min(maximum, Math.floor(value)));
}

function trimHead<T>(values: T[], maximum: number): void {
  const remove = values.length - maximum;
  if (remove > 0) values.splice(0, remove);
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
