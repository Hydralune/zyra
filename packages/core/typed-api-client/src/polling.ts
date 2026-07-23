import { abortableDelay, combineAbortSignals, throwIfAborted } from "./cancellation.ts"
import {
  RequestCancelledError,
  classifyUnknownError,
  isRetryableError,
  type SerializedZyraError,
} from "./errors.ts"

export type PollingState = "idle" | "running" | "backoff" | "stopped" | "failed"

export interface PollResult<T> {
  items: T[]
  cursor?: string
  caughtUp: boolean
}

export interface PollingSnapshot {
  state: PollingState
  cursor?: string
  cycles: number
  delivered: number
  consecutiveFailures: number
  startedAt?: number
  lastPollAt?: number
  lastDeliveryAt?: number
  nextPollAt?: number
  error?: SerializedZyraError
}

export interface PollingOptions<T> {
  poll(cursor: string | undefined, signal: AbortSignal): Promise<PollResult<T>>
  deliver(items: readonly T[], cursor: string | undefined): void | Promise<void>
  intervalMs?: number
  idleIntervalMs?: number
  maxBackoffMs?: number
  maxConsecutiveFailures?: number
  signal?: AbortSignal
  now?: () => number
}

export class CursorPollingLoop<T> {
  readonly #poll: PollingOptions<T>["poll"]
  readonly #deliver: PollingOptions<T>["deliver"]
  readonly #intervalMs: number
  readonly #idleIntervalMs: number
  readonly #maxBackoffMs: number
  readonly #maxConsecutiveFailures: number
  readonly #parentSignal?: AbortSignal
  readonly #now: () => number
  readonly #listeners = new Set<(snapshot: PollingSnapshot) => void>()
  #controller?: AbortController
  #run?: Promise<void>
  #state: PollingState = "idle"
  #cursor?: string
  #cycles = 0
  #delivered = 0
  #consecutiveFailures = 0
  #startedAt?: number
  #lastPollAt?: number
  #lastDeliveryAt?: number
  #nextPollAt?: number
  #error?: SerializedZyraError

  constructor(options: PollingOptions<T>) {
    this.#poll = options.poll
    this.#deliver = options.deliver
    this.#intervalMs = positiveDelay(options.intervalMs ?? 250, "poll interval")
    this.#idleIntervalMs = positiveDelay(options.idleIntervalMs ?? 1_000, "idle poll interval")
    this.#maxBackoffMs = positiveDelay(options.maxBackoffMs ?? 10_000, "maximum poll backoff")
    this.#maxConsecutiveFailures = Math.max(1, Math.floor(options.maxConsecutiveFailures ?? 8))
    this.#parentSignal = options.signal
    this.#now = options.now ?? Date.now
  }

  start(cursor?: string): Promise<void> {
    if (this.#run) return this.#run
    this.#cursor = cursor
    this.#controller = new AbortController()
    const combined = combineAbortSignals([this.#controller.signal, this.#parentSignal], "polling")
    this.#state = "running"
    this.#startedAt = this.#now()
    this.#error = undefined
    this.#emit()
    const run = this.#loop(combined.signal)
      .catch((error) => {
        const classified = classifyUnknownError(error, { operation: "cursor.polling" })
        if (classified.category === "cancellation") {
          this.#state = "stopped"
          return
        }
        this.#state = "failed"
        this.#error = classified.serialize()
        throw classified
      })
      .finally(() => {
        combined.dispose()
        if (this.#run === run) this.#run = undefined
        this.#nextPollAt = undefined
        this.#emit()
      })
    this.#run = run
    return run
  }

  stop(reason: unknown = "Polling stopped."): void {
    this.#controller?.abort(reason)
    this.#controller = undefined
    if (this.#state !== "failed") this.#state = "stopped"
    this.#emit()
  }

  reset(cursor?: string): void {
    if (this.#run) throw new TypeError("Cannot reset a running polling loop")
    this.#cursor = cursor
    this.#state = "idle"
    this.#cycles = 0
    this.#delivered = 0
    this.#consecutiveFailures = 0
    this.#startedAt = undefined
    this.#lastPollAt = undefined
    this.#lastDeliveryAt = undefined
    this.#nextPollAt = undefined
    this.#error = undefined
    this.#emit()
  }

  listen(listener: (snapshot: PollingSnapshot) => void): () => void {
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  snapshot(): PollingSnapshot {
    return {
      state: this.#state,
      cursor: this.#cursor,
      cycles: this.#cycles,
      delivered: this.#delivered,
      consecutiveFailures: this.#consecutiveFailures,
      startedAt: this.#startedAt,
      lastPollAt: this.#lastPollAt,
      lastDeliveryAt: this.#lastDeliveryAt,
      nextPollAt: this.#nextPollAt,
      error: this.#error
        ? {
            ...this.#error,
            details: { ...this.#error.details },
            context: { ...this.#error.context },
          }
        : undefined,
    }
  }

  get running(): boolean {
    return Boolean(this.#run)
  }

  async #loop(signal: AbortSignal): Promise<void> {
    while (!signal.aborted) {
      throwIfAborted(signal, "cursor.polling")
      this.#cycles += 1
      this.#lastPollAt = this.#now()
      this.#state = "running"
      this.#emit()
      try {
        const result = await this.#poll(this.#cursor, signal)
        throwIfAborted(signal, "cursor.polling")
        if (!Array.isArray(result.items)) throw new TypeError("Polling result items must be an array")
        if (result.cursor !== undefined && typeof result.cursor !== "string") {
          throw new TypeError("Polling cursor must be a string")
        }
        if (result.items.length) {
          await this.#deliver(result.items, result.cursor)
          this.#delivered += result.items.length
          this.#lastDeliveryAt = this.#now()
        }
        this.#cursor = result.cursor ?? this.#cursor
        this.#consecutiveFailures = 0
        this.#error = undefined
        const delay = result.caughtUp ? this.#idleIntervalMs : this.#intervalMs
        this.#nextPollAt = this.#now() + delay
        this.#emit()
        await abortableDelay(delay, signal, { source: "cursor.polling.interval" })
      } catch (error) {
        const classified = classifyUnknownError(error, { operation: "cursor.polling" })
        if (classified.category === "cancellation") throw classified
        this.#consecutiveFailures += 1
        this.#error = classified.serialize()
        if (!isRetryableError(classified) || this.#consecutiveFailures >= this.#maxConsecutiveFailures) {
          throw classified
        }
        this.#state = "backoff"
        const delay = Math.min(
          this.#maxBackoffMs,
          this.#intervalMs * 2 ** Math.max(0, this.#consecutiveFailures - 1),
        )
        this.#nextPollAt = this.#now() + delay
        this.#emit()
        await abortableDelay(delay, signal, { source: "cursor.polling.backoff" })
      }
    }
    throw new RequestCancelledError(signal.reason, { operation: "cursor.polling" })
  }

  #emit(): void {
    const snapshot = this.snapshot()
    for (const listener of this.#listeners) {
      try {
        listener(snapshot)
      } catch {
        // Polling observers cannot alter delivery or retry semantics.
      }
    }
  }
}

function positiveDelay(value: number, label: string): number {
  if (!Number.isFinite(value) || value < 1 || value > 10 * 60_000) {
    throw new TypeError(`${label} must be between 1 ms and 10 minutes`)
  }
  return Math.floor(value)
}

export class BoundedIdentityWindow {
  readonly #maximum: number
  readonly #seen = new Set<string>()

  constructor(maximum = 10_000) {
    if (!Number.isInteger(maximum) || maximum < 1) throw new TypeError("Identity window maximum must be positive")
    this.#maximum = maximum
  }

  filter<T>(items: readonly T[], identity: (item: T) => string): T[] {
    const fresh: T[] = []
    for (const item of items) {
      const id = identity(item)
      if (!id || this.#seen.has(id)) continue
      this.#seen.add(id)
      fresh.push(item)
    }
    while (this.#seen.size > this.#maximum) {
      const oldest = this.#seen.values().next().value
      if (oldest === undefined) break
      this.#seen.delete(oldest)
    }
    return fresh
  }

  has(identity: string): boolean {
    return this.#seen.has(identity)
  }

  clear(): void {
    this.#seen.clear()
  }

  get size(): number {
    return this.#seen.size
  }
}
