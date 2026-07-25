import {
  comparePermissionTime,
  freshPermissionId,
  identifier,
  safePermissionClone,
} from "./canonical.ts"
import type {
  PermissionDecisionReceipt,
  PermissionResponseAttempt,
  PermissionResponseEffect,
} from "./contracts.ts"

export interface PermissionResponseClaim {
  readonly attempt: PermissionResponseAttempt
  readonly won: boolean
  readonly activeResponseId?: string
}

export class PermissionResponseRace {
  readonly #attempts = new Map<string, PermissionResponseAttempt>()
  readonly #activeByRequest = new Map<string, string>()
  readonly #responseToRequest = new Map<string, string>()
  readonly #now: () => Date
  readonly #maximum: number

  constructor(options: { now?: () => Date; maximum?: number } = {}) {
    this.#now = options.now ?? (() => new Date())
    this.#maximum = Math.min(
      10_000,
      Math.max(16, Math.floor(options.maximum ?? 512)),
    )
  }

  create(input: {
    requestId: string
    responseId: string
    effect: PermissionResponseEffect
    source: PermissionResponseAttempt["source"]
    displayResponder: string
  }): PermissionResponseAttempt {
    const requestId = identifier(input.requestId, "permission request id")
    const responseId = identifier(input.responseId, "permission response id")
    const priorRequest = this.#responseToRequest.get(responseId)
    if (priorRequest && priorRequest !== requestId) {
      throw raceError(
        "permission_response_id_reused",
        `Response id ${responseId} is already bound to another request.`,
      )
    }
    const timestamp = this.#now().toISOString()
    const attemptId = freshPermissionId(
      "permission_response_attempt",
      this.#now(),
    )
    const attempt = freezeAttempt({
      attemptId,
      requestId,
      responseId,
      effect: input.effect,
      source: input.source,
      displayResponder: input.displayResponder,
      phase: "created",
      createdAt: timestamp,
      updatedAt: timestamp,
    })
    this.#attempts.set(attemptId, attempt)
    this.#responseToRequest.set(responseId, requestId)
    this.#prune()
    return freezeAttempt(attempt)
  }

  claim(attemptIdValue: string): PermissionResponseClaim {
    const attempt = this.#require(attemptIdValue)
    if (terminal(attempt.phase)) {
      return {
        attempt: freezeAttempt(attempt),
        won: attempt.phase === "accepted",
        activeResponseId: this.activeResponseId(attempt.requestId),
      }
    }
    const activeAttemptId = this.#activeByRequest.get(attempt.requestId)
    if (activeAttemptId && activeAttemptId !== attempt.attemptId) {
      const active = this.#attempts.get(activeAttemptId)
      const lost = this.#transition(
        attempt,
        "lost_race",
        "permission_response_race_lost",
        active
          ? `Response ${active.responseId} already claimed this request.`
          : "Another response already claimed this request.",
      )
      return {
        attempt: lost,
        won: false,
        activeResponseId: active?.responseId,
      }
    }
    if (
      attempt.phase !== "created"
      && attempt.phase !== "proof_ready"
    ) {
      throw raceError(
        "permission_response_claim_phase_invalid",
        `Cannot claim response attempt in phase ${attempt.phase}.`,
      )
    }
    this.#activeByRequest.set(attempt.requestId, attempt.attemptId)
    return {
      attempt: this.#transition(attempt, "claimed"),
      won: true,
      activeResponseId: attempt.responseId,
    }
  }

  proofReady(attemptIdValue: string): PermissionResponseAttempt {
    const attempt = this.#require(attemptIdValue)
    if (attempt.phase !== "claimed" && attempt.phase !== "created") {
      throw raceError(
        "permission_response_proof_phase_invalid",
        `Cannot attach proof in phase ${attempt.phase}.`,
      )
    }
    return this.#transition(attempt, "proof_ready")
  }

  submitted(attemptIdValue: string): PermissionResponseAttempt {
    const attempt = this.#require(attemptIdValue)
    const active = this.#activeByRequest.get(attempt.requestId)
    if (active !== attempt.attemptId) {
      return this.#transition(
        attempt,
        "lost_race",
        "permission_response_claim_lost",
        "Response claim was released before submission.",
      )
    }
    if (attempt.phase !== "proof_ready" && attempt.phase !== "claimed") {
      throw raceError(
        "permission_response_submit_phase_invalid",
        `Cannot submit response in phase ${attempt.phase}.`,
      )
    }
    return this.#transition(attempt, "submitted")
  }

  accept(
    attemptIdValue: string,
    receipt: PermissionDecisionReceipt,
  ): PermissionResponseAttempt {
    const attempt = this.#require(attemptIdValue)
    if (
      receipt.requestId !== attempt.requestId
      || receipt.responseId !== attempt.responseId
      || receipt.effect !== attempt.effect
    ) {
      return this.reject(
        attempt.attemptId,
        "permission_response_receipt_mismatch",
        "Canonical receipt does not match the claimed response.",
      )
    }
    const active = this.#activeByRequest.get(attempt.requestId)
    if (active && active !== attempt.attemptId) {
      return this.#transition(
        attempt,
        "lost_race",
        "permission_response_receipt_lost_race",
        "A different response attempt owns the canonical request.",
      )
    }
    this.#activeByRequest.delete(attempt.requestId)
    return this.#transition(
      attempt,
      receipt.accepted ? "accepted" : "rejected",
      receipt.accepted ? undefined : "permission_response_not_accepted",
      receipt.accepted
        ? undefined
        : "Canonical permission runtime rejected the response.",
      receipt,
    )
  }

  reject(
    attemptIdValue: string,
    code: string,
    message: string,
  ): PermissionResponseAttempt {
    const attempt = this.#require(attemptIdValue)
    if (this.#activeByRequest.get(attempt.requestId) === attempt.attemptId) {
      this.#activeByRequest.delete(attempt.requestId)
    }
    return this.#transition(attempt, "rejected", code, message)
  }

  cancel(
    attemptIdValue: string,
    message = "Permission response was cancelled.",
  ): PermissionResponseAttempt {
    const attempt = this.#require(attemptIdValue)
    if (this.#activeByRequest.get(attempt.requestId) === attempt.attemptId) {
      this.#activeByRequest.delete(attempt.requestId)
    }
    return this.#transition(
      attempt,
      "cancelled",
      "permission_response_cancelled",
      message,
    )
  }

  releaseRequest(
    requestIdValue: string,
    reason = "Canonical request is no longer pending.",
  ): PermissionResponseAttempt[] {
    const requestId = identifier(
      requestIdValue,
      "permission request id",
    )
    this.#activeByRequest.delete(requestId)
    const released: PermissionResponseAttempt[] = []
    for (const attempt of this.#attempts.values()) {
      if (attempt.requestId !== requestId || terminal(attempt.phase)) continue
      released.push(
        this.#transition(
          attempt,
          "lost_race",
          "permission_request_settled_elsewhere",
          reason,
        ),
      )
    }
    return released
  }

  activeResponseId(requestIdValue: string): string | undefined {
    const requestId = identifier(
      requestIdValue,
      "permission request id",
    )
    const attemptId = this.#activeByRequest.get(requestId)
    return attemptId
      ? this.#attempts.get(attemptId)?.responseId
      : undefined
  }

  isClaimed(requestIdValue: string): boolean {
    return Boolean(this.activeResponseId(requestIdValue))
  }

  get(attemptIdValue: string): PermissionResponseAttempt | undefined {
    const attempt = this.#attempts.get(
      identifier(attemptIdValue, "permission response attempt id"),
    )
    return attempt ? freezeAttempt(attempt) : undefined
  }

  list(): PermissionResponseAttempt[] {
    return [...this.#attempts.values()]
      .sort(
        (left, right) =>
          comparePermissionTime(left.createdAt, right.createdAt)
          || left.attemptId.localeCompare(right.attemptId),
      )
      .map(freezeAttempt)
  }

  snapshot(): Readonly<Record<string, unknown>> {
    return Object.freeze({
      version: "zyra.permission-response-race/v1",
      attempts: Object.freeze(this.list()),
      active: Object.freeze(
        [...this.#activeByRequest.entries()]
          .map(([requestId, attemptId]) =>
            Object.freeze({ requestId, attemptId }),
          )
          .sort((left, right) => left.requestId.localeCompare(right.requestId)),
      ),
      response_ids: Object.freeze(
        [...this.#responseToRequest.entries()]
          .map(([responseId, requestId]) =>
            Object.freeze({ responseId, requestId }),
          )
          .sort((left, right) =>
            left.responseId.localeCompare(right.responseId),
          ),
      ),
    })
  }

  clear(): void {
    this.#attempts.clear()
    this.#activeByRequest.clear()
    this.#responseToRequest.clear()
  }

  #require(attemptIdValue: string): PermissionResponseAttempt {
    const attemptId = identifier(
      attemptIdValue,
      "permission response attempt id",
    )
    const attempt = this.#attempts.get(attemptId)
    if (!attempt) {
      throw raceError(
        "permission_response_attempt_not_found",
        `Permission response attempt ${attemptId} was not found.`,
      )
    }
    return attempt
  }

  #transition(
    prior: PermissionResponseAttempt,
    phase: PermissionResponseAttempt["phase"],
    errorCode?: string,
    errorMessage?: string,
    receipt?: PermissionDecisionReceipt,
  ): PermissionResponseAttempt {
    const next = freezeAttempt({
      ...prior,
      phase,
      updatedAt: this.#now().toISOString(),
      errorCode,
      errorMessage,
      receipt,
    })
    this.#attempts.set(next.attemptId, next)
    return freezeAttempt(next)
  }

  #prune(): void {
    if (this.#attempts.size <= this.#maximum) return
    const removable = this.list().filter(
      (attempt) =>
        terminal(attempt.phase)
        && this.#activeByRequest.get(attempt.requestId) !== attempt.attemptId,
    )
    for (const attempt of removable) {
      if (this.#attempts.size <= this.#maximum) break
      this.#attempts.delete(attempt.attemptId)
      const request = this.#responseToRequest.get(attempt.responseId)
      if (request === attempt.requestId) {
        this.#responseToRequest.delete(attempt.responseId)
      }
    }
  }
}

function terminal(phase: PermissionResponseAttempt["phase"]): boolean {
  return [
    "accepted",
    "rejected",
    "lost_race",
    "cancelled",
  ].includes(phase)
}

function freezeAttempt(
  value: PermissionResponseAttempt,
): PermissionResponseAttempt {
  return Object.freeze(safePermissionClone(value))
}

function raceError(code: string, message: string): Error {
  return Object.assign(new Error(message), {
    name: "PermissionResponseRaceError",
    code,
  })
}
