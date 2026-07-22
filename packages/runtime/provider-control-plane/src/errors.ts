import type { JsonRecord } from "./canonical.ts";
import { deepClone } from "./canonical.ts";

export type ProviderFailureLayer = "catalog" | "integration" | "credential" | "route" | "transport" | "protocol";

export type ProviderFailureKind =
  | "provider_control_plane_disabled"
  | "catalog_revision_conflict"
  | "provider_not_found"
  | "provider_disabled"
  | "model_not_found"
  | "model_disabled"
  | "integration_unavailable"
  | "credential_missing"
  | "credential_revoked"
  | "credential_expired"
  | "credential_blocked"
  | "credential_version_conflict"
  | "route_not_found"
  | "route_expired"
  | "route_revision_conflict"
  | "route_policy_rejected"
  | "invalid_request"
  | "authentication_failed"
  | "authorization_failed"
  | "rate_limited"
  | "usage_limited"
  | "provider_unavailable"
  | "provider_timeout"
  | "stream_timeout"
  | "context_overflow"
  | "request_too_large"
  | "response_protocol_error"
  | "partial_response_observed"
  | "request_aborted"
  | "unknown_provider_failure";

export type RecoveryIntent =
  | "none"
  | "refresh_credential"
  | "rotate_credential"
  | "change_provider_route"
  | "compact_context"
  | "reduce_request"
  | "retry_same_route"
  | "reconcile_partial_response"
  | "surface_to_operator";

export interface ProviderFailureShape {
  readonly layer: ProviderFailureLayer;
  readonly kind: ProviderFailureKind;
  readonly message: string;
  readonly retryable: boolean;
  readonly recoveryIntent: RecoveryIntent;
  readonly httpStatus: number | null;
  readonly providerId: string | null;
  readonly modelId: string | null;
  readonly routeId: string | null;
  readonly credentialId: string | null;
  readonly bytesSent: number;
  readonly bytesReceived: number;
  readonly outputObserved: boolean;
  readonly retryAfterMilliseconds: number | null;
  readonly detail: JsonRecord;
}

export class ProviderControlPlaneError extends Error implements ProviderFailureShape {
  readonly layer: ProviderFailureLayer;
  readonly kind: ProviderFailureKind;
  readonly retryable: boolean;
  readonly recoveryIntent: RecoveryIntent;
  readonly httpStatus: number | null;
  readonly providerId: string | null;
  readonly modelId: string | null;
  readonly routeId: string | null;
  readonly credentialId: string | null;
  readonly bytesSent: number;
  readonly bytesReceived: number;
  readonly outputObserved: boolean;
  readonly retryAfterMilliseconds: number | null;
  readonly detail: JsonRecord;

  constructor(input: Partial<Omit<ProviderFailureShape, "message">> & Pick<ProviderFailureShape, "layer" | "kind" | "message">, options?: ErrorOptions) {
    super(input.message, options);
    this.name = "ProviderControlPlaneError";
    this.layer = input.layer;
    this.kind = input.kind;
    this.retryable = input.retryable ?? false;
    this.recoveryIntent = input.recoveryIntent ?? "none";
    this.httpStatus = input.httpStatus ?? null;
    this.providerId = input.providerId ?? null;
    this.modelId = input.modelId ?? null;
    this.routeId = input.routeId ?? null;
    this.credentialId = input.credentialId ?? null;
    this.bytesSent = input.bytesSent ?? 0;
    this.bytesReceived = input.bytesReceived ?? 0;
    this.outputObserved = input.outputObserved ?? false;
    this.retryAfterMilliseconds = input.retryAfterMilliseconds ?? null;
    this.detail = deepClone(input.detail ?? {});
  }

  safe(): ProviderFailureShape {
    return {
      layer: this.layer,
      kind: this.kind,
      message: this.message,
      retryable: this.retryable,
      recoveryIntent: this.recoveryIntent,
      httpStatus: this.httpStatus,
      providerId: this.providerId,
      modelId: this.modelId,
      routeId: this.routeId,
      credentialId: this.credentialId,
      bytesSent: this.bytesSent,
      bytesReceived: this.bytesReceived,
      outputObserved: this.outputObserved,
      retryAfterMilliseconds: this.retryAfterMilliseconds,
      detail: deepClone(this.detail),
    };
  }
}

export function asProviderError(error: unknown): ProviderControlPlaneError {
  if (error instanceof ProviderControlPlaneError) return error;
  if (error instanceof DOMException && error.name === "AbortError") {
    return new ProviderControlPlaneError({
      layer: "transport",
      kind: "request_aborted",
      message: error.message || "provider request aborted",
      retryable: false,
      recoveryIntent: "none",
    }, { cause: error });
  }
  return new ProviderControlPlaneError({
    layer: "transport",
    kind: "unknown_provider_failure",
    message: error instanceof Error ? error.message : String(error),
    retryable: false,
    recoveryIntent: "surface_to_operator",
  }, { cause: error });
}
