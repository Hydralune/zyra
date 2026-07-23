export type RetryChannel = "runtime" | "task-list" | "task-detail" | "command"

export interface RetryDecision {
  channel: RetryChannel
  retry: boolean
  attempt: number
  delayMs: number
  retryAt?: number
  exhausted: boolean
  reason: string
}

export interface RetryChannelState {
  attempt: number
  lastFailureAt?: number
  lastSuccessAt?: number
  openUntil?: number
}

export class RetrySupervisor {
  readonly #states = new Map<RetryChannel, RetryChannelState>()
  readonly #maximumAttempts: number
  readonly #maximumDelayMs: number
  readonly #baseDelayMs: number

  constructor(options: {
    maximumAttempts?: number
    maximumDelayMs?: number
    baseDelayMs?: number
  } = {}) {
    this.#maximumAttempts = Math.max(1, Math.min(20, Math.floor(options.maximumAttempts ?? 8)))
    this.#maximumDelayMs = Math.max(1_000, Math.min(300_000, Math.floor(options.maximumDelayMs ?? 30_000)))
    this.#baseDelayMs = Math.max(100, Math.min(30_000, Math.floor(options.baseDelayMs ?? 500)))
  }

  failure(
    channel: RetryChannel,
    options: { retryable: boolean; now?: number; retryAfterMs?: number; reason?: string },
  ): RetryDecision {
    const now = options.now ?? Date.now()
    const state = this.#states.get(channel) ?? { attempt: 0 }
    state.attempt += 1
    state.lastFailureAt = now
    const exhausted = !options.retryable || state.attempt > this.#maximumAttempts
    const delayMs = exhausted
      ? 0
      : this.#delay(channel, state.attempt, options.retryAfterMs)
    state.openUntil = exhausted ? undefined : now + delayMs
    this.#states.set(channel, state)
    return {
      channel,
      retry: !exhausted,
      attempt: state.attempt,
      delayMs,
      retryAt: exhausted ? undefined : now + delayMs,
      exhausted,
      reason:
        options.reason ??
        (options.retryable
          ? exhausted
            ? "Retry budget exhausted."
            : "Transient failure scheduled for retry."
          : "Failure is not retryable."),
    }
  }

  success(channel: RetryChannel, now = Date.now()): void {
    const state = this.#states.get(channel) ?? { attempt: 0 }
    state.attempt = 0
    state.lastSuccessAt = now
    state.openUntil = undefined
    this.#states.set(channel, state)
  }

  canAttempt(channel: RetryChannel, now = Date.now()): boolean {
    const state = this.#states.get(channel)
    return !state?.openUntil || state.openUntil <= now
  }

  remainingDelay(channel: RetryChannel, now = Date.now()): number {
    const openUntil = this.#states.get(channel)?.openUntil
    return openUntil ? Math.max(0, openUntil - now) : 0
  }

  state(channel: RetryChannel): RetryChannelState {
    return { ...(this.#states.get(channel) ?? { attempt: 0 }) }
  }

  reset(channel?: RetryChannel): void {
    if (channel) this.#states.delete(channel)
    else this.#states.clear()
  }

  #delay(channel: RetryChannel, attempt: number, requested?: number): number {
    if (requested !== undefined && Number.isFinite(requested)) {
      return Math.max(0, Math.min(this.#maximumDelayMs, Math.floor(requested)))
    }
    const exponential = Math.min(
      this.#maximumDelayMs,
      this.#baseDelayMs * 2 ** Math.max(0, attempt - 1),
    )
    let hash = 0
    for (const character of `${channel}:${attempt}`) {
      hash = (Math.imul(hash, 31) + character.charCodeAt(0)) >>> 0
    }
    const jitter = 0.85 + (hash % 31) / 100
    return Math.min(this.#maximumDelayMs, Math.round(exponential * jitter))
  }
}
