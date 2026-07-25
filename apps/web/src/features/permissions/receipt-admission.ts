import {
  canonicalPermissionJson,
  identifier,
  safePermissionClone,
  stablePermissionId,
} from "./canonical.ts"
import {
  PERMISSION_CANONICAL_OWNER,
  type PermissionDecisionReceipt,
  type PermissionRequestProjection,
  type PermissionResponseProof,
} from "./contracts.ts"

export interface PermissionReceiptAdmission {
  admissionId: string
  receipt: PermissionDecisionReceipt
  requestId: string
  responseId: string
  permitId?: string
  argumentsDigest: string
  receiptDigest: string
  replayed: boolean
  admittedAt: string
}

export interface PermissionReceiptLedgerSnapshot {
  schema: "zyra.permission-receipt-ledger/v1"
  admissions: readonly PermissionReceiptAdmission[]
  settledRequestIds: readonly string[]
  permitIds: readonly string[]
  conflictCount: number
  rejectedCount: number
  replayCount: number
  proofRequired: true
  ownsPermissionState: false
  revision: number
}

export class PermissionReceiptLedger {
  readonly #admissions = new Map<string, PermissionReceiptAdmission>()
  readonly #settledByRequest = new Map<string, string>()
  readonly #permitToRequest = new Map<string, string>()
  readonly #now: () => Date
  readonly #maximum: number
  #conflictCount = 0
  #rejectedCount = 0
  #replayCount = 0
  #revision = 0
  #disabled = false

  constructor(options: {
    now?: () => Date
    maximum?: number
  } = {}) {
    this.#now = options.now ?? (() => new Date())
    this.#maximum = Math.min(
      10_000,
      Math.max(16, Math.floor(options.maximum ?? 512)),
    )
  }

  admit(input: {
    receipt: PermissionDecisionReceipt
    request: PermissionRequestProjection
    proof: PermissionResponseProof
  }): PermissionReceiptAdmission {
    this.#assertEnabled()
    const receipt = input.receipt
    const request = input.request
    const proof = input.proof
    try {
      this.#validateExact(receipt, request, proof)
      const receiptDigest = receiptFingerprint(receipt)
      const prior = this.#admissions.get(receipt.responseId)
      if (prior) {
        if (
          prior.receiptDigest !== receiptDigest
          || prior.requestId !== receipt.requestId
          || prior.argumentsDigest !== request.argumentsDigest
        ) {
          this.#conflictCount += 1
          throw admissionError(
            "permission_receipt_response_conflict",
            "The same response id was projected with different canonical receipt fields.",
          )
        }
        this.#replayCount += 1
        this.#revision += 1
        return freezeAdmission({
          ...prior,
          replayed: true,
          receipt: Object.freeze({ ...receipt, replayed: true }),
        })
      }
      const settledResponse = this.#settledByRequest.get(request.requestId)
      if (settledResponse && settledResponse !== receipt.responseId) {
        this.#conflictCount += 1
        throw admissionError(
          "permission_receipt_request_already_settled",
          `Request ${request.requestId} already settled through response ${settledResponse}.`,
        )
      }
      if (receipt.permitId) {
        const permitRequest = this.#permitToRequest.get(receipt.permitId)
        if (permitRequest && permitRequest !== request.requestId) {
          this.#conflictCount += 1
          throw admissionError(
            "permission_receipt_permit_reused",
            `Permit ${receipt.permitId} is already bound to another permission request.`,
          )
        }
      }
      const admission = freezeAdmission({
        admissionId: stablePermissionId(
          "permission_receipt_admission",
          receipt.responseId,
          receiptDigest,
        ),
        receipt: Object.freeze(safePermissionClone(receipt)),
        requestId: request.requestId,
        responseId: receipt.responseId,
        permitId: receipt.permitId,
        argumentsDigest: request.argumentsDigest,
        receiptDigest,
        replayed: receipt.replayed,
        admittedAt: this.#now().toISOString(),
      })
      this.#admissions.set(receipt.responseId, admission)
      this.#settledByRequest.set(request.requestId, receipt.responseId)
      if (receipt.permitId) {
        this.#permitToRequest.set(receipt.permitId, request.requestId)
      }
      this.#revision += 1
      this.#prune()
      return freezeAdmission(admission)
    } catch (error) {
      this.#rejectedCount += 1
      this.#revision += 1
      throw error
    }
  }

  get(responseIdValue: string): PermissionReceiptAdmission | undefined {
    const responseId = identifier(
      responseIdValue,
      "permission response id",
    )
    const admission = this.#admissions.get(responseId)
    return admission ? freezeAdmission(admission) : undefined
  }

  settled(requestIdValue: string): boolean {
    const requestId = identifier(
      requestIdValue,
      "permission request id",
    )
    return this.#settledByRequest.has(requestId)
  }

  snapshot(): PermissionReceiptLedgerSnapshot {
    return Object.freeze({
      schema: "zyra.permission-receipt-ledger/v1",
      admissions: Object.freeze(
        [...this.#admissions.values()]
          .sort(
            (left, right) =>
              left.admittedAt.localeCompare(right.admittedAt)
              || left.admissionId.localeCompare(right.admissionId),
          )
          .map(freezeAdmission),
      ),
      settledRequestIds: Object.freeze(
        [...this.#settledByRequest.keys()].sort(),
      ),
      permitIds: Object.freeze([...this.#permitToRequest.keys()].sort()),
      conflictCount: this.#conflictCount,
      rejectedCount: this.#rejectedCount,
      replayCount: this.#replayCount,
      proofRequired: true,
      ownsPermissionState: false,
      revision: this.#revision,
    })
  }

  disable(): void {
    this.#disabled = true
    this.#revision += 1
  }

  clear(): void {
    this.#admissions.clear()
    this.#settledByRequest.clear()
    this.#permitToRequest.clear()
    this.#conflictCount = 0
    this.#rejectedCount = 0
    this.#replayCount = 0
    this.#disabled = false
    this.#revision += 1
  }

  #validateExact(
    receipt: PermissionDecisionReceipt,
    request: PermissionRequestProjection,
    proof: PermissionResponseProof,
  ): void {
    if (
      receipt.requestId !== request.requestId
      || receipt.requestId !== proof.request_id
      || receipt.responseId !== proof.response_id
      || receipt.effect !== proof.effect
    ) {
      throw admissionError(
        "permission_receipt_identity_mismatch",
        "Canonical receipt does not match the request and response proof identity.",
      )
    }
    if (receipt.canonicalOwner !== PERMISSION_CANONICAL_OWNER) {
      throw admissionError(
        "permission_receipt_owner_mismatch",
        "Permission receipt was not issued by the canonical TypeScript owner.",
      )
    }
    if (!receipt.accepted || !receipt.resumed) {
      throw admissionError(
        "permission_receipt_not_resumed",
        "Permission receipt did not prove an accepted continuation.",
      )
    }
    if (!receipt.responseProofVerified) {
      throw admissionError(
        "permission_receipt_proof_unverified",
        "Canonical owner did not verify the console response proof.",
      )
    }
    if (receipt.responseProofDigest !== proof.proof) {
      throw admissionError(
        "permission_receipt_proof_digest_mismatch",
        "Canonical receipt response-proof digest does not match the submitted proof.",
      )
    }
    if (
      receipt.responseChallengeDigest
      !== request.responseChallenge.challengeDigest
    ) {
      throw admissionError(
        "permission_receipt_challenge_mismatch",
        "Canonical receipt belongs to a different response challenge.",
      )
    }
    if (receipt.finalArgumentsDigest !== request.argumentsDigest) {
      throw admissionError(
        "permission_receipt_arguments_mismatch",
        "Canonical receipt changed the exact arguments digest.",
      )
    }
    if (receipt.effect === "allow" && !receipt.permitId) {
      throw admissionError(
        "permission_receipt_permit_missing",
        "Allowed permission response did not issue an exact-call permit.",
      )
    }
    if (receipt.effect === "deny" && receipt.permitId) {
      throw admissionError(
        "permission_receipt_deny_permit_invalid",
        "Denied permission response unexpectedly issued a permit.",
      )
    }
  }

  #prune(): void {
    if (this.#admissions.size <= this.#maximum) return
    const ordered = this.snapshot().admissions
    for (const admission of ordered) {
      if (this.#admissions.size <= this.#maximum) break
      this.#admissions.delete(admission.responseId)
      if (
        this.#settledByRequest.get(admission.requestId)
        === admission.responseId
      ) {
        this.#settledByRequest.delete(admission.requestId)
      }
      if (
        admission.permitId
        && this.#permitToRequest.get(admission.permitId)
          === admission.requestId
      ) {
        this.#permitToRequest.delete(admission.permitId)
      }
    }
  }

  #assertEnabled(): void {
    if (this.#disabled) {
      throw admissionError(
        "permission_receipt_admission_disabled",
        "Permission receipt admission is disabled.",
      )
    }
  }
}

function receiptFingerprint(
  receipt: PermissionDecisionReceipt,
): string {
  return stablePermissionId(
    "permission_receipt_digest",
    canonicalPermissionJson({
      request_id: receipt.requestId,
      response_id: receipt.responseId,
      effect: receipt.effect,
      accepted: receipt.accepted,
      response_proof_verified: receipt.responseProofVerified,
      response_proof_digest: receipt.responseProofDigest,
      response_challenge_digest: receipt.responseChallengeDigest,
      permit_id: receipt.permitId,
      decision_id: receipt.decisionId,
      final_arguments_digest: receipt.finalArgumentsDigest,
      state_digest: receipt.stateDigest,
      canonical_owner: receipt.canonicalOwner,
      resumed: receipt.resumed,
    }),
  )
}

function freezeAdmission(
  value: PermissionReceiptAdmission,
): PermissionReceiptAdmission {
  return Object.freeze({
    ...value,
    receipt: Object.freeze(safePermissionClone(value.receipt)),
  })
}

function admissionError(code: string, message: string): Error {
  return Object.assign(new Error(message), {
    name: "PermissionReceiptAdmissionError",
    code,
  })
}
