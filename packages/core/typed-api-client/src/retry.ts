import {
  DEFAULT_RETRY_ATTEMPTS,
  DEFAULT_RETRY_BASE_DELAY_MS,
  DEFAULT_RETRY_FACTOR,
  DEFAULT_RETRY_JITTER,
  DEFAULT_RETRY_MAX_DELAY_MS,
  MAX_ATTEMPT_HISTORY,
  RETRYABLE_HTTP_STATUSES,
  ZYRA_RETRY_AFTER_HEADER,
  clampInteger,
} from "./constants.ts"
import { DeadlineBudget, abortableDelay, throwIfAborted } from "./cancellation.ts"
import {
  IdempotencyRequiredError,
  RequestCancelledError,
  RequestTimeoutError,
  RetryExhaustedError,
  ZyraApiError,
  classifyUnknownError,
  isRetryableError,
} from "./errors.ts"

export type RetryDecision = "retry" | "fail" | "succeed"

export interface RetryPolicy {
  attempts: number
  baseDelayMs: number
  maxDelayMs: number
  factor: number
  jitter: number
  respectRetryAfter: boolean
  retryUnsafeMutations: boolean
}

export interface RetryContext {
  operation: string
  method: string
  idempotencyKey?: string
  signal?: AbortSignal
  deadline: DeadlineBudget
}

export interface AttemptRecord {
  attempt: number
  startedAt: number
  finishedAt: number
  elapsedMs: number
  outcome: "success" | "failure" | "cancelled"
  decision: RetryDecision
  delayMs: number
  errorCode?: string
  errorMessage?: string
  status?: number
}

export interface RetryResult<T> {
  value: T
  attempts: AttemptRecord[]
}

function normalizeFactor(value: number | undefined): number {
  const candidate = value ?? DEFAULT_RETRY_FACTOR
  if (!Number.isFinite(candidate) || candidate < 1 || candidate > 10) {
    throw new TypeError("Retry factor must be between 1 and 10")
  }
  return candidate
}

function normalizeJitter(value: number | undefined): number {
  const candidate = value ?? DEFAULT_RETRY_JITTER
  if (!Number.isFinite(candidate) || candidate < 0 || candidate > 1) {
    throw new TypeError("Retry jitter must be between 0 and 1")
  }
  return candidate
}

export function normalizeRetryPolicy(policy: Partial<RetryPolicy> = {}): RetryPolicy {
  const attempts = clampInteger(policy.attempts ?? DEFAULT_RETRY_ATTEMPTS, 1, 10, "retry attempts")
  const baseDelayMs = clampInteger(
    policy.baseDelayMs ?? DEFAULT_RETRY_BASE_DELAY_MS,
    0,
    60_000,
    "retry base delay",
  )
  const maxDelayMs = clampInteger(
    policy.maxDelayMs ?? DEFAULT_RETRY_MAX_DELAY_MS,
    baseDelayMs,
    5 * 60_000,
    "retry maximum delay",
  )
  return {
    attempts,
    baseDelayMs,
    maxDelayMs,
    factor: normalizeFactor(policy.factor),
    jitter: normalizeJitter(policy.jitter),
    respectRetryAfter: policy.respectRetryAfter ?? true,
    retryUnsafeMutations: policy.retryUnsafeMutations ?? false,
  }
}

export function methodCanRetry(method: string, idempotencyKey?: string, allowUnsafe = false): boolean {
  const normalized = method.toUpperCase()
  if (normalized === "GET" || normalized === "HEAD" || normalized === "OPTIONS") return true
  if (normalized === "PUT" || normalized === "DELETE") return true
  if (idempotencyKey) return true
  return allowUnsafe
}

export function assertRetrySafety(context: Pick<RetryContext, "operation" | "method" | "idempotencyKey">): void {
  if (!methodCanRetry(context.method, context.idempotencyKey, false)) {
    throw new IdempotencyRequiredError(context.operation, {
      method: context.method,
      operation: context.operation,
    })
  }
}

export function retryDelay(
  attempt: number,
  policy: RetryPolicy,
  options: { random?: () => number; retryAfterMs?: number } = {},
): number {
  if (options.retryAfterMs !== undefined && policy.respectRetryAfter) {
    return Math.min(policy.maxDelayMs, Math.max(0, Math.floor(options.retryAfterMs)))
  }
  const exponent = Math.max(0, attempt - 1)
  const base = Math.min(policy.maxDelayMs, policy.baseDelayMs * policy.factor ** exponent)
  if (policy.jitter === 0 || base === 0) return Math.floor(base)
  const random = Math.min(1, Math.max(0, (options.random ?? Math.random)()))
  const spread = base * policy.jitter
  return Math.max(0, Math.floor(base - spread + random * spread * 2))
}

export function parseRetryAfter(value: string | null | undefined, now = Date.now()): number | undefined {
  if (!value?.trim()) return undefined
  const seconds = Number(value)
  if (Number.isFinite(seconds) && seconds >= 0) return Math.floor(seconds * 1_000)
  const timestamp = Date.parse(value)
  if (Number.isNaN(timestamp)) return undefined
  return Math.max(0, timestamp - now)
}

export function retryAfterFromError(error: unknown): number | undefined {
  if (!(error instanceof ZyraApiError)) return undefined
  const details = error.details
  const direct = details.retry_after_ms
  if (typeof direct === "number" && Number.isFinite(direct) && direct >= 0) return Math.floor(direct)
  if (typeof direct === "string") {
    const parsed = Number(direct)
    if (Number.isFinite(parsed) && parsed >= 0) return Math.floor(parsed)
  }
  const header =
    typeof details.retry_after === "string"
      ? details.retry_after
      : typeof details[ZYRA_RETRY_AFTER_HEADER] === "string"
        ? details[ZYRA_RETRY_AFTER_HEADER]
        : undefined
  return parseRetryAfter(header)
}

export function defaultRetryDecision(error: unknown, context: RetryContext, attempt: number, policy: RetryPolicy): RetryDecision {
  if (error instanceof RequestCancelledError) return "fail"
  if (error instanceof RequestTimeoutError && context.deadline.exhausted()) return "fail"
  const classified = classifyUnknownError(error, {
    operation: context.operation,
    method: context.method,
    attempt,
  })
  if (!classified.retryable) return "fail"
  if (!methodCanRetry(context.method, context.idempotencyKey, policy.retryUnsafeMutations)) return "fail"
  if (classified.status && !RETRYABLE_HTTP_STATUSES.has(classified.status) && classified.category !== "disconnect") {
    return "fail"
  }
  return attempt < policy.attempts ? "retry" : "fail"
}

export class AttemptJournal {
  readonly #maximum: number
  readonly #records: AttemptRecord[] = []

  constructor(maximum = MAX_ATTEMPT_HISTORY) {
    this.#maximum = clampInteger(maximum, 1, 10_000, "attempt journal maximum")
  }

  push(record: AttemptRecord): void {
    if (!Number.isInteger(record.attempt) || record.attempt < 1) throw new TypeError("Attempt must be positive")
    if (record.finishedAt < record.startedAt) throw new TypeError("Attempt finish must follow start")
    this.#records.push({ ...record })
    while (this.#records.length > this.#maximum) this.#records.shift()
  }

  last(): AttemptRecord | undefined {
    const value = this.#records[this.#records.length - 1]
    return value ? { ...value } : undefined
  }

  failures(): AttemptRecord[] {
    return this.#records.filter((record) => record.outcome === "failure").map((record) => ({ ...record }))
  }

  totalElapsedMs(): number {
    return this.#records.reduce((total, record) => total + record.elapsedMs + record.delayMs, 0)
  }

  clear(): void {
    this.#records.length = 0
  }

  snapshot(): AttemptRecord[] {
    return this.#records.map((record) => ({ ...record }))
  }

  get size(): number {
    return this.#records.length
  }
}

export async function withRetry<T>(
  operation: (attempt: number) => Promise<T>,
  context: RetryContext,
  options: {
    policy?: Partial<RetryPolicy>
    decide?: (error: unknown, context: RetryContext, attempt: number, policy: RetryPolicy) => RetryDecision
    random?: () => number
    now?: () => number
    onAttempt?: (record: AttemptRecord) => void | Promise<void>
  } = {},
): Promise<RetryResult<T>> {
  const policy = normalizeRetryPolicy(options.policy)
  const decide = options.decide ?? defaultRetryDecision
  const now = options.now ?? Date.now
  const journal = new AttemptJournal(policy.attempts)
  if (policy.attempts > 1 && !policy.retryUnsafeMutations) assertRetrySafety(context)
  let lastError: unknown

  for (let attempt = 1; attempt <= policy.attempts; attempt += 1) {
    throwIfAborted(context.signal, context.operation)
    context.deadline.assert(context.operation)
    const startedAt = now()
    try {
      const value = await operation(attempt)
      const finishedAt = now()
      const record: AttemptRecord = {
        attempt,
        startedAt,
        finishedAt,
        elapsedMs: Math.max(0, finishedAt - startedAt),
        outcome: "success",
        decision: "succeed",
        delayMs: 0,
      }
      journal.push(record)
      await options.onAttempt?.({ ...record })
      return { value, attempts: journal.snapshot() }
    } catch (error) {
      lastError = error
      const finishedAt = now()
      const classified = classifyUnknownError(error, {
        operation: context.operation,
        method: context.method,
        attempt,
      })
      const decision = decide(classified, context, attempt, policy)
      const retryAfterMs = retryAfterFromError(classified)
      const delayMs =
        decision === "retry"
          ? Math.min(
              retryDelay(attempt, policy, { random: options.random, retryAfterMs }),
              context.deadline.remaining(),
            )
          : 0
      const record: AttemptRecord = {
        attempt,
        startedAt,
        finishedAt,
        elapsedMs: Math.max(0, finishedAt - startedAt),
        outcome: classified.category === "cancellation" ? "cancelled" : "failure",
        decision,
        delayMs,
        errorCode: classified.code,
        errorMessage: classified.message,
        status: classified.status,
      }
      journal.push(record)
      await options.onAttempt?.({ ...record })
      if (decision !== "retry") throw classified
      if (delayMs >= context.deadline.remaining()) {
        throw new RequestTimeoutError(context.deadline.timeoutMs, {
          operation: context.operation,
          method: context.method,
          attempt,
        }, classified)
      }
      await abortableDelay(delayMs, context.signal, { source: `${context.operation}.retry-delay` })
    }
  }
  throw new RetryExhaustedError(policy.attempts, lastError, {
    operation: context.operation,
    method: context.method,
  })
}

export class RetryController {
  #policy: RetryPolicy
  readonly #journals = new Map<string, AttemptJournal>()

  constructor(policy: Partial<RetryPolicy> = {}) {
    this.#policy = normalizeRetryPolicy(policy)
  }

  get policy(): RetryPolicy {
    return { ...this.#policy }
  }

  update(policy: Partial<RetryPolicy>): void {
    this.#policy = normalizeRetryPolicy({ ...this.#policy, ...policy })
  }

  async run<T>(
    key: string,
    operation: (attempt: number) => Promise<T>,
    context: RetryContext,
    options: Omit<Parameters<typeof withRetry<T>>[2], "policy" | "onAttempt"> = {},
  ): Promise<T> {
    if (!key.trim()) throw new TypeError("Retry journal key must not be empty")
    const journal = new AttemptJournal(this.#policy.attempts)
    this.#journals.set(key, journal)
    const result = await withRetry(operation, context, {
      ...options,
      policy: this.#policy,
      onAttempt: (record) => journal.push(record),
    })
    return result.value
  }

  journal(key: string): AttemptRecord[] {
    return this.#journals.get(key)?.snapshot() ?? []
  }

  clear(key?: string): void {
    if (key) this.#journals.delete(key)
    else this.#journals.clear()
  }

  keys(): string[] {
    return [...this.#journals.keys()].sort()
  }
}

export function retryableCauseChain(error: unknown): boolean {
  let current: unknown = error
  const seen = new Set<unknown>()
  while (current && !seen.has(current)) {
    seen.add(current)
    if (isRetryableError(current)) return true
    current =
      current instanceof Error && "cause" in current
        ? (current as Error & { cause?: unknown }).cause
        : undefined
  }
  return false
}
