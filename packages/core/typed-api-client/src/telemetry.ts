import { MAX_ATTEMPT_HISTORY, clampInteger } from "./constants.ts"
import type { SerializedZyraError } from "./errors.ts"
import { serializeError } from "./errors.ts"
import type { IdentityBinding } from "./identifiers.ts"

export type TransportPhase =
  | "prepared"
  | "queued"
  | "started"
  | "headers"
  | "body"
  | "normalized"
  | "retry"
  | "completed"
  | "failed"
  | "cancelled"

export interface TransportTrace {
  sequence: number
  phase: TransportPhase
  at: number
  operation: string
  requestId: string
  method: string
  path: string
  attempt: number
  binding: IdentityBinding
  status?: number
  elapsedMs?: number
  bytes?: number
  retryDelayMs?: number
  receiptId?: string
  replayed?: boolean
  error?: SerializedZyraError
  metadata: Record<string, unknown>
}

export interface TraceInput {
  phase: TransportPhase
  operation: string
  requestId: string
  method: string
  path: string
  attempt?: number
  binding?: IdentityBinding
  status?: number
  elapsedMs?: number
  bytes?: number
  retryDelayMs?: number
  receiptId?: string
  replayed?: boolean
  error?: unknown
  metadata?: Record<string, unknown>
}

export type TraceListener = (trace: TransportTrace) => void

function cloneTrace(trace: TransportTrace): TransportTrace {
  return {
    ...trace,
    binding: { ...trace.binding },
    metadata: { ...trace.metadata },
    error: trace.error
      ? {
          ...trace.error,
          details: { ...trace.error.details },
          context: { ...trace.error.context },
          cause: trace.error.cause ? { ...trace.error.cause } : undefined,
        }
      : undefined,
  }
}

export class TransportTelemetry {
  readonly #maximum: number
  readonly #traces: TransportTrace[] = []
  readonly #listeners = new Set<TraceListener>()
  readonly #now: () => number
  #sequence = 0

  constructor(options: { maximum?: number; now?: () => number } = {}) {
    this.#maximum = clampInteger(options.maximum ?? MAX_ATTEMPT_HISTORY * 8, 1, 100_000, "trace maximum")
    this.#now = options.now ?? Date.now
  }

  record(input: TraceInput): TransportTrace {
    const trace: TransportTrace = {
      sequence: ++this.#sequence,
      phase: input.phase,
      at: this.#now(),
      operation: input.operation,
      requestId: input.requestId,
      method: input.method,
      path: input.path,
      attempt: input.attempt ?? 1,
      binding: { ...(input.binding ?? {}) },
      status: input.status,
      elapsedMs: input.elapsedMs,
      bytes: input.bytes,
      retryDelayMs: input.retryDelayMs,
      receiptId: input.receiptId,
      replayed: input.replayed,
      error: input.error
        ? serializeError(input.error, {
            operation: input.operation,
            method: input.method,
            requestId: input.requestId,
            attempt: input.attempt,
            status: input.status,
          })
        : undefined,
      metadata: { ...(input.metadata ?? {}) },
    }
    this.#traces.push(trace)
    while (this.#traces.length > this.#maximum) this.#traces.shift()
    for (const listener of this.#listeners) {
      try {
        listener(cloneTrace(trace))
      } catch {
        // Telemetry listeners may not change request semantics.
      }
    }
    return cloneTrace(trace)
  }

  listen(listener: TraceListener): () => void {
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  byRequest(requestId: string): TransportTrace[] {
    return this.#traces.filter((trace) => trace.requestId === requestId).map(cloneTrace)
  }

  byOperation(operation: string): TransportTrace[] {
    return this.#traces.filter((trace) => trace.operation === operation).map(cloneTrace)
  }

  failures(): TransportTrace[] {
    return this.#traces.filter((trace) => trace.phase === "failed" || trace.phase === "cancelled").map(cloneTrace)
  }

  latest(): TransportTrace | undefined {
    const trace = this.#traces[this.#traces.length - 1]
    return trace ? cloneTrace(trace) : undefined
  }

  snapshot(): TransportTrace[] {
    return this.#traces.map(cloneTrace)
  }

  clear(): void {
    this.#traces.length = 0
  }

  summary(): {
    total: number
    completed: number
    failed: number
    cancelled: number
    retries: number
    averageElapsedMs: number
    operations: Record<string, number>
  } {
    const completed = this.#traces.filter((trace) => trace.phase === "completed")
    const durations = completed.map((trace) => trace.elapsedMs ?? 0)
    const operations: Record<string, number> = {}
    for (const trace of completed) operations[trace.operation] = (operations[trace.operation] ?? 0) + 1
    return {
      total: this.#traces.length,
      completed: completed.length,
      failed: this.#traces.filter((trace) => trace.phase === "failed").length,
      cancelled: this.#traces.filter((trace) => trace.phase === "cancelled").length,
      retries: this.#traces.filter((trace) => trace.phase === "retry").length,
      averageElapsedMs: durations.length
        ? durations.reduce((total, value) => total + value, 0) / durations.length
        : 0,
      operations,
    }
  }
}

export class InflightTracker {
  readonly #requests = new Map<
    string,
    {
      operation: string
      startedAt: number
      attempt: number
      cancel?: (reason?: unknown) => void
    }
  >()

  start(
    requestId: string,
    operation: string,
    attempt: number,
    cancel?: (reason?: unknown) => void,
  ): void {
    if (this.#requests.has(requestId)) throw new TypeError(`Request is already in flight: ${requestId}`)
    this.#requests.set(requestId, { operation, startedAt: Date.now(), attempt, cancel })
  }

  finish(requestId: string): boolean {
    return this.#requests.delete(requestId)
  }

  cancel(requestId: string, reason?: unknown): boolean {
    const entry = this.#requests.get(requestId)
    if (!entry) return false
    entry.cancel?.(reason)
    return true
  }

  cancelAll(reason?: unknown): number {
    let count = 0
    for (const [requestId, entry] of this.#requests) {
      entry.cancel?.(reason)
      this.#requests.delete(requestId)
      count += 1
    }
    return count
  }

  snapshot(): Array<{
    requestId: string
    operation: string
    startedAt: number
    elapsedMs: number
    attempt: number
  }> {
    const now = Date.now()
    return [...this.#requests.entries()]
      .map(([requestId, value]) => ({
        requestId,
        operation: value.operation,
        startedAt: value.startedAt,
        elapsedMs: Math.max(0, now - value.startedAt),
        attempt: value.attempt,
      }))
      .sort((left, right) => left.startedAt - right.startedAt)
  }

  get size(): number {
    return this.#requests.size
  }
}
