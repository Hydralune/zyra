import type {
  ProviderDispatchAttempt,
  ProviderDispatchRequest,
  ProviderDispatchResult,
  ProviderRouteLease,
  SecretResolver,
} from "../contracts.ts";
import {
  type Clock,
  type IdFactory,
  RandomIdFactory,
  SystemClock,
  assertNonEmpty,
  assertPositiveInteger,
  canonicalize,
  deepClone,
  digestText,
  normalizeHeaders,
  redactHeaders,
  sleep,
} from "../canonical.ts";
import { CredentialManager } from "../credentials.ts";
import { ProviderControlPlaneError, asProviderError } from "../errors.ts";
import { classifyProviderResponse, classifyTransportException } from "../quirks/error-classifier.ts";
import { ProviderControlPlaneStore } from "../store.ts";
import { ProviderRoutePlanner, routeUrl } from "../routing.ts";
import { ModelFallbackPolicy } from "../model-fallback-policy.ts";
import { ProviderStreamSupervisor, type ProviderStreamCompletion } from "../stream-supervisor.ts";
import { ProviderDispatchLifecycle } from "../dispatch-lifecycle.ts";
import { ProviderRouteHealthRuntime, type ProviderAdmissionPermit } from "../route-health.ts";
import { ProviderCredentialPoolRuntime } from "../credential-pool.ts";
import { decodeProviderEvent, encodeProviderBody, protocolHeaders, ProtocolFrameState } from "./protocols.ts";
import { readSse } from "./sse.ts";

export interface ProviderTransportOptions {
  readonly fetch?: typeof fetch;
  readonly clock?: Clock;
  readonly ids?: IdFactory;
  readonly userAgent?: string;
}

export class ProviderTransportRuntime {
  private readonly fetchImpl: typeof fetch;
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly userAgent: string;
  private readonly store: ProviderControlPlaneStore;
  private readonly routes: ProviderRoutePlanner;
  private readonly credentials: CredentialManager;
  private readonly fallback: ModelFallbackPolicy;
  private readonly lifecycle: ProviderDispatchLifecycle | null;
  private readonly routeHealth: ProviderRouteHealthRuntime | null;
  private readonly credentialPool: ProviderCredentialPoolRuntime | null;

  constructor(
    store: ProviderControlPlaneStore,
    routes: ProviderRoutePlanner,
    credentials: CredentialManager,
    _secrets: SecretResolver,
    options: ProviderTransportOptions = {},
    lifecycle: ProviderDispatchLifecycle | null = null,
    routeHealth: ProviderRouteHealthRuntime | null = null,
    credentialPool: ProviderCredentialPoolRuntime | null = null,
  ) {
    this.store = store;
    this.routes = routes;
    this.credentials = credentials;
    this.fetchImpl = options.fetch ?? fetch;
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.userAgent = options.userAgent ?? "Zyra-ProviderControlPlane/1";
    this.fallback = new ModelFallbackPolicy();
    this.lifecycle = lifecycle;
    this.routeHealth = routeHealth;
    this.credentialPool = credentialPool;
  }

  async dispatch(
    request: ProviderDispatchRequest,
    signal?: AbortSignal,
    lifecycleOwnerToken = "",
  ): Promise<ProviderDispatchResult> {
    validateDispatchRequest(request);
    let lease = this.routes.require(request.routeId);
    assertDispatchIdentity(request, lease);
    const allAttempts: ProviderDispatchAttempt[] = [];
    let lastError: ProviderControlPlaneError | null = null;

    for (let attemptNumber = 1; attemptNumber <= lease.retryPolicy.maximumAttempts; attemptNumber += 1) {
      let result: ProviderDispatchResult | null = null;
      try {
        result = await this.dispatchOnce(request, lease, attemptNumber, allAttempts, signal, lifecycleOwnerToken);
        this.credentials.recordSuccessIfCurrent(lease.credentialId, lease.credentialVersion);
        return { ...result, attempts: deepClone(allAttempts) };
      } catch (error) {
        lastError = asProviderError(error);
        if (lastError.credentialId === lease.credentialId && lastError.kind !== "credential_version_conflict") {
          try { this.credentials.recordFailure(lease.credentialId, lease.credentialVersion, lastError.kind); } catch { /* preserve transport error */ }
        }
        const decision = this.fallback.decide(lastError, lease, attemptNumber, allAttempts);
        if (!decision.retry) throw lastError;
        if (decision.changeRoute) {
          if (request.routeFallbackPolicy === "pin_initial_route") {
            if (decision.rotateCredential) throw lastError;
          } else {
            lease = this.routes.failover(lease.routeId, lastError.kind);
          }
        }
        await sleep(decision.delayMilliseconds, signal);
      }
    }
    throw lastError ?? new ProviderControlPlaneError({
      layer: "transport",
      kind: "unknown_provider_failure",
      message: "provider dispatch exhausted attempts",
      routeId: lease.routeId,
    });
  }

  private async dispatchOnce(
    request: ProviderDispatchRequest,
    lease: ProviderRouteLease,
    attemptNumber: number,
    attempts: ProviderDispatchAttempt[],
    signal?: AbortSignal,
    lifecycleOwnerToken = "",
  ): Promise<ProviderDispatchResult> {
    const body = encodeProviderBody(lease, { ...request, routeId: lease.routeId });
    const preparedRequestBytes = Buffer.byteLength(body);
    let bytesSent = 0;
    const attemptId = this.ids.next("provider_attempt");
    const startedAt = this.clock.now();
    let attempt: ProviderDispatchAttempt = {
      attemptId,
      dispatchId: request.dispatchId,
      routeId: lease.routeId,
      attempt: attemptNumber,
      startedAt,
      completedAt: null,
      httpStatus: null,
      requestBytes: 0,
      responseBytes: 0,
      outputObserved: false,
      outcome: "running",
      failureKind: null,
      recoveryIntent: null,
      retryAfterMilliseconds: null,
      requestDigest: digestText(body),
      responseDigest: null,
      metadata: {
        url: routeUrl(lease),
        protocol: lease.protocol,
        preparedRequestBytes,
      },
    };
    this.store.putAttempt(attempt);
    attempts.push(attempt);
    this.credentialPool?.recordAttempt(lease);

    let responseBytes = 0;
    const frameState = new ProtocolFrameState(
      { ...request, routeId: lease.routeId },
      this.ids,
      () => this.clock.now(),
    );
    const streamSupervisor = new ProviderStreamSupervisor(
      { ...request, routeId: lease.routeId },
      lease,
      { now: () => this.clock.now() },
    );
    let streamCompletion: ProviderStreamCompletion | null = null;
    let admissionPermit: ProviderAdmissionPermit | null = null;
    const frames = [];
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(new Error(`provider request timed out after ${request.timeoutMilliseconds} ms`)),
      request.timeoutMilliseconds,
    );
    const combinedSignal = signal === undefined ? controller.signal : AbortSignal.any([signal, controller.signal]);
    try {
      // Credential validity is checked before fetch. Revoked, expired, blocked,
      // unresolved, or version-conflicted records therefore send zero bytes.
      const resolved = await this.credentials.resolveRoutePinned(
        lease.routeId,
        lease.credentialId,
        lease.credentialVersion,
        combinedSignal,
      );
      const headers = normalizeHeaders({
        ...lease.requestHeaders,
        ...protocolHeaders(lease),
        ...resolved.headers,
        "content-type": "application/json",
        "user-agent": this.userAgent,
        "idempotency-key": request.idempotencyKey,
      });
      assertRouteEndpointAllowed(lease);
      admissionPermit = this.routeHealth?.acquire(lease, combinedSignal) ?? null;
      bytesSent = preparedRequestBytes;
      attempt = { ...attempt, requestBytes: bytesSent };
      replaceAttempt(attempts, attempt);
      this.store.putAttempt(attempt);
      const response = await this.fetchImpl(routeUrl(lease), {
        method: "POST",
        headers,
        body,
        signal: combinedSignal,
        redirect: "manual",
      });
      if (response.status >= 300 && response.status < 400) {
        const location = response.headers.get("location") ?? "";
        throw new ProviderControlPlaneError({
          layer: "transport",
          kind: "response_protocol_error",
          message: "provider redirect rejected by route host policy",
          retryable: false,
          recoveryIntent: "surface_to_operator",
          httpStatus: response.status,
          providerId: lease.providerId,
          modelId: lease.modelId,
          routeId: lease.routeId,
          credentialId: lease.credentialId,
          bytesSent,
          detail: { location: redactLocation(location) },
        });
      }
      const contentType = response.headers.get("content-type") ?? "";
      if (!response.ok) {
        const errorBody = await response.text();
        responseBytes = Buffer.byteLength(errorBody);
        throw classifyProviderResponse({
          status: response.status,
          headers: response.headers,
          body: errorBody,
          providerId: lease.providerId,
          modelId: lease.modelId,
          routeId: lease.routeId,
          credentialId: lease.credentialId,
          bytesSent,
          bytesReceived: responseBytes,
          outputObserved: false,
        });
      }
      if (!contentType.toLowerCase().includes("text/event-stream")) {
        const text = await response.text();
        responseBytes = Buffer.byteLength(text);
        const decodedFrames = decodeProviderEvent(lease, text, "response.completed", frameState);
        if (lifecycleOwnerToken && this.lifecycle) this.lifecycle.observeFrames(request.dispatchId, lifecycleOwnerToken, decodedFrames);
        streamSupervisor.observe(decodedFrames);
        frames.push(...decodedFrames);
      } else {
        for await (const event of readSse(response, {
          chunkTimeoutMilliseconds: request.chunkTimeoutMilliseconds,
          signal: combinedSignal,
        })) {
          responseBytes += Buffer.byteLength(event.data);
          const decodedFrames = decodeProviderEvent(lease, event.data, event.event, frameState);
          if (lifecycleOwnerToken && this.lifecycle) this.lifecycle.observeFrames(request.dispatchId, lifecycleOwnerToken, decodedFrames);
          streamSupervisor.observe(decodedFrames);
          frames.push(...decodedFrames);
        }
      }
      streamCompletion = streamSupervisor.complete({
        requireTerminalFrame: contentType.toLowerCase().includes("text/event-stream"),
      });
      attempt = {
        ...attempt,
        completedAt: this.clock.now(),
        httpStatus: response.status,
        responseBytes,
        outputObserved: frameState.outputObserved,
        outcome: "succeeded",
        responseDigest: digestText(frames.map((frame) => `${frame.sequence}:${frame.kind}:${frame.text ?? ""}`).join("\n")),
        metadata: {
          ...attempt.metadata,
          requestHeaders: redactHeaders(headers),
          streamEvidenceDigest: streamCompletion.snapshot.evidenceDigest,
          streamFrameCount: streamCompletion.snapshot.frameCount,
          streamToolCallCount: streamCompletion.snapshot.toolCalls.length,
          streamReplaySafe: streamCompletion.replaySafe,
        },
      };
      replaceAttempt(attempts, attempt);
      this.store.putAttempt(attempt);
      if (admissionPermit && this.routeHealth) {
        this.routeHealth.recordSuccess(admissionPermit, this.clock.now() - startedAt, response.status);
      }
      this.credentialPool?.recordSuccess(lease);
      return {
        dispatchId: request.dispatchId,
        routeId: lease.routeId,
        providerId: lease.providerId,
        modelId: lease.modelId,
        protocol: lease.protocol,
        text: frameState.text,
        frames,
        attempts: [],
        usage: deepClone(frameState.usage),
        stopReason: frameState.stopReason,
        completedAt: this.clock.now(),
        metadata: {
          catalogRevision: lease.catalogRevision,
          credentialVersion: lease.credentialVersion,
          requestBytes: bytesSent,
          responseBytes,
          streamEvidenceDigest: streamCompletion.snapshot.evidenceDigest,
          streamReplaySafe: streamCompletion.replaySafe,
        },
      };
    } catch (error) {
      const classified = error instanceof ProviderControlPlaneError
        ? error
        : classifyTransportException(error, {
            providerId: lease.providerId,
            modelId: lease.modelId,
            routeId: lease.routeId,
            credentialId: lease.credentialId,
            bytesSent,
            bytesReceived: responseBytes,
            outputObserved: frameState.outputObserved || streamSupervisor.outputObserved,
          });
      attempt = {
        ...attempt,
        completedAt: this.clock.now(),
        httpStatus: classified.httpStatus,
        responseBytes: classified.bytesReceived || responseBytes,
        outputObserved: classified.outputObserved || frameState.outputObserved || streamSupervisor.outputObserved,
        outcome: classified.kind === "request_aborted" ? "aborted" : "failed",
        failureKind: classified.kind,
        recoveryIntent: classified.recoveryIntent,
        retryAfterMilliseconds: classified.retryAfterMilliseconds,
        metadata: {
          ...attempt.metadata,
          streamEvidenceDigest: streamSupervisor.snapshot().evidenceDigest,
          streamFrameCount: streamSupervisor.snapshot().frameCount,
          streamReplaySafe: streamSupervisor.replaySafe,
          streamRecoveryAction: streamSupervisor.recoveryFor(classified),
        },
      };
      replaceAttempt(attempts, attempt);
      this.store.putAttempt(attempt);
      if (admissionPermit && this.routeHealth) {
        this.routeHealth.recordFailure(admissionPermit, {
          latencyMilliseconds: this.clock.now() - startedAt,
          httpStatus: classified.httpStatus,
          failureKind: classified.kind,
          retryAfterMilliseconds: classified.retryAfterMilliseconds,
        });
      }
      this.credentialPool?.recordFailure(
        lease,
        classified.kind,
        classified.retryAfterMilliseconds,
      );
      throw classified;
    } finally {
      clearTimeout(timeout);
      if (admissionPermit && this.routeHealth) this.routeHealth.release(admissionPermit);
    }
  }
}

function validateDispatchRequest(request: ProviderDispatchRequest): void {
  for (const [name, value] of Object.entries({
    dispatchId: request.dispatchId,
    routeId: request.routeId,
    runId: request.runId,
    taskId: request.taskId,
    sessionId: request.sessionId,
    turnId: request.turnId,
    idempotencyKey: request.idempotencyKey,
  })) assertNonEmpty(value, name);
  if (!["allow_route_change", "pin_initial_route"].includes(request.routeFallbackPolicy)) {
    throw new TypeError("routeFallbackPolicy must be allow_route_change or pin_initial_route");
  }
  assertPositiveInteger(request.maximumOutputTokens, "maximumOutputTokens");
  assertPositiveInteger(request.timeoutMilliseconds, "timeoutMilliseconds");
  assertPositiveInteger(request.chunkTimeoutMilliseconds, "chunkTimeoutMilliseconds");
  if (request.temperature !== null && (!Number.isFinite(request.temperature) || request.temperature < 0 || request.temperature > 2)) {
    throw new TypeError("temperature must be null or between 0 and 2");
  }
  if (request.messages.length === 0) throw new TypeError("provider dispatch requires at least one message");
}

function assertDispatchIdentity(request: ProviderDispatchRequest, lease: ProviderRouteLease): void {
  for (const [name, requestValue, leaseValue] of [
    ["runId", request.runId, lease.runId],
    ["taskId", request.taskId, lease.taskId],
    ["sessionId", request.sessionId, lease.sessionId],
    ["turnId", request.turnId, lease.turnId],
  ] as const) {
    if (requestValue !== leaseValue) {
      throw new ProviderControlPlaneError({
        layer: "route",
        kind: "route_policy_rejected",
        message: `dispatch ${name} does not match provider route custody`,
        routeId: lease.routeId,
        detail: { requestValue, leaseValue },
      });
    }
  }
}

function assertRouteEndpointAllowed(lease: ProviderRouteLease): void {
  const target = new URL(routeUrl(lease));
  const allowed = new Set(lease.allowedHosts.map((host) => host.toLowerCase()));
  const base = new URL(lease.baseUrl);
  allowed.add(base.hostname.toLowerCase());
  if (!allowed.has(target.hostname.toLowerCase())) {
    throw new ProviderControlPlaneError({
      layer: "transport",
      kind: "route_policy_rejected",
      message: "provider endpoint host is outside the pinned allowlist",
      providerId: lease.providerId,
      modelId: lease.modelId,
      routeId: lease.routeId,
      credentialId: lease.credentialId,
      detail: { host: target.hostname, allowedHosts: [...allowed].sort() },
    });
  }
}

function redactLocation(value: string): string {
  if (!value) return "";
  try {
    const url = new URL(value);
    url.username = "";
    url.password = "";
    url.search = "";
    url.hash = "";
    return url.toString();
  } catch {
    return "[invalid redirect location]";
  }
}

function replaceAttempt(attempts: ProviderDispatchAttempt[], attempt: ProviderDispatchAttempt): void {
  const index = attempts.findIndex((candidate) => candidate.attemptId === attempt.attemptId);
  if (index >= 0) attempts[index] = attempt;
}
