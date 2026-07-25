import { stablePermissionId } from "./canonical.ts"

export type PermissionReconnectPhase =
  | "idle"
  | "healthy"
  | "offline"
  | "backoff"
  | "probing"
  | "terminal"
  | "closed"

export interface PermissionReconnectSnapshot {
  schema: "zyra.permission-reconnect/v1"
  phase: PermissionReconnectPhase
  generation: number
  failureCount: number
  consecutiveFailures: number
  successCount: number
  probeCount: number
  activeProbeId?: string
  lastFailureCode?: string
  lastFailureMessage?: string
  lastFailureAt?: string
  lastSuccessAt?: string
  nextProbeAt?: string
  retryable: boolean
  online: boolean
  revision: number
}

export class PermissionReconnectSupervisor {
  readonly #now: () => Date
  readonly #pollIntervalMs: number
  readonly #baseDelayMs: number
  readonly #maximumDelayMs: number
  readonly #maximumFailures: number
  #phase: PermissionReconnectPhase = "idle"
  #generation = 0
  #failureCount = 0
  #consecutiveFailures = 0
  #successCount = 0
  #probeCount = 0
  #activeProbeId?: string
  #lastFailureCode?: string
  #lastFailureMessage?: string
  #lastFailureAt?: string
  #lastSuccessAt?: string
  #nextProbeAt?: string
  #retryable = true
  #online = true
  #revision = 0

  constructor(options: {
    now?: () => Date
    pollIntervalMs?: number
    baseDelayMs?: number
    maximumDelayMs?: number
    maximumFailures?: number
  } = {}) {
    this.#now = options.now ?? (() => new Date())
    this.#pollIntervalMs = bounded(
      options.pollIntervalMs,
      2_000,
      300_000,
      5_000,
    )
    this.#baseDelayMs = bounded(
      options.baseDelayMs,
      250,
      30_000,
      1_000,
    )
    this.#maximumDelayMs = bounded(
      options.maximumDelayMs,
      this.#baseDelayMs,
      300_000,
      30_000,
    )
    this.#maximumFailures = bounded(
      options.maximumFailures,
      1,
      100,
      12,
    )
  }

  bind(generation: number): PermissionReconnectSnapshot {
    this.#assertOpen()
    this.#generation = Math.max(0, Math.floor(generation))
    this.#phase = "healthy"
    this.#consecutiveFailures = 0
    this.#activeProbeId = undefined
    this.#lastFailureCode = undefined
    this.#lastFailureMessage = undefined
    this.#lastFailureAt = undefined
    this.#nextProbeAt = undefined
    this.#retryable = true
    this.#online = true
    this.#revision += 1
    return this.snapshot()
  }

  success(): PermissionReconnectSnapshot {
    this.#assertOpen()
    this.#phase = "healthy"
    this.#successCount += 1
    this.#consecutiveFailures = 0
    this.#activeProbeId = undefined
    this.#lastSuccessAt = this.#now().toISOString()
    this.#nextProbeAt = undefined
    this.#retryable = true
    this.#online = true
    this.#revision += 1
    return this.snapshot()
  }

  failure(
    error: unknown,
    options: { online?: boolean } = {},
  ): PermissionReconnectSnapshot {
    this.#assertOpen()
    const now = this.#now()
    const classification = classifyFailure(error)
    this.#failureCount += 1
    this.#consecutiveFailures += 1
    this.#activeProbeId = undefined
    this.#lastFailureCode = classification.code
    this.#lastFailureMessage = classification.message
    this.#lastFailureAt = now.toISOString()
    this.#retryable = classification.retryable
    this.#online = options.online ?? this.#online
    if (!this.#online) {
      this.#phase = "offline"
      this.#nextProbeAt = undefined
    } else if (
      !classification.retryable
      || this.#consecutiveFailures >= this.#maximumFailures
    ) {
      this.#phase = "terminal"
      this.#nextProbeAt = undefined
      this.#retryable = false
    } else {
      this.#phase = "backoff"
      this.#nextProbeAt = new Date(
        now.getTime() + this.#backoffDelay(),
      ).toISOString()
    }
    this.#revision += 1
    return this.snapshot()
  }

  markOffline(): PermissionReconnectSnapshot {
    this.#assertOpen()
    this.#online = false
    this.#phase = "offline"
    this.#activeProbeId = undefined
    this.#nextProbeAt = undefined
    this.#revision += 1
    return this.snapshot()
  }

  markOnline(): PermissionReconnectSnapshot {
    this.#assertOpen()
    this.#online = true
    if (this.#phase === "offline" || this.#phase === "backoff") {
      this.#phase = "probing"
      this.#nextProbeAt = this.#now().toISOString()
    }
    this.#revision += 1
    return this.snapshot()
  }

  claimProbe(): string | undefined {
    this.#assertOpen()
    if (!this.#online || this.#phase === "terminal") return undefined
    if (this.#activeProbeId) return undefined
    if (!this.due()) return undefined
    const probeId = stablePermissionId(
      "permission_reconnect_probe",
      this.#generation,
      this.#probeCount + 1,
      this.#now().toISOString(),
    )
    this.#probeCount += 1
    this.#activeProbeId = probeId
    this.#phase = "probing"
    this.#nextProbeAt = undefined
    this.#revision += 1
    return probeId
  }

  due(): boolean {
    if (!this.#online || this.#phase === "terminal") return false
    if (this.#phase === "healthy" || this.#phase === "probing") return true
    if (!this.#nextProbeAt) return false
    return Date.parse(this.#nextProbeAt) <= this.#now().getTime()
  }

  nextDelayMs(): number | undefined {
    if (this.#phase === "closed" || this.#phase === "terminal") {
      return undefined
    }
    if (this.#phase === "offline") return undefined
    if (this.#phase === "healthy" || this.#phase === "idle") {
      return this.#pollIntervalMs
    }
    if (this.#phase === "probing") return 0
    const next = this.#nextProbeAt
      ? Date.parse(this.#nextProbeAt)
      : this.#now().getTime()
    return Math.max(0, next - this.#now().getTime())
  }

  snapshot(): PermissionReconnectSnapshot {
    return Object.freeze({
      schema: "zyra.permission-reconnect/v1",
      phase: this.#phase,
      generation: this.#generation,
      failureCount: this.#failureCount,
      consecutiveFailures: this.#consecutiveFailures,
      successCount: this.#successCount,
      probeCount: this.#probeCount,
      activeProbeId: this.#activeProbeId,
      lastFailureCode: this.#lastFailureCode,
      lastFailureMessage: this.#lastFailureMessage,
      lastFailureAt: this.#lastFailureAt,
      lastSuccessAt: this.#lastSuccessAt,
      nextProbeAt: this.#nextProbeAt,
      retryable: this.#retryable,
      online: this.#online,
      revision: this.#revision,
    })
  }

  close(): void {
    if (this.#phase === "closed") return
    this.#phase = "closed"
    this.#activeProbeId = undefined
    this.#nextProbeAt = undefined
    this.#revision += 1
  }

  #backoffDelay(): number {
    const exponent = Math.min(16, this.#consecutiveFailures - 1)
    const raw = Math.min(
      this.#maximumDelayMs,
      this.#baseDelayMs * 2 ** exponent,
    )
    const seed = stablePermissionId(
      "permission_reconnect_jitter",
      this.#generation,
      this.#consecutiveFailures,
      this.#lastFailureCode,
    )
    const suffix = Number.parseInt(seed.slice(-6), 16)
    const jitter = Number.isFinite(suffix)
      ? (suffix % Math.max(1, Math.floor(raw * 0.2)))
      : 0
    return Math.min(this.#maximumDelayMs, raw + jitter)
  }

  #assertOpen(): void {
    if (this.#phase === "closed") {
      throw reconnectError(
        "permission_reconnect_closed",
        "Permission reconnect supervisor is closed.",
      )
    }
  }
}

function classifyFailure(error: unknown): {
  code: string
  message: string
  retryable: boolean
} {
  const record =
    error && typeof error === "object"
      ? error as Record<string, unknown>
      : {}
  const code =
    typeof record.code === "string" && record.code.trim()
      ? record.code.trim().slice(0, 256)
      : "permission_transport_failed"
  const message = (
    error instanceof Error
      ? error.message
      : String(error || "Permission transport failed.")
  ).slice(0, 2_000)
  const status = Number(record.status ?? 0)
  const permanentStatus = [400, 401, 403, 404, 409, 410, 422]
    .includes(status)
  const permanentCode = [
    "permission_custody_missing",
    "permission_custody_invalid",
    "permission_session_binding_mismatch",
    "permission_response_proof_invalid",
    "permission_response_owner_mismatch",
  ].some((value) => code.includes(value))
  return {
    code,
    message,
    retryable: !permanentStatus && !permanentCode,
  }
}

function bounded(
  value: number | undefined,
  minimum: number,
  maximum: number,
  fallback: number,
): number {
  if (!Number.isFinite(value)) return fallback
  return Math.min(maximum, Math.max(minimum, Math.floor(value!)))
}

function reconnectError(code: string, message: string): Error {
  return Object.assign(new Error(message), {
    name: "PermissionReconnectError",
    code,
  })
}
