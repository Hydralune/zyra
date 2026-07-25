import {
  comparePermissionTime,
  identifier,
  stablePermissionId,
} from "./canonical.ts"
import type { PermissionRequestProjection } from "./contracts.ts"

export interface PermissionExpiryClaim {
  claimId: string
  requestIds: readonly string[]
  claimedAt: string
  generation: number
}

export interface PermissionExpirySnapshot {
  schema: "zyra.permission-expiry-supervisor/v1"
  pending: readonly {
    requestId: string
    expiresAt: string
    claimed: boolean
  }[]
  activeClaim?: PermissionExpiryClaim
  nextExpiryAt?: string
  observedTerminalCount: number
  claimCount: number
  failureCount: number
  conflictCount: number
  ownsRequestState: false
  defaultEffect: "deny"
  revision: number
}

export class PermissionExpirySupervisor {
  readonly #pending = new Map<string, string>()
  readonly #terminal = new Set<string>()
  readonly #now: () => Date
  readonly #maximum: number
  #active?: PermissionExpiryClaim
  #generation = 0
  #observedTerminalCount = 0
  #claimCount = 0
  #failureCount = 0
  #conflictCount = 0
  #revision = 0
  #closed = false

  constructor(options: {
    now?: () => Date
    maximum?: number
  } = {}) {
    this.#now = options.now ?? (() => new Date())
    this.#maximum = Math.min(
      10_000,
      Math.max(16, Math.floor(options.maximum ?? 1_000)),
    )
  }

  reconcile(
    requests: readonly PermissionRequestProjection[],
  ): PermissionExpirySnapshot {
    this.#assertOpen()
    this.#generation += 1
    const visible = new Set<string>()
    for (const request of requests) {
      const requestId = identifier(
        request.requestId,
        "permission expiry request id",
      )
      visible.add(requestId)
      if (request.terminal) {
        if (!this.#terminal.has(requestId)) {
          this.#terminal.add(requestId)
          this.#observedTerminalCount += 1
        }
        this.#pending.delete(requestId)
        continue
      }
      const expiresAt = normalizeTimestamp(request.expiresAt)
      const prior = this.#pending.get(requestId)
      if (prior && prior !== expiresAt) {
        this.#conflictCount += 1
        this.#pending.delete(requestId)
        continue
      }
      this.#pending.set(requestId, expiresAt)
    }
    for (const requestId of this.#pending.keys()) {
      if (!visible.has(requestId)) this.#pending.delete(requestId)
    }
    if (
      this.#active
      && this.#active.requestIds.every(
        (requestId) =>
          !this.#pending.has(requestId)
          || this.#terminal.has(requestId),
      )
    ) {
      this.#active = undefined
    }
    this.#prune()
    this.#revision += 1
    return this.snapshot()
  }

  due(now: Date = this.#now()): string[] {
    this.#assertOpen()
    if (!Number.isFinite(now.getTime())) {
      throw new TypeError("Permission expiry clock is invalid.")
    }
    return [...this.#pending.entries()]
      .filter(([, expiresAt]) => Date.parse(expiresAt) <= now.getTime())
      .sort(
        ([leftId, leftTime], [rightId, rightTime]) =>
          comparePermissionTime(leftTime, rightTime)
          || leftId.localeCompare(rightId),
      )
      .map(([requestId]) => requestId)
  }

  claimDue(now: Date = this.#now()): PermissionExpiryClaim | undefined {
    this.#assertOpen()
    if (this.#active) return undefined
    const requestIds = this.due(now)
    if (!requestIds.length) return undefined
    const claim = Object.freeze({
      claimId: stablePermissionId(
        "permission_expiry_claim",
        this.#generation,
        requestIds,
        now.toISOString(),
      ),
      requestIds: Object.freeze(requestIds),
      claimedAt: now.toISOString(),
      generation: this.#generation,
    })
    this.#active = claim
    this.#claimCount += 1
    this.#revision += 1
    return claim
  }

  settle(
    claimIdValue: string,
    outcome: { accepted: boolean },
  ): PermissionExpirySnapshot {
    this.#assertOpen()
    const claimId = identifier(
      claimIdValue,
      "permission expiry claim id",
    )
    if (!this.#active || this.#active.claimId !== claimId) {
      throw expiryError(
        "permission_expiry_claim_mismatch",
        "Permission expiry result does not match the active claim.",
      )
    }
    if (!outcome.accepted) this.#failureCount += 1
    this.#active = undefined
    this.#revision += 1
    return this.snapshot()
  }

  nextDelayMs(now: Date = this.#now()): number | undefined {
    this.#assertOpen()
    if (this.#active || !this.#pending.size) return undefined
    const next = [...this.#pending.values()]
      .sort(comparePermissionTime)[0]
    if (!next) return undefined
    return Math.max(0, Date.parse(next) - now.getTime())
  }

  snapshot(): PermissionExpirySnapshot {
    const pending = [...this.#pending.entries()]
      .sort(
        ([leftId, leftTime], [rightId, rightTime]) =>
          comparePermissionTime(leftTime, rightTime)
          || leftId.localeCompare(rightId),
      )
      .map(([requestId, expiresAt]) =>
        Object.freeze({
          requestId,
          expiresAt,
          claimed:
            this.#active?.requestIds.includes(requestId) === true,
        }),
      )
    return Object.freeze({
      schema: "zyra.permission-expiry-supervisor/v1",
      pending: Object.freeze(pending),
      activeClaim: this.#active,
      nextExpiryAt: pending[0]?.expiresAt,
      observedTerminalCount: this.#observedTerminalCount,
      claimCount: this.#claimCount,
      failureCount: this.#failureCount,
      conflictCount: this.#conflictCount,
      ownsRequestState: false,
      defaultEffect: "deny",
      revision: this.#revision,
    })
  }

  clear(): void {
    this.#pending.clear()
    this.#terminal.clear()
    this.#active = undefined
    this.#generation += 1
    this.#observedTerminalCount = 0
    this.#claimCount = 0
    this.#failureCount = 0
    this.#conflictCount = 0
    this.#revision += 1
  }

  close(): void {
    if (this.#closed) return
    this.clear()
    this.#closed = true
  }

  #prune(): void {
    while (this.#terminal.size > this.#maximum) {
      const oldest = this.#terminal.values().next().value
      if (!oldest) break
      this.#terminal.delete(oldest)
    }
    while (this.#pending.size > this.#maximum) {
      const last = [...this.#pending.entries()]
        .sort(
          ([leftId, leftTime], [rightId, rightTime]) =>
            comparePermissionTime(rightTime, leftTime)
            || rightId.localeCompare(leftId),
        )[0]
      if (!last) break
      this.#pending.delete(last[0])
    }
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw expiryError(
        "permission_expiry_supervisor_closed",
        "Permission expiry supervisor is closed.",
      )
    }
  }
}

function normalizeTimestamp(value: string): string {
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) {
    throw expiryError(
      "permission_expiry_timestamp_invalid",
      "Permission request expiry is invalid.",
    )
  }
  return new Date(timestamp).toISOString()
}

function expiryError(code: string, message: string): Error {
  return Object.assign(new Error(message), {
    name: "PermissionExpirySupervisorError",
    code,
  })
}
