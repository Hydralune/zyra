import {
  DEFAULT_TIMEOUT_MS,
  ZYRA_API_VERSION,
  clampTimeout,
  isMutatingHttpMethod,
} from "./constants.ts"
import { createCancellationScope, throwIfAborted } from "./cancellation.ts"
import {
  IdempotencyRequiredError,
  HttpResponseError,
  RequestCancelledError,
  RequestTimeoutError,
  TransportDisconnectedError,
  classifyUnknownError,
  errorCode,
  errorMessage,
  isAbortLike,
} from "./errors.ts"
import { HeaderPolicy, type AuthTokenProvider } from "./headers.ts"
import { InflightTracker, TransportTelemetry } from "./telemetry.ts"
import type { PreparedRequest } from "./request.ts"
import { buildRequestUrl } from "./request.ts"
import {
  consumeResponse,
  normalizeResponse,
  type NormalizedApiResponse,
  type ResponseNormalizer,
} from "./response.ts"
import {
  type AttemptRecord,
  type RetryContext,
  type RetryDecision,
  RetryController,
  type RetryPolicy,
  methodCanRetry,
} from "./retry.ts"
import { assertResponseVersion, normalizeVersionPolicy, type VersionPolicy } from "./version.ts"
import {
  RequestSemaphore,
  TransportCircuitBreaker,
  TransientFailureWindow,
  type SemaphoreLease,
} from "./resilience.ts"

export interface ApiTransport {
  readonly name: string
  readonly enabled: boolean
  send<T>(request: PreparedRequest, normalizer: ResponseNormalizer<T>): Promise<NormalizedApiResponse<T>>
  cancel(requestId: string, reason?: unknown): boolean
  close(reason?: unknown): void | Promise<void>
}

export interface FetchTransportOptions {
  baseUrl: string
  fetch?: typeof fetch
  auth?: AuthTokenProvider
  defaultHeaders?: HeadersInit
  version?: Partial<VersionPolicy>
  retry?: Partial<RetryPolicy>
  clientName?: string
  clientVersion?: string
  telemetry?: TransportTelemetry
  now?: () => number
  concurrency?: number
  circuitFailureThreshold?: number
  circuitCooldownMs?: number
}

export interface StreamingResponseHandle {
  readonly requestId: string
  readonly operation: string
  readonly response: Response
  readonly openedAt: number
  readonly closed: boolean
  close(reason?: unknown): void
}

function originOf(url: URL): string {
  return `${url.protocol}//${url.host}`
}

function fetchFunction(value: typeof fetch | undefined): typeof fetch {
  const selected = value ?? globalThis.fetch
  if (typeof selected !== "function") throw new TypeError("No Fetch implementation is available")
  return selected.bind(globalThis)
}

export class FetchApiTransport implements ApiTransport {
  readonly name = "fetch"
  readonly #baseUrl: string
  readonly #fetch: typeof fetch
  readonly #headerPolicy: HeaderPolicy
  readonly #versionPolicy: VersionPolicy
  readonly #retry: RetryController
  readonly #telemetry: TransportTelemetry
  readonly #inflight = new InflightTracker()
  readonly #controllers = new Map<string, AbortController>()
  readonly #now: () => number
  readonly #semaphore: RequestSemaphore
  readonly #circuit: TransportCircuitBreaker
  readonly #failures: TransientFailureWindow
  #enabled = true

  constructor(options: FetchTransportOptions) {
    this.#baseUrl = options.baseUrl.replace(/\/+$/g, "")
    this.#fetch = fetchFunction(options.fetch)
    this.#versionPolicy = normalizeVersionPolicy({
      requested: ZYRA_API_VERSION,
      ...options.version,
    })
    this.#headerPolicy = new HeaderPolicy({
      version: this.#versionPolicy,
      auth: options.auth,
      defaults: options.defaultHeaders,
      clientName: options.clientName,
      clientVersion: options.clientVersion,
    })
    this.#retry = new RetryController(options.retry)
    this.#telemetry = options.telemetry ?? new TransportTelemetry()
    this.#now = options.now ?? Date.now
    this.#semaphore = new RequestSemaphore(options.concurrency ?? 16, this.#now)
    this.#circuit = new TransportCircuitBreaker({
      failureThreshold: options.circuitFailureThreshold,
      cooldownMs: options.circuitCooldownMs,
      now: this.#now,
    })
    this.#failures = new TransientFailureWindow({ now: this.#now })
  }

  get enabled(): boolean {
    return this.#enabled
  }

  get telemetry(): TransportTelemetry {
    return this.#telemetry
  }

  disable(reason = "Transport disabled."): void {
    if (!this.#enabled) return
    this.#enabled = false
    this.#inflight.cancelAll(reason)
    for (const controller of this.#controllers.values()) controller.abort(reason)
    this.#controllers.clear()
    this.#semaphore.close(reason)
  }

  enable(): void {
    this.#enabled = true
    this.#semaphore.reopen()
  }

  async send<T>(
    request: PreparedRequest,
    normalizer: ResponseNormalizer<T>,
  ): Promise<NormalizedApiResponse<T>> {
    if (!this.#enabled) {
      throw new TransportDisconnectedError("The registered fetch transport is disabled.", {
        operation: request.operation,
        method: request.method,
        requestId: request.requestId,
      })
    }
    if (isMutatingHttpMethod(request.method) && request.retry && !request.idempotencyKey) {
      throw new IdempotencyRequiredError(request.operation, {
        operation: request.operation,
        method: request.method,
        requestId: request.requestId,
      })
    }
    const controller = new AbortController()
    this.#controllers.set(request.requestId, controller)
    this.#inflight.start(request.requestId, request.operation, 1, (reason) => controller.abort(reason))
    this.#telemetry.record({
      phase: "queued",
      operation: request.operation,
      requestId: request.requestId,
      method: request.method,
      path: request.path,
      binding: request.binding,
    })

    const retryEnabled =
      request.retry &&
      methodCanRetry(request.method, request.idempotencyKey, this.#retry.policy.retryUnsafeMutations)
    const policy = retryEnabled ? this.#retry.policy : { ...this.#retry.policy, attempts: 1 }
    let lease: SemaphoreLease | undefined
    try {
      this.#circuit.beforeRequest(request.operation)
      lease = await this.#semaphore.acquire(request.deadline, {
        signal: controller.signal,
        priority: Number(request.metadata.priority ?? 0),
      })
      const result = await this.#retry.run(
        request.requestId,
        (attempt) => this.#attempt(request, normalizer, attempt, controller.signal),
        {
          operation: request.operation,
          method: request.method,
          idempotencyKey: request.idempotencyKey,
          signal: controller.signal,
          deadline: request.deadline,
        },
        {
          decide: (
            error: unknown,
            _context: RetryContext,
            attempt: number,
            retryPolicy: RetryPolicy,
          ): RetryDecision => {
            const classified = classifyUnknownError(error)
            if (!retryEnabled || !classified.retryable || attempt >= retryPolicy.attempts) return "fail"
            if (classified.category === "authentication" || classified.category === "version") return "fail"
            if (classified.category === "receipt" || classified.category === "protocol") return "fail"
            return "retry"
          },
        },
      )
      this.#circuit.success()
      return result
    } catch (error) {
      const classified = classifyUnknownError(error, {
        operation: request.operation,
        method: request.method,
        requestId: request.requestId,
        binding: request.binding,
      })
      this.#circuit.failure(classified)
      this.#failures.record(request.operation, classified)
      this.#telemetry.record({
        phase: classified.category === "cancellation" ? "cancelled" : "failed",
        operation: request.operation,
        requestId: request.requestId,
        method: request.method,
        path: request.path,
        binding: request.binding,
        error: classified,
      })
      throw classified
    } finally {
      lease?.release()
      this.#controllers.delete(request.requestId)
      this.#inflight.finish(request.requestId)
    }
  }

  async #attempt<T>(
    request: PreparedRequest,
    normalizer: ResponseNormalizer<T>,
    attempt: number,
    transportSignal: AbortSignal,
  ): Promise<NormalizedApiResponse<T>> {
    if (!this.#enabled) {
      throw new TransportDisconnectedError("The registered fetch transport is disabled.", {
        operation: request.operation,
        method: request.method,
        requestId: request.requestId,
        attempt,
      })
    }
    request.deadline.assert(request.operation)
    throwIfAborted(request.signal, request.operation)
    throwIfAborted(transportSignal, request.operation)
    const remaining = Math.min(request.deadline.remaining(), clampTimeout(request.timeoutMs, DEFAULT_TIMEOUT_MS))
    const scope = createCancellationScope({
      timeoutMs: Math.max(25, remaining),
      signal: transportSignal,
      now: this.#now,
      source: request.operation,
    })
    let callerListener: (() => void) | undefined
    if (request.signal) {
      callerListener = () => scope.cancel(request.signal?.reason, "caller")
      if (request.signal.aborted) callerListener()
      else request.signal.addEventListener("abort", callerListener, { once: true })
    }
    const url = buildRequestUrl(this.#baseUrl, request.path, request.query)
    const startedAt = this.#now()
    this.#telemetry.record({
      phase: "started",
      operation: request.operation,
      requestId: request.requestId,
      method: request.method,
      path: request.path,
      attempt,
      binding: request.binding,
      metadata: { origin: originOf(url) },
    })
    try {
      const headers = await this.#headerPolicy.build(
        {
          operation: request.operation,
          contract: request.contract,
          requestId: request.requestId,
          correlationId: request.correlationId,
          causationId: request.causationId,
          idempotencyKey: request.idempotencyKey,
          attempt,
          deadlineMs: request.deadline.remaining(),
          hasBody: request.serializedBody !== undefined,
        },
        request.headers,
      )
      scope.throwIfCancelled()
      const response = await this.#fetch(url, {
        method: request.method,
        headers,
        body: request.serializedBody,
        signal: scope.signal,
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
      })
      this.#telemetry.record({
        phase: "headers",
        operation: request.operation,
        requestId: request.requestId,
        method: request.method,
        path: request.path,
        attempt,
        binding: request.binding,
        status: response.status,
        elapsedMs: Math.max(0, this.#now() - startedAt),
      })
      const raw = await consumeResponse(response, request, this.#versionPolicy, startedAt, this.#now)
      this.#telemetry.record({
        phase: "body",
        operation: request.operation,
        requestId: request.requestId,
        method: request.method,
        path: request.path,
        attempt,
        binding: request.binding,
        status: raw.status,
        bytes: raw.bytes,
        elapsedMs: raw.elapsedMs,
      })
      const normalized = normalizeResponse(raw, request, normalizer)
      this.#telemetry.record({
        phase: "completed",
        operation: request.operation,
        requestId: request.requestId,
        method: request.method,
        path: request.path,
        attempt,
        binding: request.binding,
        status: raw.status,
        bytes: raw.bytes,
        elapsedMs: raw.elapsedMs,
        receiptId: raw.receiptId,
        replayed: raw.replayed,
      })
      return normalized
    } catch (error) {
      const cancellation = scope.record()
      if (cancellation?.kind === "timeout") {
        throw new RequestTimeoutError(request.timeoutMs, {
          operation: request.operation,
          method: request.method,
          requestId: request.requestId,
          attempt,
          binding: request.binding,
        }, error)
      }
      if (cancellation || isAbortLike(error)) {
        throw new RequestCancelledError(cancellation?.reason, {
          operation: request.operation,
          method: request.method,
          requestId: request.requestId,
          attempt,
          binding: request.binding,
        }, error)
      }
      const classified = classifyUnknownError(error, {
        operation: request.operation,
        method: request.method,
        requestId: request.requestId,
        attempt,
        binding: request.binding,
      })
      if (classified.category === "unknown") {
        throw new TransportDisconnectedError(classified.message, classified.context, classified)
      }
      throw classified
    } finally {
      scope.dispose()
      if (request.signal && callerListener) request.signal.removeEventListener("abort", callerListener)
    }
  }

  async openStream(request: PreparedRequest): Promise<StreamingResponseHandle> {
    if (!this.#enabled) {
      throw new TransportDisconnectedError("The registered fetch transport is disabled.", {
        operation: request.operation,
        method: request.method,
        requestId: request.requestId,
      })
    }
    request.deadline.assert(request.operation)
    throwIfAborted(request.signal, request.operation)
    const controller = new AbortController()
    this.#controllers.set(request.requestId, controller)
    this.#inflight.start(request.requestId, request.operation, 1, (reason) => controller.abort(reason))
    this.#telemetry.record({
      phase: "queued",
      operation: request.operation,
      requestId: request.requestId,
      method: request.method,
      path: request.path,
      binding: request.binding,
      metadata: { stream: true },
    })
    let lease: SemaphoreLease | undefined
    let callerListener: (() => void) | undefined
    let settled = false
    const release = (reason?: unknown) => {
      if (settled) return
      settled = true
      if (!controller.signal.aborted) controller.abort(reason ?? "Streaming response closed.")
      if (request.signal && callerListener) request.signal.removeEventListener("abort", callerListener)
      lease?.release()
      this.#controllers.delete(request.requestId)
      this.#inflight.finish(request.requestId)
      this.#telemetry.record({
        phase: "completed",
        operation: request.operation,
        requestId: request.requestId,
        method: request.method,
        path: request.path,
        binding: request.binding,
        metadata: { stream: true, reason: reason === undefined ? undefined : String(reason) },
      })
    }
    try {
      this.#circuit.beforeRequest(request.operation)
      lease = await this.#semaphore.acquire(request.deadline, {
        signal: controller.signal,
        priority: Number(request.metadata.priority ?? 0),
      })
      if (request.signal) {
        callerListener = () => controller.abort(request.signal?.reason)
        if (request.signal.aborted) callerListener()
        else request.signal.addEventListener("abort", callerListener, { once: true })
      }
      const url = buildRequestUrl(this.#baseUrl, request.path, request.query)
      const headers = await this.#headerPolicy.build(
        {
          operation: request.operation,
          contract: request.contract,
          requestId: request.requestId,
          correlationId: request.correlationId,
          causationId: request.causationId,
          idempotencyKey: request.idempotencyKey,
          attempt: 1,
          deadlineMs: request.deadline.remaining(),
          hasBody: false,
        },
        request.headers,
      )
      const startedAt = this.#now()
      this.#telemetry.record({
        phase: "started",
        operation: request.operation,
        requestId: request.requestId,
        method: request.method,
        path: request.path,
        attempt: 1,
        binding: request.binding,
        metadata: { origin: originOf(url), stream: true },
      })
      const response = await this.#fetch(url, {
        method: request.method,
        headers,
        signal: controller.signal,
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
      })
      this.#telemetry.record({
        phase: "headers",
        operation: request.operation,
        requestId: request.requestId,
        method: request.method,
        path: request.path,
        attempt: 1,
        binding: request.binding,
        status: response.status,
        elapsedMs: Math.max(0, this.#now() - startedAt),
        metadata: { stream: true },
      })
      assertResponseVersion(response.headers, this.#versionPolicy, {
        operation: request.operation,
        requestId: request.requestId,
      })
      if (!request.expectedStatuses.includes(response.status)) {
        const text = (await response.text()).slice(0, 64 * 1024)
        let body: unknown = text
        try {
          body = text ? JSON.parse(text) : undefined
        } catch {
          // Preserve a bounded text response when the server did not return JSON.
        }
        throw new HttpResponseError(
          response.status,
          errorMessage(body, `Event stream connection failed with HTTP ${response.status}.`),
          {
            code: errorCode(body),
            headers: response.headers,
            body,
            context: {
              operation: request.operation,
              method: request.method,
              requestId: request.requestId,
              binding: request.binding,
            },
          },
        )
      }
      if (!response.body) {
        throw new TransportDisconnectedError("Streaming response omitted a readable body.", {
          operation: request.operation,
          method: request.method,
          requestId: request.requestId,
        })
      }
      this.#circuit.success()
      const openedAt = this.#now()
      let closed = false
      return {
        requestId: request.requestId,
        operation: request.operation,
        response,
        openedAt,
        get closed() {
          return closed
        },
        close(reason?: unknown) {
          if (closed) return
          closed = true
          release(reason)
        },
      }
    } catch (error) {
      release(error)
      const classified = classifyUnknownError(error, {
        operation: request.operation,
        method: request.method,
        requestId: request.requestId,
        binding: request.binding,
      })
      this.#circuit.failure(classified)
      this.#failures.record(request.operation, classified)
      this.#telemetry.record({
        phase: classified.category === "cancellation" ? "cancelled" : "failed",
        operation: request.operation,
        requestId: request.requestId,
        method: request.method,
        path: request.path,
        binding: request.binding,
        error: classified,
        metadata: { stream: true },
      })
      throw classified
    }
  }

  cancel(requestId: string, reason?: unknown): boolean {
    const controller = this.#controllers.get(requestId)
    if (!controller || controller.signal.aborted) return false
    controller.abort(reason ?? "Request cancelled by transport registry.")
    return true
  }

  close(reason?: unknown): void {
    this.disable(reason ? String(reason) : "Transport closed.")
    this.#retry.clear()
  }

  inflight(): ReturnType<InflightTracker["snapshot"]> {
    return this.#inflight.snapshot()
  }

  retryAttempts(requestId: string): AttemptRecord[] {
    return this.#retry.journal(requestId)
  }

  resilience(): {
    circuit: ReturnType<TransportCircuitBreaker["snapshot"]>
    semaphore: ReturnType<RequestSemaphore["snapshot"]>
    failures: ReturnType<TransientFailureWindow["snapshot"]>
  } {
    return {
      circuit: this.#circuit.snapshot(),
      semaphore: this.#semaphore.snapshot(),
      failures: this.#failures.snapshot(),
    }
  }
}
