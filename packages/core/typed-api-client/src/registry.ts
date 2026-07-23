import { NormalizerDisabledError, TransportDisabledError } from "./errors.ts"
import type { PreparedRequest } from "./request.ts"
import {
  NormalizerRegistry,
  type NormalizedApiResponse,
  type ResponseNormalizer,
} from "./response.ts"
import type { ApiTransport } from "./transport.ts"

export interface RegistrySnapshot {
  enabled: boolean
  transport?: string
  transportEnabled: boolean
  normalizersEnabled: boolean
  contracts: string[]
  inFlight: string[]
}

export class TransportRegistry {
  readonly #normalizers = new NormalizerRegistry()
  readonly #inflight = new Map<string, Promise<NormalizedApiResponse<unknown>>>()
  #transport?: ApiTransport
  #enabled = true
  #generation = 0

  constructor(transport?: ApiTransport) {
    if (transport) this.registerTransport(transport)
  }

  registerTransport(transport: ApiTransport, options: { replace?: boolean } = {}): void {
    if (this.#transport && !options.replace) {
      throw new TypeError(`A transport is already registered: ${this.#transport.name}`)
    }
    if (this.#transport && options.replace) {
      void this.#transport.close("Transport replaced.")
    }
    this.#transport = transport
    this.#generation += 1
  }

  unregisterTransport(reason = "Transport unregistered."): boolean {
    if (!this.#transport) return false
    const current = this.#transport
    this.#transport = undefined
    this.#generation += 1
    void current.close(reason)
    return true
  }

  registerNormalizer<T>(
    contract: string,
    normalizer: ResponseNormalizer<T>,
    options: { replace?: boolean } = {},
  ): void {
    this.#normalizers.register(contract, normalizer, options)
  }

  unregisterNormalizer(contract: string): boolean {
    return this.#normalizers.unregister(contract)
  }

  async execute<T>(request: PreparedRequest): Promise<NormalizedApiResponse<T>> {
    if (!this.#enabled || !this.#transport || !this.#transport.enabled) {
      throw new TransportDisabledError({
        operation: request.operation,
        method: request.method,
        requestId: request.requestId,
        binding: request.binding,
      })
    }
    if (!this.#normalizers.enabled) {
      throw new NormalizerDisabledError(request.contract, {
        operation: request.operation,
        method: request.method,
        requestId: request.requestId,
        binding: request.binding,
      })
    }
    const normalizer = this.#normalizers.get<T>(request.contract)
    if (!normalizer) {
      throw new NormalizerDisabledError(request.contract, {
        operation: request.operation,
        method: request.method,
        requestId: request.requestId,
        binding: request.binding,
      })
    }
    const existing = this.#inflight.get(request.requestId)
    if (existing) return existing as Promise<NormalizedApiResponse<T>>
    const generation = this.#generation
    const promise = this.#transport.send(request, normalizer)
    this.#inflight.set(request.requestId, promise as Promise<NormalizedApiResponse<unknown>>)
    try {
      const result = await promise
      if (!this.#enabled || generation !== this.#generation) {
        throw new TransportDisabledError({
          operation: request.operation,
          method: request.method,
          requestId: request.requestId,
          binding: request.binding,
        })
      }
      return result
    } finally {
      if (this.#inflight.get(request.requestId) === promise) this.#inflight.delete(request.requestId)
    }
  }

  cancel(requestId: string, reason?: unknown): boolean {
    return this.#transport?.cancel(requestId, reason) ?? false
  }

  disable(reason = "Transport registry disabled."): void {
    if (!this.#enabled) return
    this.#enabled = false
    this.#generation += 1
    for (const requestId of this.#inflight.keys()) this.#transport?.cancel(requestId, reason)
    void this.#transport?.close(reason)
  }

  enable(): void {
    this.#enabled = true
    this.#generation += 1
  }

  disableNormalizers(): void {
    this.#normalizers.disable()
    this.#generation += 1
    for (const requestId of this.#inflight.keys()) {
      this.#transport?.cancel(requestId, "Response normalizers disabled.")
    }
  }

  enableNormalizers(): void {
    this.#normalizers.enable()
    this.#generation += 1
  }

  snapshot(): RegistrySnapshot {
    return {
      enabled: this.#enabled,
      transport: this.#transport?.name,
      transportEnabled: this.#transport?.enabled ?? false,
      normalizersEnabled: this.#normalizers.enabled,
      contracts: this.#normalizers.contracts(),
      inFlight: [...this.#inflight.keys()].sort(),
    }
  }

  get transport(): ApiTransport | undefined {
    return this.#transport
  }

  get normalizers(): NormalizerRegistry {
    return this.#normalizers
  }
}
