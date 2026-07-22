import type { ProviderDispatchAttempt, ProviderRouteLease } from "./contracts.ts";
import { ProviderControlPlaneError } from "./errors.ts";

export type ModelFallbackAction =
  | "retry_same_route"
  | "change_provider_route"
  | "rotate_credential"
  | "compact_required"
  | "reduce_request"
  | "reconcile_partial_output"
  | "cancel"
  | "stop";

export interface ModelFallbackDecision {
  readonly action: ModelFallbackAction;
  readonly retry: boolean;
  readonly changeRoute: boolean;
  readonly rotateCredential: boolean;
  readonly outputObserved: boolean;
  readonly delayMilliseconds: number;
  readonly reason: string;
  readonly attempt: number;
  readonly maximumAttempts: number;
  readonly currentRouteId: string;
  readonly failureKind: string;
  readonly evidence: Readonly<Record<string, unknown>>;
}

export interface ModelFallbackPolicyOptions {
  readonly forceRouteChangeForRateLimit?: boolean;
  readonly forceRouteChangeForProviderUnavailable?: boolean;
  readonly retryNetworkWithoutStatus?: boolean;
  readonly retryAuthenticationWithSiblingCredential?: boolean;
}

export class ModelFallbackPolicy {
  private readonly routeOnRateLimit: boolean;
  private readonly routeOnUnavailable: boolean;
  private readonly retryNetwork: boolean;
  private readonly rotateAuthentication: boolean;

  constructor(options: ModelFallbackPolicyOptions = {}) {
    this.routeOnRateLimit = options.forceRouteChangeForRateLimit ?? true;
    this.routeOnUnavailable = options.forceRouteChangeForProviderUnavailable ?? true;
    this.retryNetwork = options.retryNetworkWithoutStatus ?? true;
    this.rotateAuthentication = options.retryAuthenticationWithSiblingCredential ?? true;
  }

  decide(
    error: ProviderControlPlaneError,
    lease: ProviderRouteLease,
    attempt: number,
    history: readonly ProviderDispatchAttempt[] = [],
  ): ModelFallbackDecision {
    const outputObserved = error.outputObserved || history.some((item) => item.outputObserved);
    const evidence = {
      layer: error.layer,
      httpStatus: error.httpStatus,
      retryable: error.retryable,
      recoveryIntent: error.recoveryIntent,
      bytesSent: error.bytesSent,
      bytesReceived: error.bytesReceived,
      outputObserved,
      attemptedRoutes: history.map((item) => item.routeId),
    };
    if (outputObserved || error.kind === "partial_response_observed") {
      return this.result("reconcile_partial_output", false, false, false, error, lease, attempt,
        "observable provider output forbids automatic replay", evidence);
    }
    if (error.kind === "request_aborted") {
      return this.result("cancel", false, false, false, error, lease, attempt,
        "provider request was cancelled", evidence);
    }
    if (error.kind === "context_overflow") {
      return this.result("compact_required", false, false, false, error, lease, attempt,
        "context overflow requires canonical compact", evidence);
    }
    if (error.kind === "request_too_large") {
      return this.result("reduce_request", false, false, false, error, lease, attempt,
        "request must be reduced before retry", evidence);
    }
    if (error.layer === "route" && error.recoveryIntent === "retry_same_route" && error.retryable) {
      if (attempt >= lease.retryPolicy.maximumAttempts) {
        return this.result("stop", false, false, false, error, lease, attempt,
          "same-route retry exhausted the bounded attempt budget", evidence);
      }
      return this.result("retry_same_route", true, false, false, error, lease, attempt,
        "provider requested a bounded same-route retry", evidence);
    }
    if (["authentication_failed", "usage_limited", "credential_blocked", "credential_expired"].includes(error.kind)) {
      if (attempt >= lease.retryPolicy.maximumAttempts) {
        return this.result("stop", false, false, false, error, lease, attempt,
          "credential rotation exhausted the bounded attempt budget", evidence);
      }
      const rotate = this.rotateAuthentication && lease.retryPolicy.rotateCredentialOnAuthenticationFailure;
      return this.result(rotate ? "rotate_credential" : "stop", rotate, rotate, rotate,
        error, lease, attempt, rotate ? "rotate sibling credential on pinned catalog" : "credential rotation disabled", evidence);
    }
    if (attempt >= lease.retryPolicy.maximumAttempts || !error.retryable) {
      return this.result("stop", false, false, false, error, lease, attempt,
        "provider retry exhausted or failure is non-retryable", evidence);
    }
    if (error.kind === "rate_limited") {
      const change = this.routeOnRateLimit && lease.retryPolicy.rotateRouteOnProviderUnavailable;
      return this.result(change ? "change_provider_route" : "retry_same_route", true, change, false,
        error, lease, attempt, change ? "rate limit changes route" : "rate limit retries same route", evidence);
    }
    if (["provider_unavailable", "provider_timeout", "stream_timeout"].includes(error.kind)) {
      const change = this.routeOnUnavailable && lease.retryPolicy.rotateRouteOnProviderUnavailable;
      return this.result(change ? "change_provider_route" : "retry_same_route", true, change, false,
        error, lease, attempt, change ? "availability failure changes route" : "availability failure retries route", evidence);
    }
    const statusRetry = error.httpStatus !== null && lease.retryPolicy.retryStatuses.includes(error.httpStatus);
    const networkRetry = error.httpStatus === null && this.retryNetwork;
    const retry = error.retryable && (statusRetry || networkRetry);
    return this.result(retry ? "retry_same_route" : "stop", retry, false, false,
      error, lease, attempt, retry ? "failure remains within retry policy" : "failure outside retry policy", evidence);
  }

  private result(
    action: ModelFallbackAction,
    retry: boolean,
    changeRoute: boolean,
    rotateCredential: boolean,
    error: ProviderControlPlaneError,
    lease: ProviderRouteLease,
    attempt: number,
    reason: string,
    evidence: Readonly<Record<string, unknown>>,
  ): ModelFallbackDecision {
    return {
      action,
      retry,
      changeRoute,
      rotateCredential,
      outputObserved: error.outputObserved,
      delayMilliseconds: retry ? this.delay(lease, attempt, error.retryAfterMilliseconds) : 0,
      reason,
      attempt,
      maximumAttempts: lease.retryPolicy.maximumAttempts,
      currentRouteId: lease.routeId,
      failureKind: error.kind,
      evidence,
    };
  }

  private delay(lease: ProviderRouteLease, attempt: number, retryAfter: number | null): number {
    if (retryAfter !== null) return Math.min(Math.max(0, retryAfter), lease.retryPolicy.maximumDelayMilliseconds);
    return Math.min(
      lease.retryPolicy.maximumDelayMilliseconds,
      lease.retryPolicy.baseDelayMilliseconds * 2 ** Math.max(0, attempt - 1),
    );
  }
}
