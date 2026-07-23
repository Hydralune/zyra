import type {
  ConnectionPhaseValue,
  JsonObject,
  TransportKindValue,
} from "./contracts.ts"

export const IngressErrorCode = {
  DISABLED: "event_ingress_disabled",
  CLOSED: "event_ingress_closed",
  INVALID_CONFIGURATION: "event_ingress_invalid_configuration",
  INVALID_CAPABILITIES: "event_ingress_invalid_capabilities",
  INVALID_PAGE: "event_ingress_invalid_page",
  INVALID_FRAME: "event_ingress_invalid_frame",
  INVALID_EVENT: "event_ingress_invalid_event",
  SCHEMA_UNSUPPORTED: "event_ingress_schema_unsupported",
  CROSS_TASK: "event_ingress_cross_task",
  STALE_GENERATION: "event_ingress_stale_generation",
  CURSOR_MISSING: "event_ingress_cursor_missing",
  CURSOR_STALE: "event_ingress_cursor_stale",
  CURSOR_REGRESSION: "event_ingress_cursor_regression",
  CURSOR_AHEAD: "event_ingress_cursor_ahead",
  CURSOR_SCOPE: "event_ingress_cursor_scope",
  SNAPSHOT_FAILED: "event_ingress_snapshot_failed",
  SNAPSHOT_STALE: "event_ingress_snapshot_stale",
  SNAPSHOT_INCOMPLETE: "event_ingress_snapshot_incomplete",
  GAP_DETECTED: "event_ingress_gap_detected",
  GAP_OVERFLOW: "event_ingress_gap_overflow",
  GAP_UNRECOVERABLE: "event_ingress_gap_unrecoverable",
  DUPLICATE_CONFLICT: "event_ingress_duplicate_conflict",
  BUFFER_OVERFLOW: "event_ingress_buffer_overflow",
  EVENT_TOO_LARGE: "event_ingress_event_too_large",
  ORPHAN_OVERFLOW: "event_ingress_orphan_overflow",
  PARTIAL_CONFLICT: "event_ingress_partial_conflict",
  TOMBSTONE_CONFLICT: "event_ingress_tombstone_conflict",
  TRANSPORT_UNAVAILABLE: "event_ingress_transport_unavailable",
  TRANSPORT_DISCONNECTED: "event_ingress_transport_disconnected",
  TRANSPORT_PROTOCOL: "event_ingress_transport_protocol",
  TRANSPORT_TIMEOUT: "event_ingress_transport_timeout",
  HEARTBEAT_TIMEOUT: "event_ingress_heartbeat_timeout",
  RECONNECT_EXHAUSTED: "event_ingress_reconnect_exhausted",
  RESYNC_EXHAUSTED: "event_ingress_resync_exhausted",
  SUBSCRIBER_FAILED: "event_ingress_subscriber_failed",
  CANCELLED: "event_ingress_cancelled",
  INTERNAL_INVARIANT: "event_ingress_internal_invariant",
} as const

export type IngressErrorCodeValue =
  (typeof IngressErrorCode)[keyof typeof IngressErrorCode]

export interface IngressErrorContext {
  taskId?: string
  generation?: number
  sequence?: number
  previousSequence?: number
  eventId?: string
  transport?: TransportKindValue
  phase?: ConnectionPhaseValue
  operation?: string
  retryAfterMs?: number
  details?: JsonObject
}

export class EventIngressError extends Error {
  readonly code: IngressErrorCodeValue | string
  readonly retryable: boolean
  readonly resyncRequired: boolean
  readonly context: Readonly<IngressErrorContext>
  readonly observedAtMs: number
  readonly cause?: unknown

  constructor(
    code: IngressErrorCodeValue | string,
    message: string,
    options: {
      retryable?: boolean
      resyncRequired?: boolean
      context?: IngressErrorContext
      cause?: unknown
      observedAtMs?: number
    } = {},
  ) {
    super(message)
    this.name = "EventIngressError"
    this.code = code
    this.retryable = options.retryable ?? false
    this.resyncRequired = options.resyncRequired ?? false
    this.context = Object.freeze({
      ...(options.context ?? {}),
      details: options.context?.details
        ? Object.freeze({ ...options.context.details })
        : undefined,
    })
    this.observedAtMs = options.observedAtMs ?? Date.now()
    this.cause = options.cause
  }

  withContext(context: IngressErrorContext): EventIngressError {
    return new EventIngressError(this.code, this.message, {
      retryable: this.retryable,
      resyncRequired: this.resyncRequired,
      observedAtMs: this.observedAtMs,
      cause: this.cause,
      context: {
        ...this.context,
        ...context,
        details: {
          ...(this.context.details ?? {}),
          ...(context.details ?? {}),
        },
      },
    })
  }

  toJSON(): Record<string, unknown> {
    return {
      name: this.name,
      code: this.code,
      message: this.message,
      retryable: this.retryable,
      resyncRequired: this.resyncRequired,
      context: this.context,
      observedAtMs: this.observedAtMs,
    }
  }
}

function unknownRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {}
  return value as Record<string, unknown>
}

function messageOf(value: unknown): string {
  if (value instanceof Error && value.message) return value.message
  const record = unknownRecord(value)
  const message = record.message ?? record.error_description ?? record.detail
  if (typeof message === "string" && message.trim()) return message.trim()
  if (typeof value === "string" && value.trim()) return value.trim()
  return "The event ingress operation failed."
}

function codeOf(value: unknown): string {
  if (value instanceof EventIngressError) return value.code
  const record = unknownRecord(value)
  const code = record.code ?? record.error
  return typeof code === "string" && code.trim()
    ? code.trim()
    : IngressErrorCode.TRANSPORT_DISCONNECTED
}

function retryableOf(value: unknown): boolean {
  if (value instanceof EventIngressError) return value.retryable
  const record = unknownRecord(value)
  if (typeof record.retryable === "boolean") return record.retryable
  const status = Number(record.status ?? record.statusCode)
  if ([408, 425, 429, 500, 502, 503, 504].includes(status)) return true
  const code = codeOf(value).toLowerCase()
  return [
    "timeout",
    "disconnect",
    "network",
    "temporar",
    "unavailable",
    "overload",
    "reset",
    "closed",
    "econn",
  ].some((marker) => code.includes(marker) || messageOf(value).toLowerCase().includes(marker))
}

function resyncOf(value: unknown): boolean {
  if (value instanceof EventIngressError) return value.resyncRequired
  const record = unknownRecord(value)
  if (typeof record.resyncRequired === "boolean") return record.resyncRequired
  if (typeof record.resync_required === "boolean") return record.resync_required
  const code = codeOf(value).toLowerCase()
  return [
    "cursor",
    "schema",
    "sequence",
    "gap",
    "stale",
    "conflict",
    "snapshot",
  ].some((marker) => code.includes(marker))
}

export function classifyIngressError(
  value: unknown,
  context: IngressErrorContext = {},
): EventIngressError {
  if (value instanceof EventIngressError) {
    return Object.keys(context).length ? value.withContext(context) : value
  }
  const record = unknownRecord(value)
  const name = value instanceof Error ? value.name : String(record.name ?? "")
  if (
    name === "AbortError" ||
    name === "TimeoutError" ||
    context.details?.aborted === true
  ) {
    return new EventIngressError(
      name === "TimeoutError"
        ? IngressErrorCode.TRANSPORT_TIMEOUT
        : IngressErrorCode.CANCELLED,
      messageOf(value),
      {
        retryable: name === "TimeoutError",
        context,
        cause: value,
      },
    )
  }
  const code = codeOf(value)
  return new EventIngressError(code, messageOf(value), {
    retryable: retryableOf(value),
    resyncRequired: resyncOf(value),
    context,
    cause: value,
  })
}

export function invariant(
  condition: unknown,
  message: string,
  context: IngressErrorContext = {},
): asserts condition {
  if (condition) return
  throw new EventIngressError(IngressErrorCode.INTERNAL_INVARIANT, message, {
    context,
    resyncRequired: true,
  })
}

export function schemaError(
  actual: unknown,
  expected: string | readonly string[],
  context: IngressErrorContext = {},
): EventIngressError {
  const supported = Array.isArray(expected) ? [...expected] : [expected]
  return new EventIngressError(
    IngressErrorCode.SCHEMA_UNSUPPORTED,
    `Unsupported event ingress schema ${String(actual || "[missing]")}.`,
    {
      resyncRequired: true,
      context: {
        ...context,
        details: {
          ...(context.details ?? {}),
          actual: String(actual ?? ""),
          supported,
        },
      },
    },
  )
}

export function crossTaskError(
  expectedTaskId: string,
  actualTaskId: string,
  context: IngressErrorContext = {},
): EventIngressError {
  return new EventIngressError(
    IngressErrorCode.CROSS_TASK,
    "Event ingress received a frame for another task.",
    {
      resyncRequired: true,
      context: {
        ...context,
        taskId: expectedTaskId,
        details: {
          ...(context.details ?? {}),
          expectedTaskId,
          actualTaskId,
        },
      },
    },
  )
}

export function staleGenerationError(
  expected: number,
  actual: number,
  context: IngressErrorContext = {},
): EventIngressError {
  return new EventIngressError(
    IngressErrorCode.STALE_GENERATION,
    `Frame generation ${actual} does not match active generation ${expected}.`,
    {
      context: {
        ...context,
        generation: actual,
        details: {
          ...(context.details ?? {}),
          expectedGeneration: expected,
          actualGeneration: actual,
        },
      },
    },
  )
}

export function bufferOverflowError(
  message: string,
  context: IngressErrorContext = {},
): EventIngressError {
  return new EventIngressError(IngressErrorCode.BUFFER_OVERFLOW, message, {
    retryable: true,
    resyncRequired: true,
    context,
  })
}

export function subscriberError(
  value: unknown,
  context: IngressErrorContext = {},
): EventIngressError {
  return new EventIngressError(
    IngressErrorCode.SUBSCRIBER_FAILED,
    messageOf(value),
    {
      retryable: false,
      resyncRequired: false,
      context,
      cause: value,
    },
  )
}

export function isCancellation(value: unknown): boolean {
  if (value instanceof EventIngressError) return value.code === IngressErrorCode.CANCELLED
  if (value instanceof DOMException) return value.name === "AbortError"
  return value instanceof Error && ["AbortError", "TimeoutError"].includes(value.name)
}

export function isRetryableIngressError(value: unknown): boolean {
  return classifyIngressError(value).retryable
}

export function needsIngressResync(value: unknown): boolean {
  return classifyIngressError(value).resyncRequired
}
