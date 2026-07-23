import {
  TransportKind,
  type IngressCapabilities,
  type JsonValue,
  type ReconnectPolicy,
  type TransportKindValue,
} from "./contracts.ts"
import {
  EventIngressError,
  IngressErrorCode,
  classifyIngressError,
} from "./errors.ts"

interface TransportHealth {
  kind: TransportKindValue
  successes: number
  failures: number
  consecutiveFailures: number
  lastSuccessAtMs?: number
  lastFailureAtMs?: number
  cooldownUntilMs?: number
  lastErrorCode?: string
  lastErrorMessage?: string
}

export interface ReconnectDecision {
  action: "retry" | "resync" | "fail" | "stop"
  attempt: number
  delayMs: number
  transport?: TransportKindValue
  error: EventIngressError
  exhausted: boolean
}

export class ReconnectSupervisor {
  readonly #policy: ReconnectPolicy
  readonly #now: () => number
  readonly #random: () => number
  readonly #sleepImpl: (milliseconds: number, signal?: AbortSignal) => Promise<void>
  readonly #health = new Map<TransportKindValue, TransportHealth>()
  #reconnectAttempt = 0
  #resyncAttempt = 0
  #connectedAtMs: number | undefined
  #stopped = false
  #stopReason: unknown

  constructor(
    policy: ReconnectPolicy,
    options: {
      now?: () => number
      random?: () => number
      sleep?: (milliseconds: number, signal?: AbortSignal) => Promise<void>
    } = {},
  ) {
    this.#policy = policy
    this.#now = options.now ?? Date.now
    this.#random = options.random ?? Math.random
    this.#sleepImpl = options.sleep ?? abortableSleep
    for (const kind of Object.values(TransportKind)) {
      this.#health.set(kind, {
        kind,
        successes: 0,
        failures: 0,
        consecutiveFailures: 0,
      })
    }
  }

  get reconnectAttempt(): number {
    return this.#reconnectAttempt
  }

  get resyncAttempt(): number {
    return this.#resyncAttempt
  }

  get stopped(): boolean {
    return this.#stopped
  }

  connected(kind: TransportKindValue): void {
    this.#assertRunning()
    const now = this.#now()
    const health = this.#health.get(kind)!
    health.successes += 1
    health.consecutiveFailures = 0
    health.lastSuccessAtMs = now
    health.cooldownUntilMs = undefined
    health.lastErrorCode = undefined
    health.lastErrorMessage = undefined
    this.#connectedAtMs = now
  }

  stable(kind: TransportKindValue): void {
    this.#assertRunning()
    const health = this.#health.get(kind)!
    health.consecutiveFailures = 0
    health.cooldownUntilMs = undefined
    if (
      this.#connectedAtMs !== undefined &&
      this.#now() - this.#connectedAtMs >= this.#policy.stableResetMs
    ) {
      this.#reconnectAttempt = 0
      this.#resyncAttempt = 0
    }
  }

  failure(
    value: unknown,
    options: {
      transport?: TransportKindValue
      allowResync?: boolean
      signal?: AbortSignal
    } = {},
  ): ReconnectDecision {
    const error = classifyIngressError(value, {
      transport: options.transport,
    })
    if (this.#stopped || options.signal?.aborted) {
      return {
        action: "stop",
        attempt: this.#reconnectAttempt,
        delayMs: 0,
        transport: options.transport,
        error,
        exhausted: true,
      }
    }
    if (options.transport) this.#recordFailure(options.transport, error)
    if (error.resyncRequired && options.allowResync !== false) {
      this.#resyncAttempt += 1
      const exhausted = this.#resyncAttempt > this.#policy.resyncAttempts
      return {
        action: exhausted ? "fail" : "resync",
        attempt: this.#resyncAttempt,
        delayMs: exhausted ? 0 : this.#delay(this.#resyncAttempt),
        transport: options.transport,
        error,
        exhausted,
      }
    }
    if (!error.retryable) {
      return {
        action: "fail",
        attempt: this.#reconnectAttempt,
        delayMs: 0,
        transport: options.transport,
        error,
        exhausted: true,
      }
    }
    this.#reconnectAttempt += 1
    const exhausted = this.#reconnectAttempt > this.#policy.attempts
    return {
      action: exhausted ? "fail" : "retry",
      attempt: this.#reconnectAttempt,
      delayMs: exhausted ? 0 : this.#delay(this.#reconnectAttempt),
      transport: options.transport,
      error,
      exhausted,
    }
  }

  availableTransports(
    capabilities: IngressCapabilities,
    preference: readonly TransportKindValue[],
  ): readonly TransportKindValue[] {
    this.#assertRunning()
    const now = this.#now()
    const advertised = new Map(
      capabilities.transports.map((descriptor) => [descriptor.kind, descriptor]),
    )
    const available: TransportKindValue[] = []
    for (const kind of preference) {
      const descriptor = advertised.get(kind)
      if (!descriptor?.available) continue
      const health = this.#health.get(kind)!
      if (health.cooldownUntilMs !== undefined && health.cooldownUntilMs > now) continue
      available.push(kind)
    }
    if (
      !available.length &&
      advertised.get(TransportKind.LONG_POLL)?.available
    ) {
      const health = this.#health.get(TransportKind.LONG_POLL)!
      if (health.cooldownUntilMs !== undefined && health.cooldownUntilMs <= now) {
        health.cooldownUntilMs = undefined
      }
      available.push(TransportKind.LONG_POLL)
    }
    return Object.freeze(available)
  }

  selectTransport(
    capabilities: IngressCapabilities,
    preference: readonly TransportKindValue[],
  ): TransportKindValue {
    const available = this.availableTransports(capabilities, preference)
    if (available.length) return available[0]!
    const earliest = [...this.#health.values()]
      .filter((health) =>
        capabilities.transports.some(
          (descriptor) => descriptor.kind === health.kind && descriptor.available,
        ),
      )
      .sort((left, right) =>
        (left.cooldownUntilMs ?? 0) - (right.cooldownUntilMs ?? 0) ||
        left.kind.localeCompare(right.kind),
      )[0]
    if (earliest) {
      earliest.cooldownUntilMs = undefined
      return earliest.kind
    }
    throw new EventIngressError(
      IngressErrorCode.TRANSPORT_UNAVAILABLE,
      "No server-advertised event ingress transport is available.",
      { retryable: true },
    )
  }

  async wait(decision: ReconnectDecision, signal?: AbortSignal): Promise<void> {
    if (decision.delayMs <= 0) {
      if (signal?.aborted) throw signal.reason
      return
    }
    await this.#sleepImpl(decision.delayMs, signal)
  }

  stop(reason?: unknown): void {
    this.#stopped = true
    this.#stopReason = reason
  }

  reset(): void {
    this.#reconnectAttempt = 0
    this.#resyncAttempt = 0
    this.#connectedAtMs = undefined
    this.#stopped = false
    this.#stopReason = undefined
    for (const health of this.#health.values()) {
      health.successes = 0
      health.failures = 0
      health.consecutiveFailures = 0
      health.lastSuccessAtMs = undefined
      health.lastFailureAtMs = undefined
      health.cooldownUntilMs = undefined
      health.lastErrorCode = undefined
      health.lastErrorMessage = undefined
    }
  }

  snapshot(): {
    reconnectAttempt: number
    resyncAttempt: number
    connectedAtMs?: number
    stopped: boolean
    stopReason?: string
    transports: readonly Readonly<TransportHealth>[]
  } {
    return {
      reconnectAttempt: this.#reconnectAttempt,
      resyncAttempt: this.#resyncAttempt,
      connectedAtMs: this.#connectedAtMs,
      stopped: this.#stopped,
      stopReason: this.#stopReason === undefined ? undefined : String(this.#stopReason),
      transports: Object.freeze(
        [...this.#health.values()]
          .sort((left, right) => left.kind.localeCompare(right.kind))
          .map((health) => Object.freeze({ ...health })),
      ),
    }
  }

  exportState(): JsonValue {
    return this.snapshot() as unknown as JsonValue
  }

  #recordFailure(kind: TransportKindValue, error: EventIngressError): void {
    const now = this.#now()
    const health = this.#health.get(kind)!
    health.failures += 1
    health.consecutiveFailures += 1
    health.lastFailureAtMs = now
    health.lastErrorCode = error.code
    health.lastErrorMessage = error.message
    if (health.consecutiveFailures >= this.#policy.transportFailureThreshold) {
      health.cooldownUntilMs = now + this.#policy.transportCooldownMs
    }
  }

  #delay(attempt: number): number {
    if (this.#policy.baseDelayMs === 0) return 0
    const exponential = Math.min(
      this.#policy.maxDelayMs,
      this.#policy.baseDelayMs * this.#policy.factor ** Math.max(0, attempt - 1),
    )
    const spread = exponential * this.#policy.jitter
    const random = Math.min(1, Math.max(0, this.#random()))
    return Math.max(
      0,
      Math.floor(exponential - spread + random * spread * 2),
    )
  }

  #assertRunning(): void {
    if (!this.#stopped) return
    throw new EventIngressError(
      IngressErrorCode.CLOSED,
      "Reconnect supervisor is stopped.",
      {
        context: {
          details: {
            reason: this.#stopReason === undefined ? null : String(this.#stopReason),
          },
        },
      },
    )
  }
}

export function abortableSleep(
  milliseconds: number,
  signal?: AbortSignal,
): Promise<void> {
  const delay = Math.max(0, Math.floor(milliseconds))
  if (signal?.aborted) return Promise.reject(signal.reason)
  if (delay === 0) return Promise.resolve()
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort)
      resolve()
    }, delay)
    const onAbort = () => {
      clearTimeout(timer)
      signal?.removeEventListener("abort", onAbort)
      reject(signal?.reason)
    }
    signal?.addEventListener("abort", onAbort, { once: true })
  })
}

export class HeartbeatWatchdog {
  readonly #timeoutMs: number
  readonly #now: () => number
  readonly #onTimeout: (error: EventIngressError) => void
  #timer: ReturnType<typeof setTimeout> | undefined
  #lastBeatAtMs = 0
  #taskId = ""
  #generation = 0
  #transport: TransportKindValue | undefined
  #running = false

  constructor(
    timeoutMs: number,
    onTimeout: (error: EventIngressError) => void,
    options: { now?: () => number } = {},
  ) {
    if (!Number.isFinite(timeoutMs) || timeoutMs < 1000) {
      throw new TypeError("Heartbeat timeout must be at least 1000ms")
    }
    this.#timeoutMs = Math.floor(timeoutMs)
    this.#onTimeout = onTimeout
    this.#now = options.now ?? Date.now
  }

  start(taskId: string, generation: number, transport: TransportKindValue): void {
    this.stop()
    this.#taskId = taskId
    this.#generation = generation
    this.#transport = transport
    this.#running = true
    this.beat()
  }

  beat(atMs = this.#now()): void {
    if (!this.#running) return
    this.#lastBeatAtMs = atMs
    if (this.#timer) clearTimeout(this.#timer)
    this.#timer = setTimeout(() => this.#expire(), this.#timeoutMs)
  }

  stop(): void {
    this.#running = false
    if (this.#timer) clearTimeout(this.#timer)
    this.#timer = undefined
  }

  snapshot(): {
    running: boolean
    timeoutMs: number
    lastBeatAtMs: number
    taskId: string
    generation: number
    transport?: TransportKindValue
  } {
    return {
      running: this.#running,
      timeoutMs: this.#timeoutMs,
      lastBeatAtMs: this.#lastBeatAtMs,
      taskId: this.#taskId,
      generation: this.#generation,
      transport: this.#transport,
    }
  }

  #expire(): void {
    if (!this.#running) return
    const elapsed = this.#now() - this.#lastBeatAtMs
    if (elapsed < this.#timeoutMs) {
      this.#timer = setTimeout(() => this.#expire(), this.#timeoutMs - elapsed)
      return
    }
    this.#running = false
    this.#timer = undefined
    this.#onTimeout(
      new EventIngressError(
        IngressErrorCode.HEARTBEAT_TIMEOUT,
        `Event ${this.#transport ?? "unknown"} transport missed its heartbeat deadline.`,
        {
          retryable: true,
          context: {
            taskId: this.#taskId,
            generation: this.#generation,
            transport: this.#transport,
            details: {
              timeoutMs: this.#timeoutMs,
              lastBeatAtMs: this.#lastBeatAtMs,
            },
          },
        },
      ),
    )
  }
}
