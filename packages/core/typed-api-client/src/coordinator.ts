import { RequestCancelledError } from "./errors.ts"
import type { PreparedRequest } from "./request.ts"
import type { NormalizedApiResponse } from "./response.ts"
import type { TransportRegistry } from "./registry.ts"

export interface CoordinatedRequest<T> {
  key: string
  requestId: string
  operation: string
  startedAt: number
  consumers: number
  promise: Promise<NormalizedApiResponse<T>>
}

interface InflightEntry {
  requestId: string
  operation: string
  startedAt: number
  consumers: number
  generation: number
  promise: Promise<NormalizedApiResponse<unknown>>
}

export class RequestCoordinator {
  readonly #registry: TransportRegistry
  readonly #inflight = new Map<string, InflightEntry>()
  readonly #latest = new Map<string, number>()
  #generation = 0
  #closed = false

  constructor(registry: TransportRegistry) {
    this.#registry = registry
  }

  execute<T>(
    key: string,
    request: PreparedRequest,
    options: { deduplicate?: boolean; latestWins?: boolean } = {},
  ): Promise<NormalizedApiResponse<T>> {
    if (this.#closed) return Promise.reject(new RequestCancelledError("Request coordinator is closed."))
    const normalizedKey = key.trim()
    if (!normalizedKey) return Promise.reject(new TypeError("Request coordination key must not be empty"))
    const existing = this.#inflight.get(normalizedKey)
    if (existing && options.deduplicate !== false) {
      existing.consumers += 1
      return existing.promise as Promise<NormalizedApiResponse<T>>
    }
    if (existing && options.latestWins) {
      this.#registry.cancel(existing.requestId, `Superseded by ${request.requestId}`)
      this.#inflight.delete(normalizedKey)
    }
    const generation = ++this.#generation
    this.#latest.set(normalizedKey, generation)
    const promise = this.#registry.execute<T>(request).then((response) => {
      if (this.#closed) throw new RequestCancelledError("Request coordinator closed before response commit.")
      if (options.latestWins && this.#latest.get(normalizedKey) !== generation) {
        throw new RequestCancelledError("Response was superseded by a newer request.")
      }
      return response
    })
    const entry: InflightEntry = {
      requestId: request.requestId,
      operation: request.operation,
      startedAt: Date.now(),
      consumers: 1,
      generation,
      promise: promise as Promise<NormalizedApiResponse<unknown>>,
    }
    this.#inflight.set(normalizedKey, entry)
    void promise.finally(() => {
      if (this.#inflight.get(normalizedKey) === entry) this.#inflight.delete(normalizedKey)
    }).catch(() => {})
    return promise
  }

  release(key: string): boolean {
    const entry = this.#inflight.get(key)
    if (!entry) return false
    entry.consumers = Math.max(0, entry.consumers - 1)
    return true
  }

  cancel(key: string, reason?: unknown): boolean {
    const entry = this.#inflight.get(key)
    if (!entry) return false
    this.#latest.set(key, ++this.#generation)
    const cancelled = this.#registry.cancel(entry.requestId, reason)
    this.#inflight.delete(key)
    return cancelled
  }

  cancelByRequestId(requestId: string, reason?: unknown): boolean {
    for (const [key, entry] of this.#inflight) {
      if (entry.requestId === requestId) return this.cancel(key, reason)
    }
    return false
  }

  cancelAll(reason?: unknown): number {
    let count = 0
    for (const key of [...this.#inflight.keys()]) if (this.cancel(key, reason)) count += 1
    return count
  }

  close(reason?: unknown): void {
    if (this.#closed) return
    this.#closed = true
    this.cancelAll(reason ?? "Request coordinator closed.")
  }

  reopen(): void {
    this.#closed = false
    this.#generation += 1
  }

  snapshot(): Array<{
    key: string
    requestId: string
    operation: string
    startedAt: number
    elapsedMs: number
    consumers: number
    generation: number
  }> {
    const now = Date.now()
    return [...this.#inflight.entries()]
      .map(([key, entry]) => ({
        key,
        requestId: entry.requestId,
        operation: entry.operation,
        startedAt: entry.startedAt,
        elapsedMs: Math.max(0, now - entry.startedAt),
        consumers: entry.consumers,
        generation: entry.generation,
      }))
      .sort((left, right) => left.startedAt - right.startedAt)
  }

  get size(): number {
    return this.#inflight.size
  }

  get closed(): boolean {
    return this.#closed
  }
}
