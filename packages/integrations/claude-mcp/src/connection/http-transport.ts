import type { JsonObject, JsonRpcMessage, JsonRpcResponse } from "../contracts.ts";
import type { McpHttpTransportConfig } from "../config/config-store.ts";
import {
  canonicalJson,
  cloneJson,
  deterministicMcpId,
  monotonicNow,
  redactMcpSecrets,
  sha256,
} from "../core/canonical.ts";
import {
  failureFromHttp,
  McpRuntimeError,
  normalizeFailure,
  type McpFailureRecord,
} from "../core/failure.ts";
import { McpProtocolCodec } from "../core/protocol.ts";
import type {
  McpCompletedTransportRequest,
  McpFetch,
  McpIdempotencyReceipt,
  McpPendingTransportRequest,
  McpTransportAdapter,
  McpTransportEvent,
  McpTransportHealth,
  McpTransportRequest,
  McpTransportResponse,
  McpTransportSnapshot,
} from "./contracts.ts";
import { McpSseParser } from "./sse-parser.ts";

interface InFlightHttpRequest {
  request: McpTransportRequest;
  record: McpPendingTransportRequest;
  controller: AbortController;
  started: number;
  promise: Promise<McpTransportResponse>;
}

export interface HttpMcpTransportOptions {
  serverId: string;
  config: McpHttpTransportConfig;
  connectTimeoutMs?: number;
  requestTimeoutMs?: number;
  idleTimeoutMs?: number;
  maximumCompletedRequests?: number;
  maximumEvents?: number;
  fetch?: McpFetch;
  codec?: McpProtocolCodec;
  authorizationProvider?: () => Promise<string | null>;
  onAuthenticationChallenge?: (challenge: string, response: Response) => Promise<string | null>;
  credentialHeaders?: Record<string, string>;
  now?: () => Date;
  snapshot?: McpTransportSnapshot | null;
}

export class HttpMcpTransport implements McpTransportAdapter {
  readonly serverId: string;
  readonly transportId: string;
  private readonly config: McpHttpTransportConfig;
  private readonly connectTimeoutMs: number;
  private readonly requestTimeoutMs: number;
  private readonly idleTimeoutMs: number;
  private readonly maximumCompletedRequests: number;
  private readonly maximumEvents: number;
  private readonly fetchValue: McpFetch;
  private readonly codec: McpProtocolCodec;
  private readonly authorizationProvider: () => Promise<string | null>;
  private readonly onAuthenticationChallenge: ((challenge: string, response: Response) => Promise<string | null>) | null;
  private readonly credentialHeaders: Record<string, string>;
  private readonly now: () => Date;
  private readonly messageListeners = new Set<(message: JsonRpcMessage) => void | Promise<void>>();
  private readonly eventListeners = new Set<(event: McpTransportEvent) => void | Promise<void>>();
  private readonly inFlight = new Map<string, InFlightHttpRequest>();
  private readonly completed = new Map<string, McpCompletedTransportRequest>();
  private readonly receipts = new Map<string, McpIdempotencyReceipt>();
  private readonly events: McpTransportEvent[] = [];
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
  private sessionId: string | null = null;
  private protocolVersion: string | null = null;
  private authorizationCache: string | null = null;
  private eventStreamController: AbortController | null = null;
  private eventStreamPromise: Promise<void> | null = null;

  constructor(options: HttpMcpTransportOptions) {
    this.serverId = options.serverId;
    this.config = cloneJson(options.config);
    this.transportId = deterministicMcpId("mcp-http", {
      server_id: this.serverId,
      url: this.config.url,
      kind: this.config.kind,
    });
    this.connectTimeoutMs = options.connectTimeoutMs ?? 15_000;
    this.requestTimeoutMs = options.requestTimeoutMs ?? 60_000;
    this.idleTimeoutMs = options.idleTimeoutMs ?? 300_000;
    this.maximumCompletedRequests = options.maximumCompletedRequests ?? 10_000;
    this.maximumEvents = options.maximumEvents ?? 20_000;
    this.fetchValue = options.fetch ?? defaultFetch;
    this.codec = options.codec ?? new McpProtocolCodec();
    this.authorizationProvider = options.authorizationProvider ?? (async () => null);
    this.onAuthenticationChallenge = options.onAuthenticationChallenge ?? null;
    this.credentialHeaders = { ...(options.credentialHeaders ?? {}) };
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restoreMetadata(options.snapshot);
  }

  async start(signal?: AbortSignal): Promise<McpTransportHealth> {
    if (signal?.aborted) throw httpAbort(this.serverId, "connect");
    if (this.phase === "ready" || this.phase === "degraded") return this.health();
    if (this.phase === "closing") throw httpError(this.serverId, "transport_closing", "cannot start a closing HTTP MCP transport");
    const reconnecting = this.connectionEpoch > 0;
    this.phase = reconnecting ? "reconnecting" : "connecting";
    this.connectionEpoch += 1;
    if (reconnecting) this.reconnectCount += 1;
    this.emitEvent("starting", null, null, {
      endpoint: endpointForAudit(this.config.url),
      transport_kind: this.config.kind,
      reconnecting,
    });
    const controller = linkedAbortController(signal, this.connectTimeoutMs);
    try {
      this.authorizationCache = await this.authorizationProvider();
      this.phase = "ready";
      this.startedAt = this.timestamp();
      this.lastActivityAt = this.startedAt;
      this.emitEvent("started", null, null, {
        endpoint: endpointForAudit(this.config.url),
        authorization_present: this.authorizationCache !== null,
      });
      if (this.config.preferSse || this.config.kind === "sse") this.ensureEventStream();
      return this.health();
    } catch (error) {
      this.phase = "failed";
      const failure = normalizeFailure(error, {
        failureId: deterministicMcpId("mcp-http-start", { server_id: this.serverId, epoch: this.connectionEpoch }),
        category: "transport",
        code: "http_transport_start_failed",
        message: `failed to start HTTP MCP transport ${this.serverId}`,
        serverId: this.serverId,
        operation: "connect",
        retryable: true,
        disposition: "reconnect_then_retry",
      });
      this.lastFailure = failure;
      throw new McpRuntimeError(failureToInput(failure), { cause: error });
    } finally {
      controller.dispose();
    }
  }

  async request(request: McpTransportRequest): Promise<McpTransportResponse> {
    if (request.signal?.aborted) throw httpAbort(this.serverId, request.requestId);
    const requestDigest = sha256(request.message);
    if (request.idempotencyKey) {
      const receipt = this.receipts.get(request.idempotencyKey);
      if (receipt) {
        if (receipt.requestDigest !== requestDigest) throw httpError(this.serverId, "idempotency_key_reused", "HTTP MCP idempotency key is bound to another payload", request.requestId);
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
      const coalesced = [...this.inFlight.values()].find((entry) => entry.request.idempotencyKey === request.idempotencyKey);
      if (coalesced) {
        if (sha256(coalesced.request.message) !== requestDigest) throw httpError(this.serverId, "idempotency_key_inflight_conflict", "HTTP MCP idempotency key has conflicting in-flight payload", request.requestId);
        const response = await coalesced.promise;
        return {
          ...cloneJson(response),
          requestId: request.requestId,
          replayed: true,
          metadata: { ...response.metadata, coalesced_request_id: coalesced.request.requestId },
        };
      }
    }
    if (this.inFlight.has(request.requestId) || this.completed.has(request.requestId)) {
      throw httpError(this.serverId, "duplicate_request_id", `duplicate HTTP MCP request ${request.requestId}`, request.requestId);
    }
    await this.start(request.signal);
    const timeoutMs = request.timeoutMs > 0 ? request.timeoutMs : this.requestTimeoutMs;
    const preparedAt = this.timestamp();
    const controller = linkedAbortController(request.signal, timeoutMs);
    const record: McpPendingTransportRequest = {
      requestId: request.requestId,
      method: request.method,
      messageDigest: requestDigest,
      idempotent: request.idempotent,
      idempotencyKey: request.idempotencyKey,
      connectionEpoch: this.connectionEpoch,
      preparedAt,
      sentAt: null,
      deadlineAt: new Date(Date.parse(preparedAt) + timeoutMs).toISOString(),
      attempt: 1,
      metadata: cloneJson(request.metadata),
    };
    const started = performance.now();
    const promise = this.executeRequest(request, record, controller, started);
    this.inFlight.set(request.requestId, {
      request,
      record,
      controller: controller.controller,
      started,
      promise,
    });
    try {
      return await promise;
    } finally {
      controller.dispose();
      this.inFlight.delete(request.requestId);
    }
  }

  async notify(message: JsonRpcMessage, signal?: AbortSignal): Promise<void> {
    const requestId = deterministicMcpId("mcp-http-notification", {
      server_id: this.serverId,
      epoch: this.connectionEpoch,
      sequence: this.sequence + 1,
      message,
    });
    const response = await this.request({
      requestId,
      method: "notification",
      message,
      timeoutMs: this.requestTimeoutMs,
      idempotent: true,
      idempotencyKey: requestId,
      authorization: null,
      headers: {},
      signal,
      metadata: { notification: true },
    });
    void response;
  }

  async close(reason = "requested"): Promise<void> {
    if (this.phase === "closed") return;
    this.phase = "closing";
    this.emitEvent("closing", null, null, { reason });
    for (const entry of this.inFlight.values()) entry.controller.abort(new Error(`transport closed: ${reason}`));
    this.inFlight.clear();
    this.eventStreamController?.abort(new Error(`transport closed: ${reason}`));
    if (this.eventStreamPromise) {
      try {
        await this.eventStreamPromise;
      } catch {
        // close observes the terminal state through the event log
      }
    }
    if (this.sessionId) {
      const controller = linkedAbortController(undefined, Math.min(this.requestTimeoutMs, 5_000));
      try {
        await this.fetchValue({
          url: this.config.url,
          init: {
            method: "DELETE",
            headers: await this.buildHeaders({}, null),
            signal: controller.controller.signal,
            redirect: "manual",
          },
        });
      } catch {
        // server-side session cleanup is best effort; local closure is authoritative
      } finally {
        controller.dispose();
      }
    }
    this.phase = "closed";
    this.sessionId = null;
    this.eventStreamController = null;
    this.eventStreamPromise = null;
    this.emitEvent("closed", null, null, { reason });
  }

  health(): McpTransportHealth {
    return {
      transportId: this.transportId,
      serverId: this.serverId,
      phase: this.phase,
      connectionEpoch: this.connectionEpoch,
      connected: this.phase === "ready" || this.phase === "degraded",
      startedAt: this.startedAt,
      lastActivityAt: this.lastActivityAt,
      lastMessageAt: this.lastMessageAt,
      pendingRequests: this.inFlight.size,
      completedRequests: this.completedCount,
      failedRequests: this.failedCount,
      reconnectCount: this.reconnectCount,
      bytesSent: this.bytesSent,
      bytesReceived: this.bytesReceived,
      processId: null,
      endpoint: endpointForAudit(this.config.url),
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
      pending: [...this.inFlight.values()].map((entry) => cloneJson(entry.record)),
      completed: [...this.completed.values()].map(cloneJson),
      idempotencyReceipts: [...this.receipts.values()].map(cloneJson),
      metadata: {
        endpoint: endpointForAudit(this.config.url),
        session_id: this.sessionId,
        protocol_version: this.protocolVersion,
        authorization_present: this.authorizationCache !== null,
        event_stream_active: this.eventStreamPromise !== null,
        live_connection_restored: false,
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

  private async executeRequest(
    request: McpTransportRequest,
    record: McpPendingTransportRequest,
    linked: LinkedAbort,
    started: number,
  ): Promise<McpTransportResponse> {
    const encoded = this.codec.encode(request.message);
    const bodyBytes = Buffer.byteLength(encoded, "utf8");
    this.bytesSent += bodyBytes;
    record.sentAt = this.timestamp();
    this.emitEvent("message_sent", request.requestId, record.messageDigest, {
      method: request.method,
      bytes: bodyBytes,
      idempotent: request.idempotent,
      idempotency_key_present: request.idempotencyKey !== null,
    });
    let response: Response;
    let authorization = request.authorization ?? this.authorizationCache;
    try {
      response = await this.fetchOnce(request, encoded, authorization, linked.controller.signal);
      if (response.status === 401 && this.onAuthenticationChallenge) {
        const challenge = response.headers.get("www-authenticate") ?? "";
        const refreshed = await this.onAuthenticationChallenge(challenge, response);
        if (refreshed && refreshed !== authorization) {
          authorization = refreshed;
          this.authorizationCache = refreshed;
          response = await this.fetchOnce(request, encoded, authorization, linked.controller.signal);
        }
      }
    } catch (error) {
      return this.failRequest(request, record, started, error);
    }
    this.captureSessionHeaders(response);
    const responseHeaders = headersRecord(response.headers);
    this.emitEvent("http_response", request.requestId, null, {
      status: response.status,
      content_type: response.headers.get("content-type") ?? "",
      session_id_present: this.sessionId !== null,
      protocol_version: this.protocolVersion,
    });
    if (!response.ok && response.status !== 202) {
      const failure = failureFromHttp(response, this.serverId, request.method, request.requestId, {
        response_headers: redactMcpSecrets(responseHeaders) as JsonObject,
      });
      return this.failRequest(request, record, started, new McpRuntimeError(failureToInput(failure)));
    }
    try {
      const message = await this.readResponseMessage(response, request);
      const responseDigest = sha256(message);
      const completedAt = this.timestamp();
      const completed: McpCompletedTransportRequest = {
        requestId: request.requestId,
        method: request.method,
        messageDigest: record.messageDigest,
        responseDigest,
        idempotencyKey: request.idempotencyKey,
        connectionEpoch: record.connectionEpoch,
        attempt: record.attempt,
        status: "committed",
        preparedAt: record.preparedAt,
        sentAt: record.sentAt,
        completedAt,
        failure: null,
        response: cloneJson(message),
        metadata: cloneJson(request.metadata),
      };
      this.completed.set(request.requestId, completed);
      this.completedCount += 1;
      this.lastActivityAt = completedAt;
      this.lastMessageAt = completedAt;
      if (request.idempotencyKey) {
        this.receipts.set(request.idempotencyKey, {
          idempotencyKey: request.idempotencyKey,
          requestId: request.requestId,
          requestDigest: record.messageDigest,
          responseDigest,
          response: cloneJson(message),
          connectionEpoch: record.connectionEpoch,
          committedAt: completedAt,
        });
      }
      this.trimHistory();
      return {
        requestId: request.requestId,
        message: cloneJson(message),
        transportId: this.transportId,
        connectionEpoch: record.connectionEpoch,
        elapsedMs: Math.max(0, performance.now() - started),
        replayed: false,
        headers: responseHeaders,
        metadata: {
          status: response.status,
          session_id_present: this.sessionId !== null,
          completed_at: completedAt,
        },
      };
    } catch (error) {
      return this.failRequest(request, record, started, error);
    }
  }

  private async fetchOnce(
    request: McpTransportRequest,
    body: string,
    authorization: string | null,
    signal: AbortSignal,
  ): Promise<Response> {
    const response = await this.fetchValue({
      url: this.config.url,
      init: {
        method: "POST",
        headers: await this.buildHeaders(request.headers, authorization),
        body,
        signal,
        redirect: "manual",
      },
    });
    if (isRedirect(response.status)) {
      const location = response.headers.get("location");
      if (!location) throw httpError(this.serverId, "redirect_without_location", "MCP HTTP redirect has no Location header", request.requestId);
      const redirectUrl = new URL(location, this.config.url);
      if (!this.redirectAllowed(redirectUrl)) throw httpError(this.serverId, "redirect_origin_denied", `MCP HTTP redirect origin ${redirectUrl.origin} is not allowed`, request.requestId);
      return this.fetchValue({
        url: redirectUrl.toString(),
        init: {
          method: "POST",
          headers: await this.buildHeaders(request.headers, authorization),
          body,
          signal,
          redirect: "manual",
        },
      });
    }
    return response;
  }

  private async buildHeaders(requestHeaders: Record<string, string>, authorization: string | null): Promise<Headers> {
    const headers = new Headers();
    headers.set("accept", "application/json, text/event-stream");
    headers.set("content-type", "application/json");
    headers.set("user-agent", "zyra-mcp-runtime/1");
    for (const [name, value] of Object.entries(this.config.headers)) headers.set(name, value);
    for (const [name, value] of Object.entries(this.credentialHeaders)) headers.set(name, value);
    for (const [name, value] of Object.entries(requestHeaders)) headers.set(name, value);
    if (authorization) headers.set("authorization", authorization);
    if (this.sessionId) headers.set("mcp-session-id", this.sessionId);
    if (this.protocolVersion) headers.set("mcp-protocol-version", this.protocolVersion);
    return headers;
  }

  private async readResponseMessage(response: Response, request: McpTransportRequest): Promise<JsonRpcMessage> {
    const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
    if (response.status === 202) {
      return this.codec.success(wireId(request.message), {
        accepted: true,
        request_id: request.requestId,
      });
    }
    if (contentType.includes("text/event-stream")) {
      if (!response.body) throw httpError(this.serverId, "empty_sse_response", "MCP SSE response has no body", request.requestId);
      const messages = await consumeSseBody(response.body, this.codec, (event) => {
        this.bytesReceived += event.rawBytes;
        this.emitEvent("sse_event", request.requestId, sha256(event.data), {
          event: event.event,
          event_id: event.eventId,
          bytes: event.rawBytes,
        });
      });
      const matching = messages.find((message) => isResponseFor(message, request.message));
      if (!matching) throw httpError(this.serverId, "missing_sse_response", "MCP SSE response stream ended without matching JSON-RPC response", request.requestId);
      for (const message of messages) {
        if (message !== matching) for (const listener of this.messageListeners) void Promise.resolve(listener(cloneJson(message)));
      }
      return matching;
    }
    const text = await response.text();
    this.bytesReceived += Buffer.byteLength(text, "utf8");
    if (!text.trim()) throw httpError(this.serverId, "empty_http_response", "MCP HTTP response body is empty", request.requestId);
    const message = this.codec.decodeText(text);
    this.emitEvent("message_received", request.requestId, sha256(message), {
      bytes: Buffer.byteLength(text, "utf8"),
      content_type: contentType,
    });
    return message;
  }

  private failRequest(
    request: McpTransportRequest,
    record: McpPendingTransportRequest,
    _started: number,
    error: unknown,
  ): never {
    const failure = normalizeFailure(error, {
      failureId: deterministicMcpId("mcp-http-request", {
        server_id: this.serverId,
        request_id: request.requestId,
        epoch: record.connectionEpoch,
        request_digest: record.messageDigest,
      }),
      category: request.signal?.aborted ? "cancelled" : "transport",
      code: request.signal?.aborted ? "request_cancelled" : "http_request_failed",
      message: error instanceof Error ? error.message : String(error),
      serverId: this.serverId,
      operation: request.method,
      requestId: request.requestId,
      retryable: request.idempotent && !request.signal?.aborted,
      disposition: request.idempotent && !request.signal?.aborted ? "reconnect_then_retry" : "terminal",
    });
    this.lastFailure = failure;
    this.failedCount += 1;
    this.completed.set(request.requestId, {
      requestId: request.requestId,
      method: request.method,
      messageDigest: record.messageDigest,
      responseDigest: "",
      idempotencyKey: request.idempotencyKey,
      connectionEpoch: record.connectionEpoch,
      attempt: record.attempt,
      status: request.signal?.aborted ? "cancelled" : "failed",
      preparedAt: record.preparedAt,
      sentAt: record.sentAt,
      completedAt: this.timestamp(),
      failure,
      response: null,
      metadata: cloneJson(request.metadata),
    });
    this.trimHistory();
    throw new McpRuntimeError(failureToInput(failure), { cause: error });
  }

  private ensureEventStream(): void {
    if (this.eventStreamPromise || this.phase === "closing" || this.phase === "closed") return;
    const controller = new AbortController();
    this.eventStreamController = controller;
    this.eventStreamPromise = this.runEventStream(controller.signal)
      .catch((error) => {
        if (controller.signal.aborted) return;
        const failure = normalizeFailure(error, {
          failureId: deterministicMcpId("mcp-http-sse", { server_id: this.serverId, epoch: this.connectionEpoch }),
          category: "transport",
          code: "sse_stream_failed",
          message: `MCP SSE event stream failed for ${this.serverId}`,
          serverId: this.serverId,
          operation: "sse",
          retryable: true,
          disposition: "reconnect_then_retry",
        });
        this.lastFailure = failure;
        if (this.phase === "ready") this.phase = "degraded";
        this.emitEvent("transport_error", null, null, { failure });
      })
      .finally(() => {
        this.eventStreamController = null;
        this.eventStreamPromise = null;
      });
  }

  private async runEventStream(signal: AbortSignal): Promise<void> {
    const response = await this.fetchValue({
      url: this.config.url,
      init: {
        method: "GET",
        headers: await this.buildHeaders({ accept: "text/event-stream" }, this.authorizationCache),
        signal,
        redirect: "manual",
      },
    });
    if (!response.ok) throw new McpRuntimeError(failureToInput(failureFromHttp(response, this.serverId, "sse", "event-stream")));
    if (!response.body) throw httpError(this.serverId, "empty_sse_stream", "MCP SSE event stream has no body");
    this.captureSessionHeaders(response);
    this.emitEvent("sse_opened", null, null, { status: response.status });
    const messages = await consumeSseBody(response.body, this.codec, (event) => {
      this.bytesReceived += event.rawBytes;
      this.lastActivityAt = this.timestamp();
      this.emitEvent("sse_event", null, sha256(event.data), {
        event: event.event,
        event_id: event.eventId,
        bytes: event.rawBytes,
      });
    });
    for (const message of messages) {
      this.lastMessageAt = this.timestamp();
      for (const listener of this.messageListeners) await listener(cloneJson(message));
    }
    this.emitEvent("sse_closed", null, null, { message_count: messages.length });
  }

  private captureSessionHeaders(response: Response): void {
    const session = response.headers.get("mcp-session-id");
    if (session) this.sessionId = session;
    const protocol = response.headers.get("mcp-protocol-version");
    if (protocol) this.protocolVersion = protocol;
  }

  private redirectAllowed(url: URL): boolean {
    const original = new URL(this.config.url);
    if (url.origin === original.origin) return true;
    return this.config.allowedRedirectOrigins.includes(url.origin);
  }

  private restoreMetadata(snapshot: McpTransportSnapshot): void {
    if (snapshot.version !== "zyra.mcp-transport/v1") throw httpError(this.serverId, "unsupported_transport_snapshot", "unsupported HTTP transport snapshot");
    if (snapshot.serverId !== this.serverId || snapshot.transportId !== this.transportId) throw httpError(this.serverId, "transport_snapshot_identity_mismatch", "HTTP transport snapshot identity mismatch");
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
    this.sessionId = typeof snapshot.metadata.session_id === "string" ? snapshot.metadata.session_id : null;
    this.protocolVersion = typeof snapshot.metadata.protocol_version === "string" ? snapshot.metadata.protocol_version : null;
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
      eventId: deterministicMcpId("mcp-http-event", {
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

  private trimHistory(): void {
    while (this.completed.size > this.maximumCompletedRequests) {
      const key = this.completed.keys().next().value as string | undefined;
      if (!key) break;
      this.completed.delete(key);
    }
    while (this.receipts.size > this.maximumCompletedRequests) {
      const key = this.receipts.keys().next().value as string | undefined;
      if (!key) break;
      this.receipts.delete(key);
    }
  }

  private timestamp(): string {
    return monotonicNow(this.lastActivityAt, this.now);
  }
}

interface LinkedAbort {
  controller: AbortController;
  dispose(): void;
}

function linkedAbortController(parent: AbortSignal | undefined, timeoutMs: number): LinkedAbort {
  const controller = new AbortController();
  const onAbort = (): void => controller.abort(parent?.reason ?? new Error("request aborted"));
  if (parent) parent.addEventListener("abort", onAbort, { once: true });
  const timer = setTimeout(() => controller.abort(Object.assign(new Error(`request timed out after ${timeoutMs}ms`), { name: "TimeoutError" })), timeoutMs);
  return {
    controller,
    dispose(): void {
      clearTimeout(timer);
      parent?.removeEventListener("abort", onAbort);
    },
  };
}

async function defaultFetch(input: { url: string; init: RequestInit }): Promise<Response> {
  return fetch(input.url, input.init);
}

async function consumeSseBody(
  stream: ReadableStream<Uint8Array>,
  codec: McpProtocolCodec,
  observe: (event: ReturnType<McpSseParser["push"]>[number]) => void,
): Promise<JsonRpcMessage[]> {
  const reader = stream.getReader();
  const parser = new McpSseParser();
  const messages: JsonRpcMessage[] = [];
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      for (const event of parser.push(chunk.value)) {
        observe(event);
        if (event.event === "message" || event.event === "jsonrpc") messages.push(codec.decodeText(event.data));
      }
    }
    for (const event of parser.finish()) {
      observe(event);
      if (event.event === "message" || event.event === "jsonrpc") messages.push(codec.decodeText(event.data));
    }
    return messages;
  } finally {
    reader.releaseLock();
  }
}

function headersRecord(headers: Headers): Record<string, string> {
  const output: Record<string, string> = {};
  headers.forEach((value, name) => {
    output[name.toLowerCase()] = value;
  });
  return output;
}

function endpointForAudit(value: string): string {
  const url = new URL(value);
  url.username = "";
  url.password = "";
  url.search = "";
  url.hash = "";
  return url.toString();
}

function wireId(message: JsonRpcMessage): string | number {
  if ("id" in message && message.id !== null && message.id !== undefined) return message.id;
  return deterministicMcpId("notification", message);
}

function isResponseFor(message: JsonRpcMessage, request: JsonRpcMessage): boolean {
  if (!("id" in message) || !("id" in request)) return false;
  return String(message.id) === String(request.id) && ("result" in message || "error" in message);
}

function isRedirect(status: number): boolean {
  return status === 301 || status === 302 || status === 303 || status === 307 || status === 308;
}

function httpAbort(serverId: string, requestId: string): McpRuntimeError {
  const error = httpError(serverId, "request_cancelled", "MCP HTTP request was cancelled", requestId, false);
  error.name = "AbortError";
  return error;
}

function httpError(
  serverId: string,
  code: string,
  message: string,
  requestId = "",
  retryable = false,
): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-http", { server_id: serverId, code, request_id: requestId, message }),
    category: code.includes("auth") ? "authentication" : code.includes("json") || code.includes("sse") ? "protocol" : "transport",
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
