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

  constructor(
    store: ProviderControlPlaneStore,
    routes: ProviderRoutePlanner,
    credentials: CredentialManager,
    _secrets: SecretResolver,
    options: ProviderTransportOptions = {},
  ) {
    this.store = store;
    this.routes = routes;
    this.credentials = credentials;
    this.fetchImpl = options.fetch ?? fetch;
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.userAgent = options.userAgent ?? "Zyra-ProviderControlPlane/1";
  }

  async dispatch(request: ProviderDispatchRequest, signal?: AbortSignal): Promise<ProviderDispatchResult> {
    validateDispatchRequest(request);
    let lease = this.routes.require(request.routeId);
    assertDispatchIdentity(request, lease);
    const allAttempts: ProviderDispatchAttempt[] = [];
    let lastError: ProviderControlPlaneError | null = null;

    for (let attemptNumber = 1; attemptNumber <= lease.retryPolicy.maximumAttempts; attemptNumber += 1) {
      let result: ProviderDispatchResult | null = null;
      try {
        result = await this.dispatchOnce(request, lease, attemptNumber, allAttempts, signal);
        this.credentials.recordSuccess(lease.credentialId, lease.credentialVersion);
        return { ...result, attempts: deepClone(allAttempts) };
      } catch (error) {
        lastError = asProviderError(error);
        if (lastError.credentialId === lease.credentialId && lastError.kind !== "credential_version_conflict") {
          try { this.credentials.recordFailure(lease.credentialId, lease.credentialVersion, lastError.kind); } catch { /* preserve transport error */ }
        }
        if (!shouldRetry(lastError, lease, attemptNumber)) throw lastError;
        if (shouldChangeRoute(lastError, lease)) {
          lease = this.routes.failover(lease.routeId, lastError.kind);
        }
        const delay = retryDelay(lease, attemptNumber, lastError.retryAfterMilliseconds);
        await sleep(delay, signal);
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

    let responseBytes = 0;
    const frameState = new ProtocolFrameState(
      { ...request, routeId: lease.routeId },
      this.ids,
      () => this.clock.now(),
    );
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
      const resolved = await this.credentials.resolvePinned(lease.credentialId, lease.credentialVersion, combinedSignal);
      const headers = normalizeHeaders({
        ...lease.requestHeaders,
        ...protocolHeaders(lease),
        ...resolved.headers,
        "content-type": "application/json",
        "user-agent": this.userAgent,
        "idempotency-key": request.idempotencyKey,
      });
      bytesSent = preparedRequestBytes;
      attempt = { ...attempt, requestBytes: bytesSent };
      replaceAttempt(attempts, attempt);
      this.store.putAttempt(attempt);
      const response = await this.fetchImpl(routeUrl(lease), {
        method: "POST",
        headers,
        body,
        signal: combinedSignal,
      });
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
        frames.push(...decodedFrames);
      } else {
        for await (const event of readSse(response, {
          chunkTimeoutMilliseconds: request.chunkTimeoutMilliseconds,
          signal: combinedSignal,
        })) {
          responseBytes += Buffer.byteLength(event.data);
          frames.push(...decodeProviderEvent(lease, event.data, event.event, frameState));
        }
      }
      attempt = {
        ...attempt,
        completedAt: this.clock.now(),
        httpStatus: response.status,
        responseBytes,
        outputObserved: frameState.outputObserved,
        outcome: "succeeded",
        responseDigest: digestText(frames.map((frame) => `${frame.sequence}:${frame.kind}:${frame.text ?? ""}`).join("\n")),
        metadata: { ...attempt.metadata, requestHeaders: redactHeaders(headers) },
      };
      replaceAttempt(attempts, attempt);
      this.store.putAttempt(attempt);
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
            outputObserved: frameState.outputObserved,
          });
      attempt = {
        ...attempt,
        completedAt: this.clock.now(),
        httpStatus: classified.httpStatus,
        responseBytes: classified.bytesReceived || responseBytes,
        outputObserved: classified.outputObserved || frameState.outputObserved,
        outcome: classified.kind === "request_aborted" ? "aborted" : "failed",
        failureKind: classified.kind,
        recoveryIntent: classified.recoveryIntent,
        retryAfterMilliseconds: classified.retryAfterMilliseconds,
      };
      replaceAttempt(attempts, attempt);
      this.store.putAttempt(attempt);
      throw classified;
    } finally {
      clearTimeout(timeout);
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

function shouldRetry(error: ProviderControlPlaneError, lease: ProviderRouteLease, attempt: number): boolean {
  if (!error.retryable || error.outputObserved || attempt >= lease.retryPolicy.maximumAttempts) return false;
  if (error.kind === "authentication_failed" || error.kind === "usage_limited") return lease.retryPolicy.rotateCredentialOnAuthenticationFailure;
  return error.httpStatus === null || lease.retryPolicy.retryStatuses.includes(error.httpStatus);
}

function shouldChangeRoute(error: ProviderControlPlaneError, lease: ProviderRouteLease): boolean {
  if (error.kind === "authentication_failed" || error.kind === "usage_limited") return lease.retryPolicy.rotateCredentialOnAuthenticationFailure;
  return ["provider_unavailable", "provider_timeout", "stream_timeout"].includes(error.kind) && lease.retryPolicy.rotateRouteOnProviderUnavailable;
}

function retryDelay(lease: ProviderRouteLease, attempt: number, retryAfter: number | null): number {
  if (retryAfter !== null) return Math.min(retryAfter, lease.retryPolicy.maximumDelayMilliseconds);
  return Math.min(
    lease.retryPolicy.maximumDelayMilliseconds,
    lease.retryPolicy.baseDelayMilliseconds * 2 ** Math.max(0, attempt - 1),
  );
}

function replaceAttempt(attempts: ProviderDispatchAttempt[], attempt: ProviderDispatchAttempt): void {
  const index = attempts.findIndex((candidate) => candidate.attemptId === attempt.attemptId);
  if (index >= 0) attempts[index] = attempt;
}
