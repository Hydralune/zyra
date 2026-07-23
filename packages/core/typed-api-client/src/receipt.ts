import {
  MAX_RECEIPT_HISTORY,
  ZYRA_RECEIPT_ID_HEADER,
  ZYRA_RECEIPT_REPLAY_HEADER,
} from "./constants.ts"
import { ReceiptError, ResponseValidationError } from "./errors.ts"
import { booleanHeader } from "./headers.ts"
import {
  type IdentityBinding,
  bindIdentities,
  bindingEquals,
  bindingFromUnknown,
  bindingToSnakeCase,
  createReceiptId,
  normalizeIdempotencyKey,
  normalizeIdentity,
  validateBinding,
} from "./identifiers.ts"

export type ReceiptStatus = "committed" | "replayed" | "rejected"

export interface MutationReceipt {
  receiptId: string
  requestId: string
  idempotencyKey: string
  operation: string
  status: ReceiptStatus
  statusCode: number
  replayed: boolean
  committedAt: string
  binding: IdentityBinding
  responseDigest?: string
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ResponseValidationError(`${label} must be an object.`)
  }
  return value as Record<string, unknown>
}

function requiredString(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new ResponseValidationError(`${label} must be a non-empty string.`)
  }
  return value.trim()
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined
}

function integer(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) {
    throw new ResponseValidationError(`${label} must be an integer.`)
  }
  return value
}

function boolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") throw new ResponseValidationError(`${label} must be a boolean.`)
  return value
}

function receiptCandidate(body: unknown): Record<string, unknown> {
  const envelope = record(body, "mutation response")
  const nested = envelope.receipt
  if (nested && typeof nested === "object" && !Array.isArray(nested)) {
    return nested as Record<string, unknown>
  }
  return envelope
}

export function normalizeReceipt(
  body: unknown,
  headers: Headers,
  expected: {
    requestId: string
    idempotencyKey: string
    operation: string
    statusCode: number
    binding?: IdentityBinding
  },
): MutationReceipt {
  const candidate = receiptCandidate(body)
  const headerReceiptId = headers.get(ZYRA_RECEIPT_ID_HEADER) ?? undefined
  const receiptId = normalizeIdentity(
    "receipt",
    optionalString(candidate.receipt_id ?? candidate.receiptId) ?? headerReceiptId,
  )
  const requestId = normalizeIdentity(
    "request",
    optionalString(candidate.request_id ?? candidate.requestId) ?? expected.requestId,
  )
  const idempotencyKey = normalizeIdempotencyKey(
    optionalString(candidate.idempotency_key ?? candidate.idempotencyKey) ?? expected.idempotencyKey,
  )
  const operation = requiredString(candidate.operation ?? expected.operation, "receipt operation")
  const statusCode =
    candidate.status_code === undefined && candidate.statusCode === undefined
      ? expected.statusCode
      : integer(candidate.status_code ?? candidate.statusCode, "receipt status code")
  const replayHeader = booleanHeader(headers, ZYRA_RECEIPT_REPLAY_HEADER)
  const replayed =
    candidate.replayed === undefined
      ? (replayHeader ?? false)
      : boolean(candidate.replayed, "receipt replayed")
  const rawStatus = optionalString(candidate.status)
  const status: ReceiptStatus =
    rawStatus === "committed" || rawStatus === "replayed" || rawStatus === "rejected"
      ? rawStatus
      : replayed
        ? "replayed"
        : "committed"
  const committedAt = requiredString(
    candidate.committed_at ?? candidate.committedAt ?? new Date().toISOString(),
    "receipt committed time",
  )
  if (Number.isNaN(Date.parse(committedAt))) {
    throw new ResponseValidationError("Receipt committed time must be ISO-8601.")
  }
  const candidateBinding =
    candidate.binding && typeof candidate.binding === "object"
      ? bindingFromUnknown(candidate.binding)
      : bindingFromUnknown(candidate)
  const binding = bindIdentities(expected.binding ?? {}, candidateBinding)
  const receipt: MutationReceipt = {
    receiptId,
    requestId,
    idempotencyKey,
    operation,
    status,
    statusCode,
    replayed,
    committedAt,
    binding,
    responseDigest: optionalString(candidate.response_digest ?? candidate.responseDigest),
  }
  assertReceipt(receipt, expected)
  return receipt
}

export function assertReceipt(
  receipt: MutationReceipt,
  expected: {
    requestId: string
    idempotencyKey: string
    operation: string
    statusCode?: number
    binding?: IdentityBinding
  },
): void {
  if (receipt.requestId !== expected.requestId) {
    throw new ReceiptError("receipt_mismatch", "Receipt request id does not match the request.", {
      expected_request_id: expected.requestId,
      actual_request_id: receipt.requestId,
    })
  }
  if (receipt.idempotencyKey !== expected.idempotencyKey) {
    throw new ReceiptError("receipt_mismatch", "Receipt idempotency key does not match the request.", {
      expected_idempotency_key: expected.idempotencyKey,
      actual_idempotency_key: receipt.idempotencyKey,
    })
  }
  if (receipt.operation !== expected.operation) {
    throw new ReceiptError("receipt_mismatch", "Receipt operation does not match the request.", {
      expected_operation: expected.operation,
      actual_operation: receipt.operation,
    })
  }
  if (expected.statusCode !== undefined && receipt.statusCode !== expected.statusCode) {
    throw new ReceiptError("receipt_mismatch", "Receipt status code does not match the response.", {
      expected_status_code: expected.statusCode,
      actual_status_code: receipt.statusCode,
    })
  }
  if (expected.binding && !bindingEquals(validateBinding(expected.binding), receipt.binding)) {
    throw new ReceiptError("receipt_mismatch", "Receipt identity binding does not match the request.", {
      expected_binding: bindingToSnakeCase(expected.binding),
      actual_binding: bindingToSnakeCase(receipt.binding),
    })
  }
  if (receipt.status === "rejected") {
    throw new ReceiptError("receipt_conflict", "Server rejected the mutation receipt.", {
      receipt_id: receipt.receiptId,
      operation: receipt.operation,
    })
  }
}

export function syntheticReceipt(input: {
  requestId: string
  idempotencyKey: string
  operation: string
  statusCode: number
  binding?: IdentityBinding
  replayed?: boolean
  responseDigest?: string
}): MutationReceipt {
  return {
    receiptId: createReceiptId(),
    requestId: normalizeIdentity("request", input.requestId),
    idempotencyKey: normalizeIdempotencyKey(input.idempotencyKey),
    operation: requiredString(input.operation, "receipt operation"),
    status: input.replayed ? "replayed" : "committed",
    statusCode: integer(input.statusCode, "receipt status code"),
    replayed: input.replayed ?? false,
    committedAt: new Date().toISOString(),
    binding: validateBinding(input.binding ?? {}),
    responseDigest: input.responseDigest,
  }
}

export class ReceiptJournal {
  readonly #maximum: number
  readonly #byReceipt = new Map<string, MutationReceipt>()
  readonly #byIdempotency = new Map<string, string>()
  readonly #byRequest = new Map<string, string>()

  constructor(maximum = MAX_RECEIPT_HISTORY) {
    if (!Number.isSafeInteger(maximum) || maximum < 1) throw new TypeError("Receipt journal maximum must be positive")
    this.#maximum = maximum
  }

  remember(receipt: MutationReceipt): MutationReceipt {
    assertReceipt(receipt, {
      requestId: receipt.requestId,
      idempotencyKey: receipt.idempotencyKey,
      operation: receipt.operation,
      statusCode: receipt.statusCode,
      binding: receipt.binding,
    })
    const existingId = this.#byIdempotency.get(receipt.idempotencyKey)
    if (existingId) {
      const existing = this.#byReceipt.get(existingId)
      if (
        existing &&
        (existing.operation !== receipt.operation ||
          !bindingEquals(existing.binding, receipt.binding) ||
          existing.responseDigest !== receipt.responseDigest)
      ) {
        throw new ReceiptError("receipt_conflict", "Idempotency key resolved to conflicting receipts.", {
          idempotency_key: receipt.idempotencyKey,
          previous_receipt_id: existing.receiptId,
          next_receipt_id: receipt.receiptId,
        })
      }
    }
    this.#byReceipt.delete(receipt.receiptId)
    this.#byReceipt.set(receipt.receiptId, cloneReceipt(receipt))
    this.#byIdempotency.set(receipt.idempotencyKey, receipt.receiptId)
    this.#byRequest.set(receipt.requestId, receipt.receiptId)
    this.#prune()
    return cloneReceipt(receipt)
  }

  byReceipt(receiptId: string): MutationReceipt | undefined {
    const value = this.#byReceipt.get(normalizeIdentity("receipt", receiptId))
    return value ? cloneReceipt(value) : undefined
  }

  byIdempotency(idempotencyKey: string): MutationReceipt | undefined {
    const receiptId = this.#byIdempotency.get(normalizeIdempotencyKey(idempotencyKey))
    return receiptId ? this.byReceipt(receiptId) : undefined
  }

  byRequest(requestId: string): MutationReceipt | undefined {
    const receiptId = this.#byRequest.get(normalizeIdentity("request", requestId))
    return receiptId ? this.byReceipt(receiptId) : undefined
  }

  replayed(idempotencyKey: string): boolean {
    return this.byIdempotency(idempotencyKey)?.replayed === true
  }

  forget(receiptId: string): boolean {
    const normalized = normalizeIdentity("receipt", receiptId)
    const receipt = this.#byReceipt.get(normalized)
    if (!receipt) return false
    this.#byReceipt.delete(normalized)
    if (this.#byIdempotency.get(receipt.idempotencyKey) === normalized) {
      this.#byIdempotency.delete(receipt.idempotencyKey)
    }
    if (this.#byRequest.get(receipt.requestId) === normalized) {
      this.#byRequest.delete(receipt.requestId)
    }
    return true
  }

  clear(): void {
    this.#byReceipt.clear()
    this.#byIdempotency.clear()
    this.#byRequest.clear()
  }

  snapshot(): MutationReceipt[] {
    return [...this.#byReceipt.values()].map(cloneReceipt)
  }

  get size(): number {
    return this.#byReceipt.size
  }

  #prune(): void {
    while (this.#byReceipt.size > this.#maximum) {
      const oldest = this.#byReceipt.keys().next().value
      if (!oldest) return
      this.forget(oldest)
    }
  }
}

function cloneReceipt(receipt: MutationReceipt): MutationReceipt {
  return { ...receipt, binding: { ...receipt.binding } }
}
