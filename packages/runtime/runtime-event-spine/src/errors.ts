export const EventSpineErrorCode = {
  INVALID_ARGUMENT: "invalid_argument",
  INVALID_ENVELOPE: "invalid_envelope",
  UNKNOWN_EVENT_KIND: "unknown_event_kind",
  EVENT_TOO_LARGE: "event_too_large",
  INLINE_PAYLOAD_TOO_LARGE: "inline_payload_too_large",
  SUMMARY_TOO_LARGE: "summary_too_large",
  TOO_MANY_REFS: "too_many_refs",
  FORBIDDEN_INLINE_CONTENT: "forbidden_inline_content",
  AGGREGATE_OWNER_MISMATCH: "aggregate_owner_mismatch",
  AGGREGATE_SEQUENCE_CONFLICT: "aggregate_sequence_conflict",
  EVENT_ID_CONFLICT: "event_id_conflict",
  IDEMPOTENCY_CONFLICT: "idempotency_conflict",
  REPLAY_DIVERGENCE: "replay_divergence",
  CAUSATION_MISSING: "causation_missing",
  CORRELATION_MISMATCH: "correlation_mismatch",
  PROJECTOR_GAP: "projector_gap",
  PROJECTOR_DIVERGENCE: "projector_divergence",
  TOOL_PAIR_VIOLATION: "tool_pair_violation",
  STREAM_LIFECYCLE_VIOLATION: "stream_lifecycle_violation",
  ROUTE_NOT_FOUND: "route_not_found",
  BROADCAST_FORBIDDEN: "broadcast_forbidden",
  SUBSCRIBER_NOT_FOUND: "subscriber_not_found",
  SUBSCRIBER_BACKPRESSURE: "subscriber_backpressure",
  DELIVERY_NOT_FOUND: "delivery_not_found",
  DELIVERY_LEASE_MISMATCH: "delivery_lease_mismatch",
  DELIVERY_TERMINAL: "delivery_terminal",
  CURSOR_INVALID: "cursor_invalid",
  RPC_PROTOCOL_ERROR: "rpc_protocol_error",
  RPC_COMMAND_UNKNOWN: "rpc_command_unknown",
  STORAGE_ERROR: "storage_error",
  RUNTIME_CLOSED: "runtime_closed",
} as const;

export type EventSpineErrorCodeValue = (typeof EventSpineErrorCode)[keyof typeof EventSpineErrorCode];

export interface EventSpineErrorOptions {
  code: EventSpineErrorCodeValue;
  message: string;
  retryable?: boolean;
  terminal?: boolean;
  details?: Record<string, unknown>;
  cause?: unknown;
}

export class EventSpineError extends Error {
  readonly code: EventSpineErrorCodeValue;
  readonly retryable: boolean;
  readonly terminal: boolean;
  readonly details: Readonly<Record<string, unknown>>;
  override readonly cause: unknown;

  constructor(options: EventSpineErrorOptions) {
    super(options.message);
    this.name = "EventSpineError";
    this.code = options.code;
    this.retryable = options.retryable ?? false;
    this.terminal = options.terminal ?? !this.retryable;
    this.details = Object.freeze({ ...(options.details ?? {}) });
    this.cause = options.cause;
  }

  toJSON(): Record<string, unknown> {
    return {
      name: this.name,
      code: this.code,
      message: this.message,
      retryable: this.retryable,
      terminal: this.terminal,
      details: this.details,
    };
  }
}

export class EnvelopeValidationError extends EventSpineError {
  constructor(message: string, details: Record<string, unknown> = {}) {
    super({ code: EventSpineErrorCode.INVALID_ENVELOPE, message, details });
    this.name = "EnvelopeValidationError";
  }
}

export class PayloadBudgetError extends EventSpineError {
  constructor(
    code:
      | typeof EventSpineErrorCode.EVENT_TOO_LARGE
      | typeof EventSpineErrorCode.INLINE_PAYLOAD_TOO_LARGE
      | typeof EventSpineErrorCode.SUMMARY_TOO_LARGE
      | typeof EventSpineErrorCode.TOO_MANY_REFS
      | typeof EventSpineErrorCode.FORBIDDEN_INLINE_CONTENT,
    message: string,
    details: Record<string, unknown> = {},
  ) {
    super({ code, message, details });
    this.name = "PayloadBudgetError";
  }
}

export class AggregateConflictError extends EventSpineError {
  constructor(
    code:
      | typeof EventSpineErrorCode.AGGREGATE_OWNER_MISMATCH
      | typeof EventSpineErrorCode.AGGREGATE_SEQUENCE_CONFLICT
      | typeof EventSpineErrorCode.EVENT_ID_CONFLICT
      | typeof EventSpineErrorCode.IDEMPOTENCY_CONFLICT
      | typeof EventSpineErrorCode.REPLAY_DIVERGENCE,
    message: string,
    details: Record<string, unknown> = {},
  ) {
    super({ code, message, details, retryable: code === EventSpineErrorCode.AGGREGATE_SEQUENCE_CONFLICT });
    this.name = "AggregateConflictError";
  }
}

export class ProjectorError extends EventSpineError {
  constructor(
    code:
      | typeof EventSpineErrorCode.PROJECTOR_GAP
      | typeof EventSpineErrorCode.PROJECTOR_DIVERGENCE
      | typeof EventSpineErrorCode.TOOL_PAIR_VIOLATION
      | typeof EventSpineErrorCode.STREAM_LIFECYCLE_VIOLATION,
    message: string,
    details: Record<string, unknown> = {},
  ) {
    super({ code, message, details });
    this.name = "ProjectorError";
  }
}

export class RoutingError extends EventSpineError {
  constructor(
    code:
      | typeof EventSpineErrorCode.ROUTE_NOT_FOUND
      | typeof EventSpineErrorCode.BROADCAST_FORBIDDEN
      | typeof EventSpineErrorCode.SUBSCRIBER_BACKPRESSURE,
    message: string,
    details: Record<string, unknown> = {},
  ) {
    super({ code, message, details, retryable: code === EventSpineErrorCode.SUBSCRIBER_BACKPRESSURE });
    this.name = "RoutingError";
  }
}

export class DeliveryError extends EventSpineError {
  constructor(
    code:
      | typeof EventSpineErrorCode.SUBSCRIBER_NOT_FOUND
      | typeof EventSpineErrorCode.DELIVERY_NOT_FOUND
      | typeof EventSpineErrorCode.DELIVERY_LEASE_MISMATCH
      | typeof EventSpineErrorCode.DELIVERY_TERMINAL,
    message: string,
    details: Record<string, unknown> = {},
    retryable = false,
  ) {
    super({ code, message, details, retryable });
    this.name = "DeliveryError";
  }
}

export function asEventSpineError(error: unknown, context = "runtime event spine operation failed"): EventSpineError {
  if (error instanceof EventSpineError) return error;
  if (error instanceof Error) {
    return new EventSpineError({
      code: EventSpineErrorCode.STORAGE_ERROR,
      message: `${context}: ${error.message}`,
      cause: error,
      details: { error_type: error.name },
    });
  }
  return new EventSpineError({
    code: EventSpineErrorCode.STORAGE_ERROR,
    message: context,
    cause: error,
    details: { error: String(error) },
  });
}

export function invariant(
  condition: unknown,
  code: EventSpineErrorCodeValue,
  message: string,
  details: Record<string, unknown> = {},
): asserts condition {
  if (condition) return;
  throw new EventSpineError({ code, message, details });
}
