import { DeadlineBudget, throwIfAborted } from "./cancellation.ts"
import {
  RequestCancelledError,
  RequestTimeoutError,
  TransportDisconnectedError,
  classifyUnknownError,
} from "./errors.ts"

export type CircuitState = "closed" | "open" | "half_open"

export interface CircuitSnapshot {
  state: CircuitState
  consecutiveFailures: number
  openedAt?: number
  retryAt?: number
  probeInFlight: boolean
  successes: number
  failures: number
}

export class TransportCircuitBreaker {
  readonly #failureThreshold: number
  readonly #cooldownMs: number
  readonly #now: () => number
  #state: CircuitState = "closed"
  #consecutiveFailures = 0
  #openedAt?: number
  #probeInFlight = false
  #successes = 0
  #failures = 0

  constructor(options: { failureThreshold?: number; cooldownMs?: number; now?: () => number } = {}) {
    const threshold = options.failureThreshold ?? 5
    const cooldownMs = options.cooldownMs ?? 5_000
    if (!Number.isInteger(threshold) || threshold < 1 || threshold > 100) {
      throw new TypeError("Circuit breaker failure threshold must be between 1 and 100")
    }
    if (!Number.isFinite(cooldownMs) || cooldownMs < 1 || cooldownMs > 10 * 60_000) {
      throw new TypeError("Circuit breaker cooldown must be between 1 ms and 10 minutes")
    }
    this.#failureThreshold = threshold
    this.#cooldownMs = Math.floor(cooldownMs)
    this.#now = options.now ?? Date.now
  }

  beforeRequest(operation: string): void {
    const now = this.#now()
    if (this.#state === "open") {
      const retryAt = (this.#openedAt ?? now) + this.#cooldownMs
      if (now < retryAt) {
        throw new TransportDisconnectedError("The Zyra API transport circuit is open.", {
          operation,
        }).withDetails({
          circuit_state: this.#state,
          retry_after_ms: retryAt - now,
          consecutive_failures: this.#consecutiveFailures,
        })
      }
      this.#state = "half_open"
      this.#probeInFlight = false
    }
    if (this.#state === "half_open") {
      if (this.#probeInFlight) {
        throw new TransportDisconnectedError("The Zyra API transport circuit probe is already in flight.", {
          operation,
        }).withDetails({
          circuit_state: this.#state,
          retry_after_ms: this.#cooldownMs,
        })
      }
      this.#probeInFlight = true
    }
  }

  success(): void {
    this.#successes += 1
    this.#consecutiveFailures = 0
    this.#openedAt = undefined
    this.#probeInFlight = false
    this.#state = "closed"
  }

  failure(error: unknown): void {
    const classified = classifyUnknownError(error)
    if (
      classified.category !== "disconnect" &&
      classified.category !== "timeout" &&
      classified.category !== "server"
    ) {
      if (this.#state === "half_open") this.#probeInFlight = false
      return
    }
    this.#failures += 1
    this.#consecutiveFailures += 1
    if (this.#state === "half_open" || this.#consecutiveFailures >= this.#failureThreshold) {
      this.#state = "open"
      this.#openedAt = this.#now()
      this.#probeInFlight = false
    }
  }

  reset(): void {
    this.#state = "closed"
    this.#consecutiveFailures = 0
    this.#openedAt = undefined
    this.#probeInFlight = false
  }

  forceOpen(): void {
    this.#state = "open"
    this.#openedAt = this.#now()
    this.#probeInFlight = false
  }

  snapshot(): CircuitSnapshot {
    return {
      state: this.#state,
      consecutiveFailures: this.#consecutiveFailures,
      openedAt: this.#openedAt,
      retryAt: this.#openedAt === undefined ? undefined : this.#openedAt + this.#cooldownMs,
      probeInFlight: this.#probeInFlight,
      successes: this.#successes,
      failures: this.#failures,
    }
  }
}

export interface SemaphoreLease {
  id: number
  acquiredAt: number
  queuedAt: number
  waitMs: number
  release(): void
}

interface Waiter {
  id: number
  priority: number
  queuedAt: number
  deadline: DeadlineBudget
  signal?: AbortSignal
  resolve: (lease: SemaphoreLease) => void
  reject: (error: unknown) => void
  abort?: () => void
}

export class RequestSemaphore {
  readonly #limit: number
  readonly #now: () => number
  readonly #queue: Waiter[] = []
  readonly #active = new Set<number>()
  #sequence = 0
  #closed = false

  constructor(limit = 16, now: () => number = Date.now) {
    if (!Number.isInteger(limit) || limit < 1 || limit > 10_000) {
      throw new TypeError("Request semaphore limit must be between 1 and 10000")
    }
    this.#limit = limit
    this.#now = now
  }

  acquire(
    deadline: DeadlineBudget,
    options: { signal?: AbortSignal; priority?: number } = {},
  ): Promise<SemaphoreLease> {
    if (this.#closed) return Promise.reject(new RequestCancelledError("Request semaphore is closed."))
    throwIfAborted(options.signal, "transport.queue")
    deadline.assert("transport.queue")
    const id = ++this.#sequence
    const queuedAt = this.#now()
    if (this.#active.size < this.#limit && this.#queue.length === 0) {
      return Promise.resolve(this.#grant(id, queuedAt))
    }
    return new Promise<SemaphoreLease>((resolve, reject) => {
      const waiter: Waiter = {
        id,
        priority: Number.isFinite(options.priority) ? Math.floor(options.priority!) : 0,
        queuedAt,
        deadline,
        signal: options.signal,
        resolve,
        reject,
      }
      waiter.abort = () => {
        const index = this.#queue.indexOf(waiter)
        if (index >= 0) this.#queue.splice(index, 1)
        reject(new RequestCancelledError(options.signal?.reason, { operation: "transport.queue" }))
      }
      options.signal?.addEventListener("abort", waiter.abort, { once: true })
      this.#queue.push(waiter)
      this.#queue.sort((left, right) => right.priority - left.priority || left.id - right.id)
      this.#drain()
    })
  }

  close(reason: unknown = "Request semaphore closed."): void {
    if (this.#closed) return
    this.#closed = true
    for (const waiter of this.#queue.splice(0)) {
      if (waiter.abort && waiter.signal) waiter.signal.removeEventListener("abort", waiter.abort)
      waiter.reject(new RequestCancelledError(reason, { operation: "transport.queue" }))
    }
  }

  reopen(): void {
    this.#closed = false
    this.#drain()
  }

  snapshot(): {
    limit: number
    active: number
    queued: number
    closed: boolean
    queue: Array<{ id: number; priority: number; queuedAt: number; waitMs: number; remainingMs: number }>
  } {
    const now = this.#now()
    return {
      limit: this.#limit,
      active: this.#active.size,
      queued: this.#queue.length,
      closed: this.#closed,
      queue: this.#queue.map((waiter) => ({
        id: waiter.id,
        priority: waiter.priority,
        queuedAt: waiter.queuedAt,
        waitMs: Math.max(0, now - waiter.queuedAt),
        remainingMs: waiter.deadline.remaining(),
      })),
    }
  }

  #grant(id: number, queuedAt: number): SemaphoreLease {
    this.#active.add(id)
    const acquiredAt = this.#now()
    let released = false
    return {
      id,
      acquiredAt,
      queuedAt,
      waitMs: Math.max(0, acquiredAt - queuedAt),
      release: () => {
        if (released) return
        released = true
        this.#active.delete(id)
        this.#drain()
      },
    }
  }

  #drain(): void {
    if (this.#closed) return
    const now = this.#now()
    for (let index = this.#queue.length - 1; index >= 0; index -= 1) {
      const waiter = this.#queue[index]!
      if (waiter.signal?.aborted) {
        this.#queue.splice(index, 1)
        waiter.reject(new RequestCancelledError(waiter.signal.reason, { operation: "transport.queue" }))
      } else if (waiter.deadline.exhausted()) {
        this.#queue.splice(index, 1)
        waiter.reject(new RequestTimeoutError(waiter.deadline.timeoutMs, { operation: "transport.queue" }))
      }
    }
    while (this.#active.size < this.#limit && this.#queue.length) {
      const waiter = this.#queue.shift()!
      if (waiter.abort && waiter.signal) waiter.signal.removeEventListener("abort", waiter.abort)
      if (waiter.deadline.deadline <= now) {
        waiter.reject(new RequestTimeoutError(waiter.deadline.timeoutMs, { operation: "transport.queue" }))
        continue
      }
      waiter.resolve(this.#grant(waiter.id, waiter.queuedAt))
    }
  }
}

export class TransientFailureWindow {
  readonly #windowMs: number
  readonly #maximum: number
  readonly #now: () => number
  readonly #failures: Array<{ at: number; code: string; operation: string }> = []

  constructor(options: { windowMs?: number; maximum?: number; now?: () => number } = {}) {
    this.#windowMs = Math.max(1, Math.floor(options.windowMs ?? 60_000))
    this.#maximum = Math.max(1, Math.floor(options.maximum ?? 1_024))
    this.#now = options.now ?? Date.now
  }

  record(operation: string, error: unknown): void {
    const classified = classifyUnknownError(error)
    this.#failures.push({ at: this.#now(), code: classified.code, operation })
    this.prune()
    while (this.#failures.length > this.#maximum) this.#failures.shift()
  }

  count(operation?: string): number {
    this.prune()
    return operation
      ? this.#failures.filter((failure) => failure.operation === operation).length
      : this.#failures.length
  }

  rate(operation?: string): number {
    return this.count(operation) / (this.#windowMs / 1_000)
  }

  prune(): number {
    const cutoff = this.#now() - this.#windowMs
    let removed = 0
    while (this.#failures[0] && this.#failures[0].at < cutoff) {
      this.#failures.shift()
      removed += 1
    }
    return removed
  }

  snapshot(): Array<{ at: number; code: string; operation: string }> {
    this.prune()
    return this.#failures.map((failure) => ({ ...failure }))
  }
}
