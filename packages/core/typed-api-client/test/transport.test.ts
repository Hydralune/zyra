import { describe, expect, test } from "bun:test"
import {
  ApiVersionMismatchError,
  AuthenticationError,
  BoundedIdentityWindow,
  CONTRACT_NAMES,
  FetchApiTransport,
  HttpResponseError,
  MalformedJsonError,
  NormalizerDisabledError,
  OPERATION_NAMES,
  RequestFactory,
  RequestSemaphore,
  RequestTimeoutError,
  ResponseValidationError,
  TransportDisabledError,
  TransportDisconnectedError,
  TransportRegistry,
  TransportCircuitBreaker,
  createIdempotencyKey,
  createReceiptId,
  normalizeHealth,
  normalizeReceipt,
  registerCoreNormalizers,
} from "../src/index.ts"
import { jsonResponse, scriptedFetch } from "../src/testing.ts"

function requestId(index = 1): string {
  return `request_00000000000000${index.toString(16)}_0123456789abcdefabcd`
}

function factory(): RequestFactory {
  return new RequestFactory({ defaultTimeoutMs: 500 })
}

function healthRequest(id = requestId()) {
  return factory().get(OPERATION_NAMES.health, CONTRACT_NAMES.health, "/health", {
    requestId: id,
    retry: true,
  })
}

function registryWith(fetch: typeof globalThis.fetch, options: { attempts?: number } = {}) {
  const transport = new FetchApiTransport({
    baseUrl: "http://127.0.0.1:8000",
    fetch,
    retry: {
      attempts: options.attempts ?? 1,
      baseDelayMs: 0,
      maxDelayMs: 0,
      jitter: 0,
    },
  })
  const registry = new TransportRegistry(transport)
  registerCoreNormalizers(registry.normalizers)
  return { transport, registry }
}

describe("typed transport contract", () => {
  test("deduplicates the same request identity and normalizes one real response", async () => {
    const id = requestId(1)
    const script = scriptedFetch([
      () =>
        new Promise<Response>((resolve) => {
          setTimeout(
            () =>
              resolve(
                jsonResponse(
                  {
                    status: "ok",
                    service: "zyra-api",
                    phase: "m2",
                    api_version: "1.0",
                    capabilities: ["typed_transport"],
                  },
                  { requestId: id },
                ),
              ),
            5,
          )
        }),
    ])
    const { registry } = registryWith(script.fetch)
    const request = healthRequest(id)
    const [left, right] = await Promise.all([
      registry.execute<ReturnType<typeof normalizeHealth>>(request),
      registry.execute<ReturnType<typeof normalizeHealth>>(request),
    ])
    expect(script.calls).toHaveLength(1)
    expect(left.data.service).toBe("zyra-api")
    expect(right.data.capabilities).toEqual(["typed_transport"])
  })

  test("fails closed when the unique transport is disabled", async () => {
    const script = scriptedFetch([])
    const { registry } = registryWith(script.fetch)
    registry.disable("mutation test")
    await expect(registry.execute(healthRequest(requestId(2)))).rejects.toBeInstanceOf(TransportDisabledError)
    expect(script.calls).toHaveLength(0)
  })

  test("fails closed when normalizers are disabled", async () => {
    const script = scriptedFetch([])
    const { registry } = registryWith(script.fetch)
    registry.disableNormalizers()
    await expect(registry.execute(healthRequest(requestId(3)))).rejects.toBeInstanceOf(NormalizerDisabledError)
    expect(script.calls).toHaveLength(0)
  })

  test("maps a server authentication failure without retry", async () => {
    const id = requestId(4)
    const script = scriptedFetch([
      jsonResponse(
        {
          error: "authentication_required",
          message: "A bearer token is required.",
          fallback: false,
        },
        { status: 401, requestId: id },
      ),
    ])
    const { registry } = registryWith(script.fetch, { attempts: 3 })
    await expect(registry.execute(healthRequest(id))).rejects.toBeInstanceOf(AuthenticationError)
    expect(script.calls).toHaveLength(1)
  })

  test("maps an API version mismatch without fallback", async () => {
    const id = requestId(5)
    const script = scriptedFetch([
      jsonResponse(
        {
          error: "api_version_mismatch",
          message: "Version not supported.",
          actual_version: "9.0",
          supported_versions: ["1.0"],
          fallback: false,
        },
        { status: 426, requestId: id, apiVersion: "1.0" },
      ),
    ])
    const { registry } = registryWith(script.fetch)
    await expect(registry.execute(healthRequest(id))).rejects.toBeInstanceOf(ApiVersionMismatchError)
    expect(script.calls).toHaveLength(1)
  })

  test("classifies HTTP 409 as a conflict instead of a version mismatch", async () => {
    const id = requestId(51)
    const script = scriptedFetch([
      jsonResponse(
        {
          error: "state_conflict",
          message: "The worker generation is still leased.",
          fallback: false,
        },
        { status: 409, requestId: id, apiVersion: "1.0" },
      ),
    ])
    const { registry } = registryWith(script.fetch)
    try {
      await registry.execute(healthRequest(id))
      throw new Error("expected a conflict response")
    } catch (error) {
      expect(error).toBeInstanceOf(HttpResponseError)
      expect(error).not.toBeInstanceOf(ApiVersionMismatchError)
      expect((error as HttpResponseError).category).toBe("conflict")
    }
  })

  test("rejects a successful response carrying a different API version", async () => {
    const id = requestId(6)
    const script = scriptedFetch([
      jsonResponse(
        { status: "ok", service: "zyra-api", phase: "m2", api_version: "2.0" },
        { requestId: id, apiVersion: "2.0" },
      ),
    ])
    const { registry } = registryWith(script.fetch)
    await expect(registry.execute(healthRequest(id))).rejects.toBeInstanceOf(ApiVersionMismatchError)
  })

  test("maps a disconnected fetch after bounded idempotent retries", async () => {
    const script = scriptedFetch([
      new TypeError("fetch failed: connection reset"),
      new TypeError("fetch failed: connection reset"),
    ])
    const { registry } = registryWith(script.fetch, { attempts: 2 })
    await expect(registry.execute(healthRequest(requestId(7)))).rejects.toBeInstanceOf(TransportDisconnectedError)
    expect(script.calls).toHaveLength(2)
  })

  test("times out a fetch and does not fabricate a response", async () => {
    const id = requestId(8)
    const hangingFetch = ((_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener(
          "abort",
          () => reject(new DOMException("The operation was aborted.", "AbortError")),
          { once: true },
        )
      })) as typeof fetch
    const { registry } = registryWith(hangingFetch)
    const request = factory().get(OPERATION_NAMES.health, CONTRACT_NAMES.health, "/health", {
      requestId: id,
      timeoutMs: 25,
      retry: false,
    })
    await expect(registry.execute(request)).rejects.toBeInstanceOf(RequestTimeoutError)
  })

  test("rejects malformed JSON even on HTTP 200", async () => {
    const id = requestId(9)
    const script = scriptedFetch([
      new Response("{not-json", {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "X-Zyra-Api-Version": "1.0",
          "X-Request-Id": id,
        },
      }),
    ])
    const { registry } = registryWith(script.fetch)
    await expect(registry.execute(healthRequest(id))).rejects.toBeInstanceOf(MalformedJsonError)
  })

  test("rejects a response with a mismatched request correlation id", async () => {
    const id = requestId(10)
    const script = scriptedFetch([
      jsonResponse(
        { status: "ok", service: "zyra-api", phase: "m2", api_version: "1.0" },
        { requestId: requestId(11) },
      ),
    ])
    const { registry } = registryWith(script.fetch)
    await expect(registry.execute(healthRequest(id))).rejects.toBeInstanceOf(ResponseValidationError)
  })

  test("normalizes and verifies committed and replayed receipts", () => {
    const id = requestId(12)
    const receiptId = createReceiptId()
    const taskId = "task_0123456789ab"
    const runId = "run_0123456789ab"
    const key = createIdempotencyKey("task.create", { taskId, runId }, { goal: "x" })
    const committed = normalizeReceipt(
      {
        receipt: {
          receipt_id: receiptId,
          request_id: id,
          idempotency_key: key,
          operation: "task.create",
          status: "committed",
          status_code: 201,
          replayed: false,
          committed_at: "2026-07-23T00:00:00.000Z",
          binding: { task_id: taskId, run_id: runId },
        },
      },
      new Headers({
        "X-Zyra-Receipt-Id": receiptId,
        "X-Zyra-Receipt-Replayed": "false",
      }),
      {
        requestId: id,
        idempotencyKey: key,
        operation: "task.create",
        statusCode: 201,
        binding: { taskId, runId },
      },
    )
    expect(committed.replayed).toBeFalse()
    const replayed = normalizeReceipt(
      {
        receipt: {
          ...committed,
          receipt_id: committed.receiptId,
          request_id: committed.requestId,
          idempotency_key: committed.idempotencyKey,
          status_code: committed.statusCode,
          committed_at: committed.committedAt,
          binding: { task_id: taskId, run_id: runId },
          status: "replayed",
          replayed: true,
        },
      },
      new Headers({
        "X-Zyra-Receipt-Id": receiptId,
        "X-Zyra-Receipt-Replayed": "true",
      }),
      {
        requestId: id,
        idempotencyKey: key,
        operation: "task.create",
        statusCode: 201,
        binding: { taskId, runId },
      },
    )
    expect(replayed.replayed).toBeTrue()
    expect(replayed.receiptId).toBe(committed.receiptId)
  })

  test("OMP-style correlation and cancellation target remain explicit", async () => {
    const id = requestId(13)
    const controller = new AbortController()
    const fetchImpl = ((_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener(
          "abort",
          () => reject(new DOMException("aborted", "AbortError")),
          { once: true },
        )
      })) as typeof fetch
    const { registry } = registryWith(fetchImpl)
    const pending = registry.execute(
      factory().get(OPERATION_NAMES.health, CONTRACT_NAMES.health, "/health", {
        requestId: id,
        signal: controller.signal,
        retry: false,
      }),
    )
    controller.abort({ type: "host_tool_cancel", id: "cancel-1", targetId: id })
    await expect(pending).rejects.toMatchObject({ code: "request_cancelled" })
  })

  test("circuit breaker opens after bounded transient failures and permits one later probe", () => {
    let now = 1_000
    const breaker = new TransportCircuitBreaker({
      failureThreshold: 2,
      cooldownMs: 100,
      now: () => now,
    })
    breaker.beforeRequest("health")
    breaker.failure(new TransportDisconnectedError("disconnect one"))
    expect(breaker.snapshot().state).toBe("closed")
    breaker.beforeRequest("health")
    breaker.failure(new TransportDisconnectedError("disconnect two"))
    expect(breaker.snapshot().state).toBe("open")
    expect(() => breaker.beforeRequest("health")).toThrow(TransportDisconnectedError)
    now += 100
    breaker.beforeRequest("health")
    expect(breaker.snapshot().state).toBe("half_open")
    breaker.success()
    expect(breaker.snapshot().state).toBe("closed")
  })

  test("request semaphore queues and releases without exceeding its transport limit", async () => {
    const semaphore = new RequestSemaphore(1)
    const first = await semaphore.acquire(
      new (await import("../src/cancellation.ts")).DeadlineBudget(1_000),
    )
    let secondAcquired = false
    const secondPromise = semaphore
      .acquire(new (await import("../src/cancellation.ts")).DeadlineBudget(1_000))
      .then((lease) => {
        secondAcquired = true
        return lease
      })
    await Promise.resolve()
    expect(secondAcquired).toBeFalse()
    expect(semaphore.snapshot().queued).toBe(1)
    first.release()
    const second = await secondPromise
    expect(secondAcquired).toBeTrue()
    expect(semaphore.snapshot().active).toBe(1)
    second.release()
    expect(semaphore.snapshot().active).toBe(0)
  })

  test("bounded polling identity window suppresses transport duplicates without owning event state", () => {
    const window = new BoundedIdentityWindow(3)
    const first = window.filter(
      [{ id: "event_1" }, { id: "event_2" }, { id: "event_1" }],
      (item) => item.id,
    )
    expect(first.map((item) => item.id)).toEqual(["event_1", "event_2"])
    const second = window.filter(
      [{ id: "event_2" }, { id: "event_3" }, { id: "event_4" }],
      (item) => item.id,
    )
    expect(second.map((item) => item.id)).toEqual(["event_3", "event_4"])
    expect(window.size).toBe(3)
    expect(window.has("event_1")).toBeFalse()
  })
})
