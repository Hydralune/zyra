import { AuthTokenManager, type AuthToken } from "./auth.ts"
import { CursorJournal } from "./cursor.ts"
import { coreProtocolCatalog, type EndpointDefinition, type ProtocolCatalog } from "./protocol.ts"
import { ReceiptJournal } from "./receipt.ts"
import { RequestCoordinator } from "./coordinator.ts"
import { RequestFactory, type ApiRequest, type PreparedRequest } from "./request.ts"
import { TransportRegistry, type RegistrySnapshot } from "./registry.ts"
import { registerCoreNormalizers } from "./normalizers.ts"
import { normalizeRetryPolicy, type RetryPolicy } from "./retry.ts"
import {
  FetchApiTransport,
  type StreamingResponseHandle,
} from "./transport.ts"
import { TransportTelemetry, type TransportTrace } from "./telemetry.ts"
import { normalizeVersionPolicy, type VersionPolicy } from "./version.ts"
import type { NormalizedApiResponse } from "./response.ts"

export interface ZyraTypedClientOptions {
  baseUrl: string
  token?: string | AuthToken
  tokenProvider?: () =>
    | string
    | AuthToken
    | undefined
    | null
    | Promise<string | AuthToken | undefined | null>
  version?: Partial<VersionPolicy>
  retry?: Partial<RetryPolicy>
  timeoutMs?: number
  fetch?: typeof fetch
  clientName?: string
  clientVersion?: string
  defaultHeaders?: HeadersInit
}

export interface TypedClientSnapshot {
  registry: RegistrySnapshot
  auth: ReturnType<AuthTokenManager["snapshot"]>
  cursors: ReturnType<CursorJournal["snapshot"]>
  receipts: ReturnType<ReceiptJournal["snapshot"]>
  inFlight: ReturnType<RequestCoordinator["snapshot"]>
  telemetry: ReturnType<TransportTelemetry["summary"]>
}

export function normalizeApiBaseUrl(value: string): string {
  const parsed = new URL(String(value || "").trim())
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new TypeError("Zyra API base URL must use http or https")
  }
  if (parsed.username || parsed.password) {
    throw new TypeError("Zyra API base URL must not contain credentials")
  }
  parsed.username = ""
  parsed.password = ""
  parsed.hash = ""
  parsed.search = ""
  parsed.pathname = parsed.pathname.replace(/\/+$/g, "")
  return parsed.toString().replace(/\/$/g, "")
}

/**
 * Shared typed HTTP client for non-browser product surfaces.
 *
 * The client owns transport concerns only. It never becomes a task, event,
 * scenario, permission, or daemon state owner.
 */
export class ZyraTypedApiClient {
  readonly #auth: AuthTokenManager
  readonly #transport: FetchApiTransport
  readonly #registry: TransportRegistry
  readonly #coordinator: RequestCoordinator
  readonly #factory: RequestFactory
  readonly #protocol: ProtocolCatalog
  readonly #telemetry: TransportTelemetry
  readonly #cursors = new CursorJournal()
  readonly #receipts = new ReceiptJournal()
  readonly #baseUrl: string
  #closed = false

  constructor(options: ZyraTypedClientOptions) {
    this.#baseUrl = normalizeApiBaseUrl(options.baseUrl)
    this.#auth = new AuthTokenManager({
      token: options.token,
      provider: options.tokenProvider,
    })
    this.#telemetry = new TransportTelemetry({ maximum: 8_192 })
    this.#transport = new FetchApiTransport({
      baseUrl: this.#baseUrl,
      fetch: options.fetch,
      auth: this.#auth.provider({ required: false }),
      version: normalizeVersionPolicy(options.version),
      retry: normalizeRetryPolicy(options.retry),
      telemetry: this.#telemetry,
      clientName: options.clientName ?? "zyra-typed-client",
      clientVersion: options.clientVersion ?? "0.1.0",
      defaultHeaders: options.defaultHeaders,
    })
    this.#registry = new TransportRegistry(this.#transport)
    registerCoreNormalizers(this.#registry.normalizers)
    this.#coordinator = new RequestCoordinator(this.#registry)
    this.#factory = new RequestFactory({
      defaultTimeoutMs: options.timeoutMs,
      metadata: {
        client: options.clientName ?? "zyra-typed-client",
        transport: "typed-fetch",
      },
    })
    this.#protocol = coreProtocolCatalog()
  }

  get baseUrl(): string {
    return this.#baseUrl
  }

  get protocol(): ProtocolCatalog {
    return this.#protocol
  }

  get cursors(): CursorJournal {
    return this.#cursors
  }

  get receipts(): ReceiptJournal {
    return this.#receipts
  }

  get telemetry(): TransportTelemetry {
    return this.#telemetry
  }

  get auth(): AuthTokenManager {
    return this.#auth
  }

  prepare<TBody>(request: ApiRequest<TBody>): PreparedRequest<TBody> {
    this.#assertOpen()
    return this.#factory.create(request)
  }

  execute<T>(
    request: PreparedRequest,
    options: {
      coordinationKey?: string
      deduplicate?: boolean
      latestWins?: boolean
    } = {},
  ): Promise<NormalizedApiResponse<T>> {
    this.#assertOpen()
    const key = options.coordinationKey
      ?? `${request.operation}|${request.method}|${request.path}|${JSON.stringify(request.query)}`
    return this.#coordinator.execute<T>(key, request, options)
  }

  request<T, TBody = unknown>(
    request: ApiRequest<TBody>,
    options: {
      coordinationKey?: string
      deduplicate?: boolean
      latestWins?: boolean
    } = {},
  ): Promise<NormalizedApiResponse<T>> {
    return this.execute<T>(this.prepare(request), options)
  }

  endpoint<T, TBody = unknown>(
    operation: string,
    options: {
      path?: Record<string, unknown>
      query?: Record<string, string | number | boolean | null | undefined>
      body?: TBody
      binding?: ApiRequest<TBody>["binding"]
      idempotencyKey?: string
      signal?: AbortSignal
      timeoutMs?: number
      headers?: HeadersInit
      correlationId?: string
      causationId?: string
      coordinationKey?: string
      deduplicate?: boolean
      latestWins?: boolean
    } = {},
  ): Promise<NormalizedApiResponse<T>> {
    this.#assertOpen()
    const endpoint = this.#protocol.resolve(operation, {
      path: options.path,
      query: options.query,
    })
    const request = this.#factory.create({
      operation: endpoint.operation,
      contract: endpoint.contract,
      method: endpoint.method,
      path: endpoint.path,
      query: endpoint.query,
      body: options.body,
      binding: options.binding,
      idempotencyKey: options.idempotencyKey,
      signal: options.signal,
      timeoutMs: options.timeoutMs,
      headers: options.headers,
      correlationId: options.correlationId,
      causationId: options.causationId,
      expectedStatuses: endpoint.expectedStatuses,
      retry: true,
    })
    return this.execute<T>(request, options)
  }

  openEndpointStream(
    operation: string,
    options: {
      path?: Record<string, unknown>
      query?: Record<string, string | number | boolean | null | undefined>
      binding?: ApiRequest["binding"]
      signal?: AbortSignal
      timeoutMs?: number
      headers?: HeadersInit
      correlationId?: string
      causationId?: string
    } = {},
  ): Promise<StreamingResponseHandle> {
    this.#assertOpen()
    const endpoint = this.#protocol.resolve(operation, {
      path: options.path,
      query: options.query,
    })
    if (endpoint.kind !== "stream") {
      throw new TypeError(`${operation} is not a streaming endpoint`)
    }
    const request = this.#factory.create({
      operation: endpoint.operation,
      contract: endpoint.contract,
      method: endpoint.method,
      path: endpoint.path,
      query: endpoint.query,
      binding: options.binding,
      signal: options.signal,
      timeoutMs: options.timeoutMs,
      headers: options.headers,
      correlationId: options.correlationId,
      causationId: options.causationId,
      expectedStatuses: endpoint.expectedStatuses,
      retry: false,
      metadata: { stream: true },
    })
    return this.#transport.openStream(request)
  }

  cancel(requestId: string, reason?: unknown): boolean {
    return this.#registry.cancel(requestId, reason)
  }

  cancelOperation(key: string, reason?: unknown): boolean {
    return this.#coordinator.cancel(key, reason)
  }

  setToken(token: string | AuthToken): void {
    this.#auth.set(token)
  }

  clearToken(): void {
    this.#auth.clear()
  }

  listen(listener: (trace: TransportTrace) => void): () => void {
    return this.#telemetry.listen(listener)
  }

  registerEndpoint(definition: EndpointDefinition): never {
    void definition
    throw new TypeError(
      "The core Zyra protocol catalog is frozen; extension clients must use a separate catalog",
    )
  }

  disableTransport(reason?: string): void {
    this.#registry.disable(reason)
  }

  disableNormalizers(): void {
    this.#registry.disableNormalizers()
  }

  snapshot(): TypedClientSnapshot {
    return {
      registry: this.#registry.snapshot(),
      auth: this.#auth.snapshot(),
      cursors: this.#cursors.snapshot(),
      receipts: this.#receipts.snapshot(),
      inFlight: this.#coordinator.snapshot(),
      telemetry: this.#telemetry.summary(),
    }
  }

  close(reason?: unknown): void {
    if (this.#closed) return
    this.#closed = true
    this.#coordinator.close(reason)
    this.#registry.disable(reason ? String(reason) : "Zyra API client closed.")
    this.#cursors.clear()
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Zyra API client is closed")
  }
}
