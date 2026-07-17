import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { once } from "node:events";

import type { JsonObject, JsonRpcMessage, JsonRpcResponse } from "../contracts.ts";
import type { McpStdioTransportConfig } from "../config/config-store.ts";
import {
  canonicalJson,
  cloneJson,
  deterministicMcpId,
  hashChain,
  monotonicNow,
  redactMcpSecrets,
  sha256,
} from "../core/canonical.ts";
import { McpProtocolCodec } from "../core/protocol.ts";
import {
  McpRuntimeError,
  normalizeFailure,
  type McpFailureRecord,
} from "../core/failure.ts";
import type {
  McpCompletedTransportRequest,
  McpIdempotencyReceipt,
  McpPendingTransportRequest,
  McpProcessFactory,
  McpTransportAdapter,
  McpTransportEvent,
  McpTransportHealth,
  McpTransportRequest,
  McpTransportResponse,
  McpTransportSnapshot,
} from "./contracts.ts";

interface PendingPromise {
  request: McpTransportRequest;
  record: McpPendingTransportRequest;
  started: number;
  resolve(value: McpTransportResponse): void;
  reject(error: Error): void;
  timer: NodeJS.Timeout;
  abortCleanup: (() => void) | null;
}

export interface StdioMcpTransportOptions {
  serverId: string;
  config: McpStdioTransportConfig;
  connectTimeoutMs?: number;
  requestTimeoutMs?: number;
  shutdownTimeoutMs?: number;
  maximumLineBytes?: number;
  maximumCompletedRequests?: number;
  maximumEvents?: number;
  environment?: NodeJS.ProcessEnv;
  credentialEnvironment?: Record<string, string>;
  processFactory?: McpProcessFactory;
  codec?: McpProtocolCodec;
  now?: () => Date;
  snapshot?: McpTransportSnapshot | null;
}

export class StdioMcpTransport implements McpTransportAdapter {
  readonly serverId: string;
  readonly transportId: string;
  private readonly config: McpStdioTransportConfig;
  private readonly connectTimeoutMs: number;
  private readonly requestTimeoutMs: number;
  private readonly shutdownTimeoutMs: number;
  private readonly maximumLineBytes: number;
  private readonly maximumCompletedRequests: number;
  private readonly maximumEvents: number;
  private readonly environment: NodeJS.ProcessEnv;
  private readonly credentialEnvironment: Record<string, string>;
  private readonly processFactory: McpProcessFactory;
  private readonly codec: McpProtocolCodec;
  private readonly now: () => Date;
  private readonly messageListeners = new Set<(message: JsonRpcMessage) => void | Promise<void>>();
  private readonly eventListeners = new Set<(event: McpTransportEvent) => void | Promise<void>>();
  private readonly pending = new Map<string, PendingPromise>();
  private readonly pendingByWireId = new Map<string, string>();
  private readonly completed = new Map<string, McpCompletedTransportRequest>();
  private readonly receipts = new Map<string, McpIdempotencyReceipt>();
  private readonly events: McpTransportEvent[] = [];
  private process: ChildProcessWithoutNullStreams | null = null;
  private stdoutBuffer = "";
  private stderrBuffer = "";
  private startPromise: Promise<McpTransportHealth> | null = null;
  private closePromise: Promise<void> | null = null;
  private phase: McpTransportHealth["phase"] = "idle";
  private connectionEpoch = 0;
  private sequence = 0;
  private startedAt: string | null = null;
  private lastActivityAt: string | null = null;
  private lastMessageAt: string | null = null;
  private completedCount = 0;
  private failedCount = 0;
  private reconnectCount = 0;
  private bytesSent = 0;
  private bytesReceived = 0;
  private lastFailure: McpFailureRecord | null = null;

  constructor(options: StdioMcpTransportOptions) {
    this.serverId = options.serverId;
    this.config = cloneJson(options.config);
    this.transportId = deterministicMcpId("mcp-stdio", {
      server_id: this.serverId,
      command: this.config.command,
      arguments: this.config.arguments,
      cwd: this.config.cwd,
    });
    this.connectTimeoutMs = options.connectTimeoutMs ?? 15_000;
    this.requestTimeoutMs = options.requestTimeoutMs ?? 60_000;
    this.shutdownTimeoutMs = options.shutdownTimeoutMs ?? 5_000;
    this.maximumLineBytes = options.maximumLineBytes ?? 16 * 1024 * 1024;
    this.maximumCompletedRequests = options.maximumCompletedRequests ?? 10_000;
    this.maximumEvents = options.maximumEvents ?? 20_000;
    this.environment = { ...(options.environment ?? process.env) };
    this.credentialEnvironment = { ...(options.credentialEnvironment ?? {}) };
    this.processFactory = options.processFactory ?? defaultProcessFactory;
    this.codec = options.codec ?? new McpProtocolCodec({ maximumMessageBytes: this.maximumLineBytes });
    this.now = options.now ?? (() => new Date());
    validateOptions(this);
    if (options.snapshot) this.restoreMetadata(options.snapshot);
  }

  async start(signal?: AbortSignal): Promise<McpTransportHealth> {
    if (this.phase === "ready" && this.process && this.process.exitCode === null) return this.health();
    if (this.phase === "closing") throw stdioError(this.serverId, "transport_closing", "cannot start a closing stdio transport");
    if (this.startPromise) return this.startPromise;
    this.startPromise = this.startProcess(signal);
    try {
      return await this.startPromise;
    } finally {
      this.startPromise = null;
    }
  }

  async request(request: McpTransportRequest): Promise<McpTransportResponse> {
    if (request.signal?.aborted) throw abortError(this.serverId, request.requestId);
    const requestDigest = sha256(request.message);
    if (request.idempotencyKey) {
      const receipt = this.receipts.get(request.idempotencyKey);
      if (receipt) {
        if (receipt.requestDigest !== requestDigest) {
          throw stdioError(
            this.serverId,
            "idempotency_key_reused",
            `idempotency key ${request.idempotencyKey} is bound to a different request`,
            request.requestId,
          );
        }
        return {
          requestId: request.requestId,
          message: cloneJson(receipt.response),
          transportId: this.transportId,
          connectionEpoch: receipt.connectionEpoch,
          elapsedMs: 0,
          replayed: true,
          headers: {},
          metadata: { receipt_committed_at: receipt.committedAt },
        };
      }
      const inFlight = [...this.pending.values()].find((pending) => pending.request.idempotencyKey === request.idempotencyKey);
      if (inFlight) {
        if (sha256(inFlight.request.message) !== requestDigest) {
          throw stdioError(this.serverId, "idempotency_key_inflight_conflict", "idempotency key is already in flight with different payload", request.requestId);
        }
        return new Promise<McpTransportResponse>((resolve, reject) => {
          const wait = (): void => {
            const receipt = this.receipts.get(request.idempotencyKey!);
            if (receipt) {
              resolve({
                requestId: request.requestId,
                message: cloneJson(receipt.response),
                transportId: this.transportId,
                connectionEpoch: receipt.connectionEpoch,
                elapsedMs: 0,
                replayed: true,
                headers: {},
                metadata: { coalesced_request_id: inFlight.request.requestId },
              });
              return;
            }
            if (!this.pending.has(inFlight.request.requestId)) {
              reject(stdioError(this.serverId, "coalesced_request_failed", "coalesced stdio request did not commit a receipt", request.requestId));
              return;
            }
            setTimeout(wait, 1);
          };
          wait();
        });
      }
    }
    if (this.pending.has(request.requestId) || this.completed.has(request.requestId)) {
      throw stdioError(this.serverId, "duplicate_request_id", `duplicate stdio request id ${request.requestId}`, request.requestId);
    }
    await this.start(request.signal);
    const processValue = this.process;
    if (!processValue || processValue.exitCode !== null || this.phase !== "ready") {
      throw stdioError(this.serverId, "transport_not_ready", "stdio transport is not ready", request.requestId);
    }
    const wireId = wireRequestId(request.message);
    if (wireId !== null && this.pendingByWireId.has(wireId)) {
      throw stdioError(this.serverId, "duplicate_wire_request_id", `JSON-RPC id ${wireId} is already in flight`, request.requestId);
    }
    const timeoutMs = request.timeoutMs > 0 ? request.timeoutMs : this.requestTimeoutMs;
    const preparedAt = this.timestamp();
    const deadlineAt = new Date(Date.parse(preparedAt) + timeoutMs).toISOString();
    const record: McpPendingTransportRequest = {
      requestId: request.requestId,
      method: request.method,
      messageDigest: requestDigest,
      idempotent: request.idempotent,
      idempotencyKey: request.idempotencyKey,
      connectionEpoch: this.connectionEpoch,
      preparedAt,
      sentAt: null,
      deadlineAt,
      attempt: 1,
      metadata: cloneJson(request.metadata),
    };
    const response = new Promise<McpTransportResponse>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.failPending(request.requestId, stdioError(
          this.serverId,
          "request_timeout",
          `stdio MCP request ${request.method} timed out after ${timeoutMs}ms`,
          request.requestId,
          true,
        ));
      }, timeoutMs);
      let abortCleanup: (() => void) | null = null;
      if (request.signal) {
        const onAbort = (): void => this.failPending(request.requestId, abortError(this.serverId, request.requestId));
        request.signal.addEventListener("abort", onAbort, { once: true });
        abortCleanup = () => request.signal?.removeEventListener("abort", onAbort);
      }
      this.pending.set(request.requestId, {
        request,
        record,
        started: performance.now(),
        resolve,
        reject,
        timer,
        abortCleanup,
      });
      if (wireId !== null) this.pendingByWireId.set(wireId, request.requestId);
    });
    try {
      const encoded = `${this.codec.encode(request.message)}\n`;
      const sentAt = this.timestamp();
      record.sentAt = sentAt;
      await writeStream(processValue.stdin, encoded, request.signal);
      this.bytesSent += Buffer.byteLength(encoded, "utf8");
      this.lastActivityAt = sentAt;
      this.emitEvent("message_sent", request.requestId, requestDigest, {
        method: request.method,
        idempotent: request.idempotent,
        idempotency_key_present: request.idempotencyKey !== null,
        bytes: Buffer.byteLength(encoded, "utf8"),
      });
    } catch (error) {
      this.failPending(request.requestId, error instanceof Error ? error : new Error(String(error)));
    }
    return response;
  }

  async notify(message: JsonRpcMessage, signal?: AbortSignal): Promise<void> {
    if (signal?.aborted) throw abortError(this.serverId, "notification");
    await this.start(signal);
    if (!this.process || this.process.exitCode !== null) throw stdioError(this.serverId, "transport_not_ready", "stdio transport is not ready");
    const encoded = `${this.codec.encode(message)}\n`;
    await writeStream(this.process.stdin, encoded, signal);
    const messageDigest = sha256(message);
    this.bytesSent += Buffer.byteLength(encoded, "utf8");
    this.lastActivityAt = this.timestamp();
    this.emitEvent("message_sent", null, messageDigest, {
      notification: true,
      bytes: Buffer.byteLength(encoded, "utf8"),
    });
  }

  async close(reason = "requested"): Promise<void> {
    if (this.phase === "closed" || this.phase === "idle") {
      this.phase = "closed";
      return;
    }
    if (this.closePromise) return this.closePromise;
    this.closePromise = this.closeProcess(reason);
    try {
      await this.closePromise;
    } finally {
      this.closePromise = null;
    }
  }

  health(): McpTransportHealth {
    return {
      transportId: this.transportId,
      serverId: this.serverId,
      phase: this.phase,
      connectionEpoch: this.connectionEpoch,
      connected: this.phase === "ready" && this.process !== null && this.process.exitCode === null,
      startedAt: this.startedAt,
      lastActivityAt: this.lastActivityAt,
      lastMessageAt: this.lastMessageAt,
      pendingRequests: this.pending.size,
      completedRequests: this.completedCount,
      failedRequests: this.failedCount,
      reconnectCount: this.reconnectCount,
      bytesSent: this.bytesSent,
      bytesReceived: this.bytesReceived,
      processId: this.process?.pid ?? null,
      endpoint: null,
      lastFailure: this.lastFailure ? cloneJson(this.lastFailure) : null,
    };
  }

  snapshot(): McpTransportSnapshot {
    return {
      version: "zyra.mcp-transport/v1",
      transportId: this.transportId,
      serverId: this.serverId,
      phase: this.phase,
      connectionEpoch: this.connectionEpoch,
      sequence: this.sequence,
      health: this.health(),
      events: this.events.map(cloneJson),
      pending: [...this.pending.values()].map((value) => cloneJson(value.record)),
      completed: [...this.completed.values()].map(cloneJson),
      idempotencyReceipts: [...this.receipts.values()].map(cloneJson),
      metadata: {
        framing: "newline-delimited-json-rpc",
        stderr_preview: this.stderrBuffer,
        command_digest: sha256({ command: this.config.command, arguments: this.config.arguments }),
        live_process_restored: false,
      },
    };
  }

  onMessage(listener: (message: JsonRpcMessage) => void | Promise<void>): () => void {
    this.messageListeners.add(listener);
    return () => this.messageListeners.delete(listener);
  }

  onEvent(listener: (event: McpTransportEvent) => void | Promise<void>): () => void {
    this.eventListeners.add(listener);
    return () => this.eventListeners.delete(listener);
  }

  private async startProcess(signal?: AbortSignal): Promise<McpTransportHealth> {
    if (signal?.aborted) throw abortError(this.serverId, "connect");
    const reconnecting = this.connectionEpoch > 0;
    this.phase = reconnecting ? "reconnecting" : "connecting";
    this.connectionEpoch += 1;
    if (reconnecting) this.reconnectCount += 1;
    this.stdoutBuffer = "";
    this.stderrBuffer = "";
    this.emitEvent("starting", null, null, {
      command: this.config.command,
      argument_count: this.config.arguments.length,
      cwd: this.config.cwd,
      reconnecting,
    });
    let processValue: ChildProcessWithoutNullStreams;
    try {
      processValue = this.processFactory({
        command: this.config.command,
        arguments: [...this.config.arguments],
        cwd: this.config.cwd,
        environment: this.buildEnvironment(),
        signal,
      });
    } catch (error) {
      const failure = normalizeFailure(error, {
        failureId: deterministicMcpId("mcp-stdio-spawn", { server_id: this.serverId, epoch: this.connectionEpoch }),
        category: "transport",
        code: "stdio_spawn_failed",
        message: `failed to spawn MCP stdio server ${this.serverId}`,
        serverId: this.serverId,
        operation: "connect",
        retryable: true,
        disposition: "reconnect_then_retry",
      });
      this.lastFailure = failure;
      this.phase = "failed";
      throw new McpRuntimeError(failureToInput(failure), { cause: error });
    }
    this.process = processValue;
    processValue.stdout.setEncoding("utf8");
    processValue.stderr.setEncoding("utf8");
    processValue.stdout.on("data", (chunk: string) => this.consumeStdout(chunk));
    processValue.stderr.on("data", (chunk: string) => this.consumeStderr(chunk));
    processValue.once("error", (error) => this.onProcessError(error));
    processValue.once("exit", (code, exitSignal) => this.onProcessExit(code, exitSignal));
    const timeout = setTimeout(() => {
      if (this.phase === "connecting" || this.phase === "reconnecting") {
        processValue.kill("SIGTERM");
      }
    }, this.connectTimeoutMs);
    try {
      if (processValue.pid === undefined) await once(processValue, "spawn", { signal });
      if (processValue.exitCode !== null) throw stdioError(this.serverId, "stdio_exited_during_start", "stdio server exited before becoming ready");
      this.phase = "ready";
      this.startedAt = this.timestamp();
      this.lastActivityAt = this.startedAt;
      this.emitEvent("started", null, null, { process_id: processValue.pid ?? null });
      return this.health();
    } catch (error) {
      this.phase = "failed";
      const failure = normalizeFailure(error, {
        failureId: deterministicMcpId("mcp-stdio-start", { server_id: this.serverId, epoch: this.connectionEpoch }),
        category: "transport",
        code: "stdio_start_failed",
        message: `MCP stdio server ${this.serverId} did not start`,
        serverId: this.serverId,
        operation: "connect",
        retryable: true,
        disposition: "reconnect_then_retry",
      });
      this.lastFailure = failure;
      throw new McpRuntimeError(failureToInput(failure), { cause: error });
    } finally {
      clearTimeout(timeout);
    }
  }

  private async closeProcess(reason: string): Promise<void> {
    this.phase = "closing";
    this.emitEvent("closing", null, null, { reason });
    const closeError = stdioError(this.serverId, "transport_closed", `stdio transport closed: ${reason}`);
    for (const requestId of [...this.pending.keys()]) this.failPending(requestId, closeError);
    const processValue = this.process;
    this.process = null;
    if (processValue && processValue.exitCode === null) {
      processValue.stdin.end();
      processValue.kill("SIGTERM");
      const timer = setTimeout(() => {
        if (processValue.exitCode === null) processValue.kill("SIGKILL");
      }, this.shutdownTimeoutMs);
      try {
        await Promise.race([
          once(processValue, "exit"),
          new Promise<void>((resolve) => setTimeout(resolve, this.shutdownTimeoutMs + 100)),
        ]);
      } finally {
        clearTimeout(timer);
      }
    }
    this.phase = "closed";
    this.lastActivityAt = this.timestamp();
    this.emitEvent("closed", null, null, { reason });
  }

  private consumeStdout(chunk: string): void {
    this.bytesReceived += Buffer.byteLength(chunk, "utf8");
    this.lastActivityAt = this.timestamp();
    this.stdoutBuffer += chunk;
    if (Buffer.byteLength(this.stdoutBuffer, "utf8") > this.maximumLineBytes) {
      this.onProcessError(stdioError(this.serverId, "stdout_line_overflow", `stdio line exceeds ${this.maximumLineBytes} bytes`));
      return;
    }
    let newline = this.stdoutBuffer.indexOf("\n");
    while (newline >= 0) {
      const raw = this.stdoutBuffer.slice(0, newline).replace(/\r$/, "");
      this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1);
      if (raw.trim()) this.consumeLine(raw);
      newline = this.stdoutBuffer.indexOf("\n");
    }
  }

  private consumeLine(line: string): void {
    let message: JsonRpcMessage;
    try {
      message = this.codec.decodeText(line);
    } catch (error) {
      const failure = normalizeFailure(error, {
        failureId: deterministicMcpId("mcp-stdio-protocol", { server_id: this.serverId, line_digest: sha256(line) }),
        category: "protocol",
        code: "invalid_stdio_json_rpc",
        message: `MCP stdio server ${this.serverId} emitted invalid JSON-RPC`,
        serverId: this.serverId,
        operation: "receive",
        retryable: false,
        disposition: "terminal",
        details: { line_digest: sha256(line), line_bytes: Buffer.byteLength(line, "utf8") },
      });
      this.lastFailure = failure;
      this.emitEvent("transport_error", null, sha256(line), { failure });
      return;
    }
    const digest = sha256(message);
    this.lastMessageAt = this.timestamp();
    this.emitEvent("message_received", null, digest, {
      bytes: Buffer.byteLength(line, "utf8"),
      response: isResponse(message),
    });
    if (isResponse(message)) {
      const wireId = String(message.id);
      const requestId = this.pendingByWireId.get(wireId);
      if (requestId) {
        this.completePending(requestId, message);
        return;
      }
    }
    for (const listener of this.messageListeners) void Promise.resolve(listener(cloneJson(message)));
  }

  private consumeStderr(chunk: string): void {
    const sanitized = String(redactMcpSecrets(chunk));
    this.stderrBuffer += sanitized;
    while (Buffer.byteLength(this.stderrBuffer, "utf8") > this.config.stderrLimitBytes) {
      this.stderrBuffer = this.stderrBuffer.slice(Math.max(1, Math.floor(this.stderrBuffer.length / 8)));
    }
    this.emitEvent("stderr", null, sha256(sanitized), {
      bytes: Buffer.byteLength(chunk, "utf8"),
      preview: sanitized.slice(0, 4_096),
    });
  }

  private completePending(requestId: string, message: JsonRpcMessage): void {
    const pending = this.pending.get(requestId);
    if (!pending) return;
    clearTimeout(pending.timer);
    pending.abortCleanup?.();
    this.pending.delete(requestId);
    const wireId = wireRequestId(pending.request.message);
    if (wireId !== null) this.pendingByWireId.delete(wireId);
    const responseDigest = sha256(message);
    const completedAt = this.timestamp();
    const completed: McpCompletedTransportRequest = {
      requestId,
      method: pending.request.method,
      messageDigest: pending.record.messageDigest,
      responseDigest,
      idempotencyKey: pending.request.idempotencyKey,
      connectionEpoch: pending.record.connectionEpoch,
      attempt: pending.record.attempt,
      status: "committed",
      preparedAt: pending.record.preparedAt,
      sentAt: pending.record.sentAt,
      completedAt,
      failure: null,
      response: cloneJson(message),
      metadata: cloneJson(pending.request.metadata),
    };
    this.completed.set(requestId, completed);
    this.trimCompleted();
    this.completedCount += 1;
    if (pending.request.idempotencyKey) {
      this.receipts.set(pending.request.idempotencyKey, {
        idempotencyKey: pending.request.idempotencyKey,
        requestId,
        requestDigest: pending.record.messageDigest,
        responseDigest,
        response: cloneJson(message),
        connectionEpoch: pending.record.connectionEpoch,
        committedAt: completedAt,
      });
    }
    pending.resolve({
      requestId,
      message: cloneJson(message),
      transportId: this.transportId,
      connectionEpoch: pending.record.connectionEpoch,
      elapsedMs: Math.max(0, performance.now() - pending.started),
      replayed: false,
      headers: {},
      metadata: { completed_at: completedAt },
    });
  }

  private failPending(requestId: string, error: Error): void {
    const pending = this.pending.get(requestId);
    if (!pending) return;
    clearTimeout(pending.timer);
    pending.abortCleanup?.();
    this.pending.delete(requestId);
    const wireId = wireRequestId(pending.request.message);
    if (wireId !== null) this.pendingByWireId.delete(wireId);
    const failure = normalizeFailure(error, {
      failureId: deterministicMcpId("mcp-stdio-request", { server_id: this.serverId, request_id: requestId, epoch: this.connectionEpoch }),
      category: error.name === "AbortError" ? "cancelled" : "transport",
      code: error.name === "AbortError" ? "request_cancelled" : "stdio_request_failed",
      message: error.message,
      serverId: this.serverId,
      operation: pending.request.method,
      requestId,
      retryable: pending.request.idempotent && error.name !== "AbortError",
      disposition: pending.request.idempotent && error.name !== "AbortError" ? "reconnect_then_retry" : "terminal",
    });
    this.lastFailure = failure;
    this.failedCount += 1;
    this.completed.set(requestId, {
      requestId,
      method: pending.request.method,
      messageDigest: pending.record.messageDigest,
      responseDigest: "",
      idempotencyKey: pending.request.idempotencyKey,
      connectionEpoch: pending.record.connectionEpoch,
      attempt: pending.record.attempt,
      status: error.name === "AbortError" ? "cancelled" : "failed",
      preparedAt: pending.record.preparedAt,
      sentAt: pending.record.sentAt,
      completedAt: this.timestamp(),
      failure,
      response: null,
      metadata: cloneJson(pending.request.metadata),
    });
    this.trimCompleted();
    pending.reject(new McpRuntimeError(failureToInput(failure), { cause: error }));
  }

  private onProcessError(error: Error): void {
    const failure = normalizeFailure(error, {
      failureId: deterministicMcpId("mcp-stdio-error", { server_id: this.serverId, epoch: this.connectionEpoch, message: error.message }),
      category: "transport",
      code: "stdio_transport_error",
      message: error.message,
      serverId: this.serverId,
      operation: "transport",
      retryable: true,
      disposition: "reconnect_then_retry",
    });
    this.lastFailure = failure;
    this.phase = "failed";
    this.emitEvent("transport_error", null, null, { failure });
    for (const requestId of [...this.pending.keys()]) this.failPending(requestId, error);
  }

  private onProcessExit(code: number | null, exitSignal: NodeJS.Signals | null): void {
    const expected = this.phase === "closing" || this.phase === "closed";
    this.emitEvent("process_exit", null, null, {
      exit_code: code,
      signal: exitSignal,
      expected,
      stderr_preview: this.stderrBuffer,
    });
    this.process = null;
    if (expected) return;
    const error = stdioError(
      this.serverId,
      "stdio_process_exited",
      `MCP stdio process exited with code ${code ?? "null"}${exitSignal ? ` signal ${exitSignal}` : ""}`,
      "",
      true,
    );
    this.onProcessError(error);
  }

  private buildEnvironment(): NodeJS.ProcessEnv {
    const output: NodeJS.ProcessEnv = {};
    for (const name of this.config.inheritEnvironment) {
      const value = this.environment[name];
      if (value !== undefined) output[name] = value;
    }
    for (const [name, handle] of Object.entries(this.config.environmentHandles)) {
      const value = this.credentialEnvironment[handle];
      if (value === undefined) throw stdioError(this.serverId, "missing_environment_handle", `missing stdio environment handle ${handle}`);
      output[name] = value;
    }
    output.ZYRA_MCP_SERVER_ID = this.serverId;
    output.ZYRA_MCP_TRANSPORT_ID = this.transportId;
    return output;
  }

  private restoreMetadata(snapshot: McpTransportSnapshot): void {
    if (snapshot.version !== "zyra.mcp-transport/v1") throw stdioError(this.serverId, "unsupported_transport_snapshot", "unsupported stdio transport snapshot");
    if (snapshot.serverId !== this.serverId || snapshot.transportId !== this.transportId) {
      throw stdioError(this.serverId, "transport_snapshot_identity_mismatch", "stdio transport snapshot identity mismatch");
    }
    this.connectionEpoch = snapshot.connectionEpoch;
    this.sequence = snapshot.sequence;
    this.startedAt = snapshot.health.startedAt;
    this.lastActivityAt = snapshot.health.lastActivityAt;
    this.lastMessageAt = snapshot.health.lastMessageAt;
    this.completedCount = snapshot.health.completedRequests;
    this.failedCount = snapshot.health.failedRequests;
    this.reconnectCount = snapshot.health.reconnectCount;
    this.bytesSent = snapshot.health.bytesSent;
    this.bytesReceived = snapshot.health.bytesReceived;
    this.lastFailure = snapshot.health.lastFailure ? cloneJson(snapshot.health.lastFailure) : null;
    for (const completed of snapshot.completed) this.completed.set(completed.requestId, cloneJson(completed));
    for (const receipt of snapshot.idempotencyReceipts) this.receipts.set(receipt.idempotencyKey, cloneJson(receipt));
    for (const event of snapshot.events.slice(-this.maximumEvents)) this.events.push(cloneJson(event));
    this.phase = "idle";
  }

  private emitEvent(
    kind: McpTransportEvent["kind"],
    requestId: string | null,
    messageDigest: string | null,
    details: JsonObject,
  ): void {
    this.sequence += 1;
    const event: McpTransportEvent = {
      eventId: deterministicMcpId("mcp-stdio-event", {
        transport_id: this.transportId,
        epoch: this.connectionEpoch,
        sequence: this.sequence,
        kind,
        request_id: requestId,
      }),
      serverId: this.serverId,
      transportId: this.transportId,
      connectionEpoch: this.connectionEpoch,
      sequence: this.sequence,
      kind,
      requestId,
      messageDigest,
      details: canonicalJson(details) as JsonObject,
      occurredAt: this.timestamp(),
    };
    this.events.push(event);
    if (this.events.length > this.maximumEvents) this.events.splice(0, this.events.length - this.maximumEvents);
    for (const listener of this.eventListeners) void Promise.resolve(listener(cloneJson(event)));
  }

  private trimCompleted(): void {
    while (this.completed.size > this.maximumCompletedRequests) {
      const first = this.completed.keys().next().value as string | undefined;
      if (first === undefined) return;
      this.completed.delete(first);
    }
    while (this.receipts.size > this.maximumCompletedRequests) {
      const first = this.receipts.keys().next().value as string | undefined;
      if (first === undefined) return;
      this.receipts.delete(first);
    }
  }

  private timestamp(): string {
    const next = monotonicNow(this.lastActivityAt, this.now);
    return next;
  }
}

function defaultProcessFactory(input: Parameters<McpProcessFactory>[0]): ChildProcessWithoutNullStreams {
  return spawn(input.command, input.arguments, {
    cwd: input.cwd ?? undefined,
    env: input.environment,
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
    signal: input.signal,
  });
}

async function writeStream(stream: NodeJS.WritableStream, value: string, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) throw Object.assign(new Error("write aborted"), { name: "AbortError" });
  const ready = stream.write(value);
  if (ready) return;
  await once(stream, "drain", { signal });
}

function wireRequestId(message: JsonRpcMessage): string | null {
  if (!("id" in message) || message.id === null || message.id === undefined) return null;
  return String(message.id);
}

function isResponse(message: JsonRpcMessage): message is JsonRpcResponse {
  return "id" in message && ("result" in message || "error" in message);
}

function abortError(serverId: string, requestId: string): McpRuntimeError {
  const error = stdioError(serverId, "request_cancelled", "MCP stdio request was cancelled", requestId, false);
  error.name = "AbortError";
  return error;
}

function stdioError(
  serverId: string,
  code: string,
  message: string,
  requestId = "",
  retryable = false,
): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-stdio", { server_id: serverId, code, request_id: requestId, message }),
    category: code.includes("protocol") || code.includes("json") ? "protocol" : "transport",
    code,
    message,
    serverId,
    requestId,
    retryable,
    disposition: retryable ? "reconnect_then_retry" : "terminal",
  });
}

function failureToInput(failure: McpFailureRecord) {
  return {
    failureId: failure.failure_id,
    category: failure.category,
    code: failure.code,
    message: failure.message,
    serverId: failure.server_id,
    operation: failure.operation,
    requestId: failure.request_id,
    sessionId: failure.session_id,
    retryable: failure.retryable,
    disposition: failure.disposition,
    retryAfterMs: failure.retry_after_ms,
    statusCode: failure.status_code,
    jsonRpcCode: failure.json_rpc_code,
    details: failure.details,
    causeChain: failure.cause_chain,
    occurredAt: failure.occurred_at,
  };
}

function validateOptions(transport: StdioMcpTransport): void {
  const health = transport.health();
  if (!health.serverId) throw stdioError("", "invalid_server_id", "stdio transport server id is required");
}
