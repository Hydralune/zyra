import { ProviderControlPlaneError, type ProviderFailureKind, type RecoveryIntent } from "../errors.ts";

export interface ErrorClassificationInput {
  readonly status: number | null;
  readonly headers: Headers | Readonly<Record<string, string>>;
  readonly body: string;
  readonly providerId: string;
  readonly modelId: string;
  readonly routeId: string;
  readonly credentialId: string;
  readonly bytesSent: number;
  readonly bytesReceived: number;
  readonly outputObserved: boolean;
}

export function classifyProviderResponse(input: ErrorClassificationInput): ProviderControlPlaneError {
  const text = input.body.toLowerCase();
  const status = input.status;
  let kind: ProviderFailureKind = "unknown_provider_failure";
  let recoveryIntent: RecoveryIntent = "surface_to_operator";
  let retryable = false;

  if (input.outputObserved) {
    kind = "partial_response_observed";
    recoveryIntent = "reconcile_partial_response";
  } else if (status === 401 || /invalid.+(api.?key|token)|authentication/.test(text)) {
    kind = "authentication_failed";
    recoveryIntent = "rotate_credential";
    retryable = true;
  } else if (status === 403) {
    kind = "authorization_failed";
  } else if (status === 429 && /usage|quota|credit|billing/.test(text)) {
    kind = "usage_limited";
    recoveryIntent = "rotate_credential";
    retryable = true;
  } else if (status === 429) {
    kind = "rate_limited";
    recoveryIntent = "retry_same_route";
    retryable = true;
  } else if (/context.+(length|window)|too many tokens|maximum context|prompt is too long/.test(text)) {
    kind = "context_overflow";
    recoveryIntent = "compact_context";
  } else if (status === 413 || /request.+too large|payload.+too large/.test(text)) {
    kind = "request_too_large";
    recoveryIntent = "reduce_request";
  } else if (status !== null && [408, 425, 500, 502, 503, 504].includes(status)) {
    kind = status === 408 || status === 504 ? "provider_timeout" : "provider_unavailable";
    recoveryIntent = "change_provider_route";
    retryable = true;
  } else if (status !== null && status >= 400 && status < 500) {
    kind = "invalid_request";
  } else if (status !== null && status >= 500) {
    kind = "provider_unavailable";
    recoveryIntent = "change_provider_route";
    retryable = true;
  }

  if (input.outputObserved) retryable = false;
  return new ProviderControlPlaneError({
    layer: "transport",
    kind,
    message: safeErrorMessage(input.body, status),
    retryable,
    recoveryIntent,
    httpStatus: status,
    providerId: input.providerId,
    modelId: input.modelId,
    routeId: input.routeId,
    credentialId: input.credentialId,
    bytesSent: input.bytesSent,
    bytesReceived: input.bytesReceived,
    outputObserved: input.outputObserved,
    retryAfterMilliseconds: retryAfterMilliseconds(input.headers),
  });
}

export function classifyTransportException(
  error: unknown,
  context: Omit<ErrorClassificationInput, "status" | "headers" | "body">,
): ProviderControlPlaneError {
  const message = error instanceof Error ? error.message : String(error);
  const timeout = /timeout|timed out|headers timeout/i.test(message);
  const aborted = error instanceof DOMException && error.name === "AbortError";
  return new ProviderControlPlaneError({
    layer: "transport",
    kind: aborted ? "request_aborted" : timeout ? "provider_timeout" : "provider_unavailable",
    message,
    retryable: !context.outputObserved && !aborted,
    recoveryIntent: context.outputObserved ? "reconcile_partial_response" : aborted ? "none" : "change_provider_route",
    providerId: context.providerId,
    modelId: context.modelId,
    routeId: context.routeId,
    credentialId: context.credentialId,
    bytesSent: context.bytesSent,
    bytesReceived: context.bytesReceived,
    outputObserved: context.outputObserved,
  }, { cause: error });
}

function retryAfterMilliseconds(headers: Headers | Readonly<Record<string, string>>): number | null {
  const raw = headers instanceof Headers ? headers.get("retry-after") : Object.entries(headers).find(([name]) => name.toLowerCase() === "retry-after")?.[1] ?? null;
  if (!raw) return null;
  const seconds = Number(raw);
  if (Number.isFinite(seconds) && seconds >= 0) return Math.round(seconds * 1_000);
  const timestamp = Date.parse(raw);
  return Number.isFinite(timestamp) ? Math.max(0, timestamp - Date.now()) : null;
}

function safeErrorMessage(body: string, status: number | null): string {
  const compact = body.replace(/\s+/g, " ").trim().slice(0, 1_000);
  const redacted = compact.replace(/(?:sk|key|token|bearer)[-_a-z0-9]{8,}/gi, "[REDACTED]");
  return redacted || `provider request failed${status === null ? "" : ` with HTTP ${status}`}`;
}
