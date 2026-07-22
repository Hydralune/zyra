import { randomUUID } from "node:crypto";

import type { JsonObject } from "../contracts.ts";
import type { SupplementaryObservationEmitter } from "./execution-supervisor.ts";
import type { WatchdogObservation, WatchdogRefs } from "./runtime.ts";

export type McpConnectionPhase =
  | "connected"
  | "disconnected"
  | "backoff"
  | "reconnecting"
  | "breaker_open"
  | "stopped";

export interface McpConnectionInput {
  refs: WatchdogRefs;
  generation: number;
  maxReconnectAttempts?: number;
  reconnectWindowMs?: number;
  reconnectBurstLimit?: number;
  baseBackoffMs?: number;
  maxBackoffMs?: number;
  metadata?: JsonObject;
}

export interface McpRequestInput {
  requestId: string;
  method: string;
  timeoutMs: number;
  idempotencyKey: string;
  sideEffecting: boolean;
  metadata?: JsonObject;
}

export interface McpRequestReceipt {
  requestId: string;
  generation: number;
  phase: "pending" | "completed" | "failed" | "timed_out" | "cancelled";
  accepted: boolean;
  duplicate: boolean;
  responseDigest: string;
  revision: number;
}

interface McpRequestRecord {
  requestId: string;
  method: string;
  generation: number;
  timeoutMs: number;
  startedAtMs: number;
  deadlineAtMs: number;
  idempotencyKey: string;
  sideEffecting: boolean;
  phase: McpRequestReceipt["phase"];
  responseDigest: string;
  revision: number;
  abortController: AbortController;
  timeout: NodeJS.Timeout | undefined;
  metadata: JsonObject;
}

interface McpConnectionRecord {
  refs: WatchdogRefs;
  generation: number;
  phase: McpConnectionPhase;
  maxReconnectAttempts: number;
  reconnectWindowMs: number;
  reconnectBurstLimit: number;
  baseBackoffMs: number;
  maxBackoffMs: number;
  reconnectAttempts: number;
  reconnectHistory: number[];
  nextReconnectAtMs: number | null;
  connectedAtMs: number;
  disconnectedAtMs: number | null;
  lastReasonCode: string;
  observationId: string;
  revision: number;
  requests: Map<string, McpRequestRecord>;
  requestKeys: Map<string, string>;
  reconnectPromise: Promise<boolean> | null;
  metadata: JsonObject;
}

function runtimeId(prefix: string): string {
  return prefix + "_" + randomUUID().replaceAll("-", "");
}

function nowIso(): string {
  return new Date().toISOString();
}

function requireIdentity(name: string, value: string): string {
  const selected = value.trim();
  if (!selected) throw new Error(name + " must not be empty");
  return selected;
}

/**
 * OMP-derived MCP timeout, transport-close and reconnect-storm supervisor.
 *
 * The source manager uses a per-server single-flight reconnect and a sliding
 * crash window. Zyra retains that TypeScript control flow while emitting only
 * typed observations; durable signals and recovery planning remain Python and
 * M1-07C responsibilities respectively.
 */
export class McpTransportSupervisor {
  readonly observerId = "ts-process-transport";
  readonly sourceRevision = "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca";
  readonly migrationMode = "cropped_same_language";

  #connections = new Map<string, McpConnectionRecord>();
  #emit: SupplementaryObservationEmitter;
  #now: () => number;
  #connectCount = 0;
  #disconnectCount = 0;
  #requestCount = 0;
  #requestTimeoutCount = 0;
  #duplicateRequestCount = 0;
  #reconnectCount = 0;
  #reconnectFailureCount = 0;
  #breakerOpenCount = 0;

  constructor(emit: SupplementaryObservationEmitter, now: () => number = () => Date.now()) {
    this.#emit = emit;
    this.#now = now;
  }

  connect(input: McpConnectionInput): void {
    const serverId = requireIdentity("mcpServerId", input.refs.mcpServerId);
    requireIdentity("runId", input.refs.runId);
    requireIdentity("taskId", input.refs.taskId);
    if (!Number.isSafeInteger(input.generation) || input.generation < 0) {
      throw new Error("MCP generation must be non-negative");
    }
    const current = this.#connections.get(serverId);
    if (current !== undefined) {
      if (input.generation < current.generation) throw new Error("stale MCP generation");
      if (input.generation === current.generation && current.phase !== "stopped") {
        current.phase = "connected";
        current.disconnectedAtMs = null;
        current.nextReconnectAtMs = null;
        current.lastReasonCode = "";
        current.revision += 1;
        return;
      }
      this.#cancelRequests(current, "connection_generation_replaced");
      current.phase = "stopped";
      current.revision += 1;
    }
    const now = this.#now();
    const record: McpConnectionRecord = {
      refs: structuredClone(input.refs),
      generation: input.generation,
      phase: "connected",
      maxReconnectAttempts: this.#positive(input.maxReconnectAttempts ?? 4, "maxReconnectAttempts"),
      reconnectWindowMs: this.#positive(input.reconnectWindowMs ?? 10_000, "reconnectWindowMs"),
      reconnectBurstLimit: this.#positive(input.reconnectBurstLimit ?? 5, "reconnectBurstLimit"),
      baseBackoffMs: this.#positive(input.baseBackoffMs ?? 250, "baseBackoffMs"),
      maxBackoffMs: this.#positive(input.maxBackoffMs ?? 30_000, "maxBackoffMs"),
      reconnectAttempts: 0,
      reconnectHistory: [],
      nextReconnectAtMs: null,
      connectedAtMs: now,
      disconnectedAtMs: null,
      lastReasonCode: "",
      observationId: "",
      revision: 1,
      requests: new Map(),
      requestKeys: new Map(),
      reconnectPromise: null,
      metadata: structuredClone(input.metadata ?? {}),
    };
    if (record.maxBackoffMs < record.baseBackoffMs) throw new Error("MCP maxBackoffMs is below baseBackoffMs");
    this.#connections.set(serverId, record);
    this.#connectCount += 1;
  }

  beginRequest(
    serverId: string,
    generation: number,
    input: McpRequestInput,
  ): { signal: AbortSignal; receipt: McpRequestReceipt } {
    const record = this.#requireConnected(serverId, generation);
    requireIdentity("requestId", input.requestId);
    requireIdentity("method", input.method);
    const idempotencyKey = requireIdentity("idempotencyKey", input.idempotencyKey);
    const priorId = record.requestKeys.get(idempotencyKey);
    if (priorId !== undefined) {
      const prior = record.requests.get(priorId);
      if (prior === undefined) throw new Error("MCP request key index is corrupt");
      this.#duplicateRequestCount += 1;
      return { signal: prior.abortController.signal, receipt: this.#requestReceipt(prior, true) };
    }
    if (record.requests.has(input.requestId)) throw new Error("MCP requestId already exists");
    const timeoutMs = this.#positive(input.timeoutMs, "timeoutMs");
    const now = this.#now();
    const request: McpRequestRecord = {
      requestId: input.requestId,
      method: input.method,
      generation,
      timeoutMs,
      startedAtMs: now,
      deadlineAtMs: now + timeoutMs,
      idempotencyKey,
      sideEffecting: input.sideEffecting,
      phase: "pending",
      responseDigest: "",
      revision: 1,
      abortController: new AbortController(),
      timeout: undefined,
      metadata: structuredClone(input.metadata ?? {}),
    };
    request.timeout = setTimeout(() => {
      void this.timeoutRequest(serverId, generation, input.requestId);
    }, timeoutMs);
    record.requests.set(input.requestId, request);
    record.requestKeys.set(idempotencyKey, input.requestId);
    record.revision += 1;
    this.#requestCount += 1;
    return { signal: request.abortController.signal, receipt: this.#requestReceipt(request, false) };
  }

  settleRequest(
    serverId: string,
    generation: number,
    requestId: string,
    responseDigest: string,
  ): McpRequestReceipt {
    const connection = this.#require(serverId, generation);
    const request = this.#requireRequest(connection, requestId);
    requireIdentity("responseDigest", responseDigest);
    if (request.phase === "completed") {
      if (request.responseDigest !== responseDigest) {
        throw new Error("duplicate MCP response has a different digest");
      }
      return this.#requestReceipt(request, true);
    }
    if (request.phase !== "pending") return this.#requestReceipt(request, true);
    request.phase = "completed";
    request.responseDigest = responseDigest;
    request.revision += 1;
    this.#clearRequestTimer(request);
    connection.revision += 1;
    return this.#requestReceipt(request, false);
  }

  failRequest(
    serverId: string,
    generation: number,
    requestId: string,
    errorCode: string,
  ): McpRequestReceipt {
    const connection = this.#require(serverId, generation);
    const request = this.#requireRequest(connection, requestId);
    if (request.phase !== "pending") return this.#requestReceipt(request, true);
    request.phase = "failed";
    request.metadata = { ...request.metadata, error_code: requireIdentity("errorCode", errorCode) };
    request.revision += 1;
    this.#clearRequestTimer(request);
    connection.revision += 1;
    return this.#requestReceipt(request, false);
  }

  async timeoutRequest(serverId: string, generation: number, requestId: string): Promise<boolean> {
    const connection = this.#connections.get(serverId);
    if (connection === undefined || connection.generation !== generation) return false;
    const request = connection.requests.get(requestId);
    if (request === undefined || request.phase !== "pending") return false;
    request.phase = "timed_out";
    request.revision += 1;
    this.#clearRequestTimer(request);
    if (!request.abortController.signal.aborted) {
      request.abortController.abort(new Error("MCP request timeout"));
    }
    connection.revision += 1;
    this.#requestTimeoutCount += 1;
    await this.#emit(this.observerId, this.#observation(connection, {
      code: "request_timeout",
      reasonCode: "request_deadline_elapsed",
      request,
      terminal: true,
    }));
    return true;
  }

  async disconnected(
    serverId: string,
    generation: number,
    reasonCode: string,
  ): Promise<void> {
    const record = this.#require(serverId, generation);
    if (record.phase === "stopped") return;
    const now = this.#now();
    record.phase = "disconnected";
    record.disconnectedAtMs = now;
    record.lastReasonCode = requireIdentity("reasonCode", reasonCode);
    record.observationId = runtimeId("ts_mcp_disconnect_observation");
    record.revision += 1;
    this.#cancelRequests(record, "transport_closed");
    this.#disconnectCount += 1;
    await this.#emit(this.observerId, this.#observation(record, {
      code: "transport_closed",
      reasonCode,
      request: null,
      terminal: true,
    }));
  }

  async reconnect(
    serverId: string,
    generation: number,
    operation: () => Promise<boolean>,
  ): Promise<boolean> {
    const record = this.#require(serverId, generation);
    if (record.phase === "stopped") throw new Error("MCP transport is stopped");
    if (record.phase === "breaker_open") return false;
    if (record.reconnectPromise !== null) return await record.reconnectPromise;
    const now = this.#now();
    if (record.nextReconnectAtMs !== null && now < record.nextReconnectAtMs) return false;
    this.#pruneReconnectHistory(record, now);
    if (record.reconnectHistory.length >= record.reconnectBurstLimit) {
      await this.#openBreaker(record, "reconnect_crash_storm");
      return false;
    }
    record.phase = "reconnecting";
    record.reconnectAttempts += 1;
    record.reconnectHistory.push(now);
    record.revision += 1;
    const promise = this.#runReconnect(record, operation);
    record.reconnectPromise = promise;
    try {
      return await promise;
    } finally {
      if (record.reconnectPromise === promise) record.reconnectPromise = null;
    }
  }

  manualReset(serverId: string, generation: number): void {
    const record = this.#require(serverId, generation);
    record.reconnectHistory = [];
    record.reconnectAttempts = 0;
    record.nextReconnectAtMs = null;
    record.phase = "disconnected";
    record.revision += 1;
  }

  stop(serverId: string, generation: number): void {
    const record = this.#require(serverId, generation);
    this.#cancelRequests(record, "intentional_stop");
    record.phase = "stopped";
    record.nextReconnectAtMs = null;
    record.reconnectPromise = null;
    record.revision += 1;
  }

  dueReconnects(atMs: number = this.#now()): string[] {
    return [...this.#connections.entries()]
      .filter(([, value]) => value.phase === "backoff" && value.nextReconnectAtMs !== null && value.nextReconnectAtMs <= atMs)
      .map(([serverId]) => serverId)
      .sort();
  }

  snapshot(): JsonObject {
    const connections: JsonObject = {};
    for (const [serverId, record] of [...this.#connections.entries()].sort(([left], [right]) => left.localeCompare(right))) {
      const requestCounts: Record<string, number> = {};
      for (const request of record.requests.values()) {
        requestCounts[request.phase] = (requestCounts[request.phase] ?? 0) + 1;
      }
      connections[serverId] = {
        run_id: record.refs.runId,
        task_id: record.refs.taskId,
        generation: record.generation,
        phase: record.phase,
        reconnect_attempts: record.reconnectAttempts,
        reconnect_history: [...record.reconnectHistory],
        reconnect_burst_limit: record.reconnectBurstLimit,
        reconnect_window_ms: record.reconnectWindowMs,
        next_reconnect_at_ms: record.nextReconnectAtMs,
        disconnected_at_ms: record.disconnectedAtMs,
        last_reason_code: record.lastReasonCode,
        observation_id: record.observationId,
        request_counts: requestCounts,
        revision: record.revision,
      };
    }
    return {
      schema: "zyra.typescript-mcp-transport-supervisor/v1",
      source_repo: "oh-my-pi",
      source_revision: this.sourceRevision,
      migration_mode: this.migrationMode,
      connections,
      counts: {
        connects: this.#connectCount,
        disconnects: this.#disconnectCount,
        requests: this.#requestCount,
        request_timeouts: this.#requestTimeoutCount,
        duplicate_requests: this.#duplicateRequestCount,
        reconnects: this.#reconnectCount,
        reconnect_failures: this.#reconnectFailureCount,
        breaker_opens: this.#breakerOpenCount,
      },
      reconnect_single_flight: true,
      crash_storm_breaker: true,
      request_timeout_aborts_pending_effect: true,
      injection_fallback_when_disabled: false,
      canonical_signal_owner: "python.FaultStateStore",
      recovery_plan_owner: "M1-S07C",
    };
  }

  async #runReconnect(record: McpConnectionRecord, operation: () => Promise<boolean>): Promise<boolean> {
    let ok = false;
    try {
      ok = await operation();
    } catch {
      ok = false;
    }
    if (record.phase === "stopped") return false;
    if (ok) {
      record.phase = "connected";
      record.connectedAtMs = this.#now();
      record.disconnectedAtMs = null;
      record.nextReconnectAtMs = null;
      record.reconnectAttempts = 0;
      record.lastReasonCode = "";
      record.revision += 1;
      this.#reconnectCount += 1;
      return true;
    }
    this.#reconnectFailureCount += 1;
    if (record.reconnectAttempts >= record.maxReconnectAttempts) {
      await this.#openBreaker(record, "reconnect_attempt_budget_exhausted");
      return false;
    }
    const delay = Math.min(
      record.baseBackoffMs * (2 ** Math.max(0, record.reconnectAttempts - 1)),
      record.maxBackoffMs,
    );
    record.phase = "backoff";
    record.nextReconnectAtMs = this.#now() + delay;
    record.lastReasonCode = "reconnect_failed";
    record.revision += 1;
    return false;
  }

  async #openBreaker(record: McpConnectionRecord, reasonCode: string): Promise<void> {
    if (record.phase === "breaker_open") return;
    record.phase = "breaker_open";
    record.nextReconnectAtMs = null;
    record.lastReasonCode = reasonCode;
    record.observationId = runtimeId("ts_mcp_breaker_observation");
    record.revision += 1;
    this.#breakerOpenCount += 1;
    await this.#emit(this.observerId, this.#observation(record, {
      code: "reconnect_breaker_open",
      reasonCode,
      request: null,
      terminal: true,
    }));
  }

  #observation(
    record: McpConnectionRecord,
    input: {
      code: "transport_closed" | "request_timeout" | "reconnect_breaker_open";
      reasonCode: string;
      request: McpRequestRecord | null;
      terminal: boolean;
    },
  ): WatchdogObservation {
    const observationId = record.observationId || runtimeId("ts_mcp_observation");
    const now = this.#now();
    return {
      observationId,
      observerId: this.observerId,
      category: "mcp",
      code: input.code,
      status: record.phase,
      summary: "MCP transport crossed a typed timeout, disconnect, or breaker boundary.",
      errorType: input.code === "request_timeout" ? "McpRequestTimeout" : "McpTransportClosed",
      retryable: record.phase !== "breaker_open",
      terminal: input.terminal,
      elapsedMs: input.request ? Math.max(0, now - input.request.startedAtMs) : null,
      deadlineMs: input.request?.timeoutMs ?? null,
      statusCode: null,
      refs: {
        ...structuredClone(record.refs),
        observationId,
        sourceStateRevision: record.revision,
      },
      details: {
        ...structuredClone(record.metadata),
        generation: record.generation,
        reason_code: input.reasonCode,
        reconnect_attempts: record.reconnectAttempts,
        reconnect_burst_count: record.reconnectHistory.length,
        reconnect_burst_limit: record.reconnectBurstLimit,
        next_reconnect_at_ms: record.nextReconnectAtMs,
        request_id: input.request?.requestId ?? "",
        request_method: input.request?.method ?? "",
        request_side_effecting: input.request?.sideEffecting ?? false,
        request_idempotency_key: input.request?.idempotencyKey ?? "",
        pending_request_count: [...record.requests.values()].filter((value) => value.phase === "pending").length,
        reconnect_plan_selected: false,
      },
      observedAt: nowIso(),
    };
  }

  #cancelRequests(record: McpConnectionRecord, reasonCode: string): void {
    for (const request of record.requests.values()) {
      if (request.phase !== "pending") continue;
      request.phase = "cancelled";
      request.metadata = { ...request.metadata, cancellation_reason_code: reasonCode };
      request.revision += 1;
      this.#clearRequestTimer(request);
      if (!request.abortController.signal.aborted) {
        request.abortController.abort(new Error("MCP transport cancelled pending request"));
      }
    }
  }

  #pruneReconnectHistory(record: McpConnectionRecord, now: number): void {
    const cutoff = now - record.reconnectWindowMs;
    record.reconnectHistory = record.reconnectHistory.filter((value) => value >= cutoff);
  }

  #requestReceipt(request: McpRequestRecord, duplicate: boolean): McpRequestReceipt {
    return {
      requestId: request.requestId,
      generation: request.generation,
      phase: request.phase,
      accepted: request.phase === "pending" || request.phase === "completed",
      duplicate,
      responseDigest: request.responseDigest,
      revision: request.revision,
    };
  }

  #require(serverId: string, generation: number): McpConnectionRecord {
    const record = this.#connections.get(serverId);
    if (record === undefined) throw new Error("MCP server is not bound: " + serverId);
    if (record.generation !== generation) throw new Error("stale MCP transport generation");
    return record;
  }

  #requireConnected(serverId: string, generation: number): McpConnectionRecord {
    const record = this.#require(serverId, generation);
    if (record.phase !== "connected") throw new Error("MCP server is not connected: " + record.phase);
    return record;
  }

  #requireRequest(record: McpConnectionRecord, requestId: string): McpRequestRecord {
    const request = record.requests.get(requestId);
    if (request === undefined) throw new Error("MCP request does not exist: " + requestId);
    return request;
  }

  #clearRequestTimer(request: McpRequestRecord): void {
    if (request.timeout !== undefined) {
      clearTimeout(request.timeout);
      request.timeout = undefined;
    }
  }

  #positive(value: number, name: string): number {
    if (!Number.isSafeInteger(value) || value < 1) throw new Error(name + " must be a positive integer");
    return value;
  }
}

export function mcpTransportSupervisorContract(): JsonObject {
  return {
    schema: "zyra.typescript-mcp-transport-supervisor-contract/v1",
    source_repo: "oh-my-pi",
    source_revision: "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
    migration_mode: "cropped_same_language",
    source_mechanisms: [
      "MCP request timeout and abort cleanup",
      "transport onClose reconnect",
      "single-flight reconnect",
      "sliding-window crash-storm breaker",
    ],
    request_deadline_active: true,
    pending_request_abort_on_disconnect: true,
    reconnect_single_flight: true,
    crash_storm_breaker: true,
    manual_reset_available: true,
    injection_fallback_when_disabled: false,
    recovery_plan_owner: "M1-S07C",
    canonical_signal_owner: "python.FaultStateStore",
  };
}
