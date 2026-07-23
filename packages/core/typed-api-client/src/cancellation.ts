import { clampTimeout } from "./constants.ts"
import { RequestCancelledError, RequestTimeoutError } from "./errors.ts"

export type CancellationKind = "caller" | "timeout" | "registry" | "transport" | "shutdown"

export interface CancellationRecord {
  kind: CancellationKind
  reason?: unknown
  at: number
  source: string
}

export interface CancellationScope {
  signal: AbortSignal
  deadline: number
  timeoutMs: number
  cancel(reason?: unknown, kind?: CancellationKind): void
  throwIfCancelled(): void
  dispose(): void
  record(): CancellationRecord | undefined
}

function abortReason(signal: AbortSignal): unknown {
  if ("reason" in signal) return signal.reason
  return undefined
}

function isTimeoutRecord(record: CancellationRecord | undefined): boolean {
  return record?.kind === "timeout"
}

function removeAbortListener(signal: AbortSignal, listener: () => void): void {
  try {
    signal.removeEventListener("abort", listener)
  } catch {
    // AbortSignal implementations are permitted to be minimal.
  }
}

export function createCancellationScope(options: {
  timeoutMs?: number
  signal?: AbortSignal
  now?: () => number
  source?: string
} = {}): CancellationScope {
  const timeoutMs = clampTimeout(options.timeoutMs)
  const now = options.now ?? Date.now
  const source = options.source ?? "request"
  const controller = new AbortController()
  const deadline = now() + timeoutMs
  let cancellation: CancellationRecord | undefined
  let disposed = false

  const cancel = (reason?: unknown, kind: CancellationKind = "caller") => {
    if (disposed || controller.signal.aborted) return
    cancellation = { kind, reason, at: now(), source }
    controller.abort(reason)
  }

  const timeoutHandle = setTimeout(() => {
    cancel(new RequestTimeoutError(timeoutMs), "timeout")
  }, timeoutMs)

  let callerListener: (() => void) | undefined
  if (options.signal) {
    callerListener = () => cancel(abortReason(options.signal!), "caller")
    if (options.signal.aborted) callerListener()
    else options.signal.addEventListener("abort", callerListener, { once: true })
  }

  return {
    signal: controller.signal,
    deadline,
    timeoutMs,
    cancel,
    throwIfCancelled() {
      if (!controller.signal.aborted) return
      if (isTimeoutRecord(cancellation)) {
        const reason = cancellation?.reason
        if (reason instanceof RequestTimeoutError) throw reason
        throw new RequestTimeoutError(timeoutMs, {}, reason)
      }
      throw new RequestCancelledError(cancellation?.reason ?? abortReason(controller.signal))
    },
    dispose() {
      if (disposed) return
      disposed = true
      clearTimeout(timeoutHandle)
      if (options.signal && callerListener) removeAbortListener(options.signal, callerListener)
    },
    record() {
      return cancellation ? { ...cancellation } : undefined
    },
  }
}

export function combineAbortSignals(
  signals: readonly (AbortSignal | null | undefined)[],
  source = "combined",
): { signal: AbortSignal; dispose(): void; reason(): unknown } {
  const controller = new AbortController()
  const listeners: Array<{ signal: AbortSignal; listener: () => void }> = []
  let firstReason: unknown

  const abortFrom = (signal: AbortSignal) => {
    if (controller.signal.aborted) return
    firstReason = abortReason(signal)
    controller.abort(firstReason ?? new RequestCancelledError(undefined, { operation: source }))
  }

  for (const signal of signals) {
    if (!signal) continue
    if (signal.aborted) {
      abortFrom(signal)
      break
    }
    const listener = () => abortFrom(signal)
    signal.addEventListener("abort", listener, { once: true })
    listeners.push({ signal, listener })
  }

  return {
    signal: controller.signal,
    dispose() {
      for (const { signal, listener } of listeners) removeAbortListener(signal, listener)
      listeners.length = 0
    },
    reason() {
      return firstReason
    },
  }
}

export function throwIfAborted(signal: AbortSignal | undefined, context = "operation"): void {
  if (!signal?.aborted) return
  const reason = abortReason(signal)
  if (reason instanceof RequestTimeoutError || reason instanceof RequestCancelledError) throw reason
  throw new RequestCancelledError(reason, { operation: context })
}

export function abortableDelay(
  durationMs: number,
  signal?: AbortSignal,
  options: { unref?: boolean; source?: string } = {},
): Promise<void> {
  if (!Number.isFinite(durationMs) || durationMs < 0) {
    return Promise.reject(new TypeError("Delay must be a non-negative finite number"))
  }
  throwIfAborted(signal, options.source ?? "delay")
  if (durationMs === 0) return Promise.resolve()
  return new Promise<void>((resolve, reject) => {
    let settled = false
    const finish = (error?: unknown) => {
      if (settled) return
      settled = true
      if (signal) removeAbortListener(signal, onAbort)
      if (error) reject(error)
      else resolve()
    }
    const timer = setTimeout(() => finish(), durationMs)
    if (options.unref && typeof timer === "object" && "unref" in timer && typeof timer.unref === "function") {
      timer.unref()
    }
    const onAbort = () => {
      clearTimeout(timer)
      finish(new RequestCancelledError(abortReason(signal!), { operation: options.source ?? "delay" }))
    }
    if (signal) signal.addEventListener("abort", onAbort, { once: true })
  })
}

export function remainingDeadlineMs(deadline: number, now = Date.now()): number {
  if (!Number.isFinite(deadline)) throw new TypeError("Deadline must be finite")
  return Math.max(0, Math.floor(deadline - now))
}

export function assertDeadline(deadline: number, context = "request"): void {
  const remaining = remainingDeadlineMs(deadline)
  if (remaining <= 0) throw new RequestTimeoutError(0, { operation: context })
}

export class CancellationGroup {
  readonly #controllers = new Map<string, AbortController>()
  readonly #reasons = new Map<string, CancellationRecord>()
  #closed = false

  register(key: string, parent?: AbortSignal): AbortSignal {
    if (this.#closed) throw new RequestCancelledError("Cancellation group is closed")
    if (!key.trim()) throw new TypeError("Cancellation key must not be empty")
    if (this.#controllers.has(key)) throw new TypeError(`Cancellation key already registered: ${key}`)
    const controller = new AbortController()
    this.#controllers.set(key, controller)
    if (parent) {
      const abort = () => this.cancel(key, abortReason(parent), "caller")
      if (parent.aborted) abort()
      else parent.addEventListener("abort", abort, { once: true })
    }
    return controller.signal
  }

  cancel(
    key: string,
    reason: unknown = "Cancelled by cancellation group.",
    kind: CancellationKind = "registry",
  ): boolean {
    const controller = this.#controllers.get(key)
    if (!controller || controller.signal.aborted) return false
    this.#reasons.set(key, { kind, reason, at: Date.now(), source: key })
    controller.abort(reason)
    return true
  }

  release(key: string): boolean {
    this.#reasons.delete(key)
    return this.#controllers.delete(key)
  }

  cancelAll(reason: unknown = "Cancellation group closed.", kind: CancellationKind = "shutdown"): number {
    let count = 0
    for (const key of this.#controllers.keys()) {
      if (this.cancel(key, reason, kind)) count += 1
    }
    return count
  }

  close(reason?: unknown): number {
    this.#closed = true
    return this.cancelAll(reason ?? "Cancellation group closed.", "shutdown")
  }

  has(key: string): boolean {
    return this.#controllers.has(key)
  }

  activeKeys(): string[] {
    return [...this.#controllers.entries()]
      .filter(([, controller]) => !controller.signal.aborted)
      .map(([key]) => key)
      .sort()
  }

  record(key: string): CancellationRecord | undefined {
    const record = this.#reasons.get(key)
    return record ? { ...record } : undefined
  }

  get size(): number {
    return this.#controllers.size
  }

  get closed(): boolean {
    return this.#closed
  }
}

export class DeadlineBudget {
  readonly startedAt: number
  readonly deadline: number
  readonly timeoutMs: number
  readonly #now: () => number

  constructor(timeoutMs: number, options: { startedAt?: number; now?: () => number } = {}) {
    this.#now = options.now ?? Date.now
    this.startedAt = options.startedAt ?? this.#now()
    this.timeoutMs = clampTimeout(timeoutMs)
    this.deadline = this.startedAt + this.timeoutMs
  }

  remaining(): number {
    return remainingDeadlineMs(this.deadline, this.#now())
  }

  elapsed(): number {
    return Math.max(0, this.#now() - this.startedAt)
  }

  exhausted(): boolean {
    return this.remaining() <= 0
  }

  assert(operation = "request"): void {
    if (this.exhausted()) throw new RequestTimeoutError(this.timeoutMs, { operation })
  }

  slice(maximumMs: number, minimumMs = 1): number {
    if (!Number.isFinite(maximumMs) || maximumMs <= 0) throw new TypeError("Maximum slice must be positive")
    const remaining = this.remaining()
    if (remaining < minimumMs) throw new RequestTimeoutError(this.timeoutMs)
    return Math.max(minimumMs, Math.min(Math.floor(maximumMs), remaining))
  }

  fraction(value: number, minimumMs = 1): number {
    if (!Number.isFinite(value) || value <= 0 || value > 1) {
      throw new TypeError("Deadline fraction must be in (0, 1]")
    }
    return this.slice(Math.floor(this.remaining() * value), minimumMs)
  }

  snapshot(): { startedAt: number; deadline: number; timeoutMs: number; elapsedMs: number; remainingMs: number } {
    return {
      startedAt: this.startedAt,
      deadline: this.deadline,
      timeoutMs: this.timeoutMs,
      elapsedMs: this.elapsed(),
      remainingMs: this.remaining(),
    }
  }
}
