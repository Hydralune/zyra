import {
  MAX_RESPONSE_BODY_BYTES,
  ZYRA_CURSOR_HEADER,
  ZYRA_RECEIPT_ID_HEADER,
  ZYRA_RECEIPT_REPLAY_HEADER,
  isJsonMediaType,
} from "./constants.ts"
import {
  MalformedJsonError,
  ResponseValidationError,
  errorFromResponse,
  type ErrorContext,
} from "./errors.ts"
import { assertResponseCorrelation, booleanHeader, headerValue } from "./headers.ts"
import type { IdentityBinding } from "./identifiers.ts"
import { optionalIdentity } from "./identifiers.ts"
import type { PreparedRequest } from "./request.ts"
import { assertResponseVersion, type VersionPolicy } from "./version.ts"

export interface RawApiResponse {
  status: number
  statusText: string
  headers: Headers
  body: unknown
  text: string
  bytes: number
  requestId: string
  apiVersion: string
  cursor?: string
  receiptId?: string
  replayed?: boolean
  receivedAt: number
  elapsedMs: number
}

export interface NormalizedApiResponse<T> {
  data: T
  raw: RawApiResponse
  binding: IdentityBinding
}

export type ResponseNormalizer<T> = (
  body: unknown,
  context: {
    request: PreparedRequest
    raw: RawApiResponse
  },
) => T

async function responseText(response: Response): Promise<{ text: string; bytes: number }> {
  const declared = response.headers.get("content-length")
  if (declared) {
    const size = Number(declared)
    if (Number.isFinite(size) && size > MAX_RESPONSE_BODY_BYTES) {
      throw new ResponseValidationError(`Response exceeds ${MAX_RESPONSE_BODY_BYTES} bytes.`, {
        content_length: size,
      })
    }
  }
  const text = await response.text()
  const bytes = new TextEncoder().encode(text).byteLength
  if (bytes > MAX_RESPONSE_BODY_BYTES) {
    throw new ResponseValidationError(`Response exceeds ${MAX_RESPONSE_BODY_BYTES} bytes.`, {
      response_bytes: bytes,
    })
  }
  return { text, bytes }
}

function parseResponseBody(text: string, contentType: string | null, context: ErrorContext): unknown {
  if (!text) return null
  if (!isJsonMediaType(contentType)) {
    throw new ResponseValidationError("Zyra API response must use a JSON media type.", {
      content_type: contentType,
      body_preview: text.slice(0, 512),
    }, context)
  }
  try {
    return JSON.parse(text)
  } catch (error) {
    throw new MalformedJsonError("Zyra API response contains malformed JSON.", context, error)
  }
}

export async function consumeResponse(
  response: Response,
  request: PreparedRequest,
  versionPolicy: VersionPolicy,
  startedAt: number,
  now = Date.now,
): Promise<RawApiResponse> {
  const context: ErrorContext = {
    operation: request.operation,
    method: request.method,
    requestId: request.requestId,
    status: response.status,
    binding: request.binding,
  }
  if (!request.expectedStatuses.includes(response.status)) {
    throw await errorFromResponse(response, context)
  }
  let requestId: string
  try {
    requestId = assertResponseCorrelation(response.headers, request.requestId)
  } catch (error) {
    throw new ResponseValidationError(
      "Zyra API response request correlation does not match.",
      {
        expected_request_id: request.requestId,
        actual_request_id: response.headers.get("X-Request-Id"),
      },
      context,
      error,
    )
  }
  const apiVersion = assertResponseVersion(response.headers, versionPolicy, {
    operation: request.operation,
    requestId,
  })
  const receivedAt = now()
  const { text, bytes } = await responseText(response)
  const body = response.status === 204 ? null : parseResponseBody(text, response.headers.get("content-type"), context)
  const receiptValue = headerValue(response.headers, ZYRA_RECEIPT_ID_HEADER)
  const receiptId = receiptValue ? optionalIdentity("receipt", receiptValue) : undefined
  return {
    status: response.status,
    statusText: response.statusText,
    headers: new Headers(response.headers),
    body,
    text,
    bytes,
    requestId,
    apiVersion,
    cursor: headerValue(response.headers, ZYRA_CURSOR_HEADER),
    receiptId,
    replayed: booleanHeader(response.headers, ZYRA_RECEIPT_REPLAY_HEADER),
    receivedAt,
    elapsedMs: Math.max(0, receivedAt - startedAt),
  }
}

export function normalizeResponse<T>(
  raw: RawApiResponse,
  request: PreparedRequest,
  normalizer: ResponseNormalizer<T>,
): NormalizedApiResponse<T> {
  let data: T
  try {
    data = normalizer(raw.body, { request, raw })
  } catch (error) {
    if (error instanceof ResponseValidationError) throw error
    throw new ResponseValidationError(
      `Response normalization failed for ${request.contract}.`,
      { contract: request.contract },
      {
        operation: request.operation,
        method: request.method,
        requestId: request.requestId,
        status: raw.status,
        binding: request.binding,
      },
      error,
    )
  }
  return {
    data,
    raw,
    binding: { ...request.binding },
  }
}

export function objectBody(value: unknown, label = "response body"): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ResponseValidationError(`${label} must be an object.`)
  }
  return value as Record<string, unknown>
}

export function arrayBody(value: unknown, label = "response body"): unknown[] {
  if (!Array.isArray(value)) throw new ResponseValidationError(`${label} must be an array.`)
  return value
}

export function responseString(value: unknown, label: string, options: { allowEmpty?: boolean } = {}): string {
  if (typeof value !== "string") throw new ResponseValidationError(`${label} must be a string.`)
  if (!options.allowEmpty && !value.trim()) throw new ResponseValidationError(`${label} must not be empty.`)
  return value
}

export function responseNumber(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new ResponseValidationError(`${label} must be a finite number.`)
  }
  return value
}

export function responseInteger(value: unknown, label: string): number {
  const result = responseNumber(value, label)
  if (!Number.isSafeInteger(result)) throw new ResponseValidationError(`${label} must be an integer.`)
  return result
}

export function responseBoolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") throw new ResponseValidationError(`${label} must be a boolean.`)
  return value
}

export function responseRecord(value: unknown, label: string): Record<string, unknown> {
  return objectBody(value, label)
}

export function optionalResponseRecord(value: unknown, label: string): Record<string, unknown> | undefined {
  if (value === undefined || value === null) return undefined
  return responseRecord(value, label)
}

export function optionalResponseString(value: unknown, label: string): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return responseString(value, label)
}

export function responseArray<T>(
  value: unknown,
  label: string,
  normalize: (entry: unknown, index: number) => T,
): T[] {
  return arrayBody(value, label).map((entry, index) => normalize(entry, index))
}

export class NormalizerRegistry {
  readonly #normalizers = new Map<string, ResponseNormalizer<unknown>>()
  #enabled = true

  register<T>(contract: string, normalizer: ResponseNormalizer<T>, options: { replace?: boolean } = {}): void {
    if (!contract.trim()) throw new TypeError("Normalizer contract must not be empty")
    if (this.#normalizers.has(contract) && !options.replace) {
      throw new TypeError(`Normalizer already registered: ${contract}`)
    }
    this.#normalizers.set(contract, normalizer as ResponseNormalizer<unknown>)
  }

  unregister(contract: string): boolean {
    return this.#normalizers.delete(contract)
  }

  get<T>(contract: string): ResponseNormalizer<T> | undefined {
    if (!this.#enabled) return undefined
    return this.#normalizers.get(contract) as ResponseNormalizer<T> | undefined
  }

  require<T>(contract: string): ResponseNormalizer<T> {
    if (!this.#enabled) throw new TypeError(`Response normalizers are disabled: ${contract}`)
    const normalizer = this.get<T>(contract)
    if (!normalizer) throw new TypeError(`No response normalizer is registered for ${contract}`)
    return normalizer
  }

  disable(): void {
    this.#enabled = false
  }

  enable(): void {
    this.#enabled = true
  }

  clear(): void {
    this.#normalizers.clear()
  }

  contracts(): string[] {
    return [...this.#normalizers.keys()].sort()
  }

  get enabled(): boolean {
    return this.#enabled
  }
}
