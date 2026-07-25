import type {
  McpReconnectPlan,
  McpServerProjection,
} from "./contracts.ts"
import { clamp, fingerprint, scalar } from "./value.ts"

export interface McpReconnectPolicy {
  initialDelayMs: number
  maximumDelayMs: number
  multiplier: number
  jitterRatio: number
  maximumAttempts: number
  resetAfterMs: number
}

export interface McpReconnectDecision {
  allowed: boolean
  code:
    | "allow"
    | "disabled"
    | "already-connected"
    | "connecting"
    | "auth-required"
    | "backoff-active"
    | "attempts-exhausted"
    | "owner-changed"
    | "capability-changed"
  reason: string
  retryAtMs?: number
}

interface AttemptState {
  serverId: string
  ownerId: string
  connectionEpoch: number
  capabilityRevision: number
  attempt: number
  lastAttemptAtMs: number
  lastConnectedAtMs?: number
  exhausted: boolean
}

const DEFAULT_POLICY: McpReconnectPolicy = Object.freeze({
  initialDelayMs: 1_000,
  maximumDelayMs: 60_000,
  multiplier: 2,
  jitterRatio: 0.2,
  maximumAttempts: 8,
  resetAfterMs: 5 * 60_000,
})

export class McpReconnectSupervisor {
  readonly #policy: McpReconnectPolicy
  readonly #now: () => number
  readonly #attempts = new Map<string, AttemptState>()
  readonly #plans = new Map<string, McpReconnectPlan>()
  #enabled = true
  #disabledReason = "MCP reconnect supervisor is disabled."

  constructor(options: {
    policy?: Partial<McpReconnectPolicy>
    now?: () => number
  } = {}) {
    this.#policy = normalizePolicy(options.policy)
    this.#now = options.now ?? Date.now
  }

  admit(server: McpServerProjection): McpReconnectDecision {
    this.#assertEnabled()
    const now = this.#now()
    const state = this.#attempts.get(server.id)
    if (!server.enabled || server.state === "disabled") {
      return decision(false, "disabled", server.disabledReason ?? "MCP server is disabled.")
    }
    if (server.state === "connected") {
      return decision(false, "already-connected", "MCP server is already connected.")
    }
    if (server.state === "connecting") {
      return decision(false, "connecting", "MCP server connection is already in progress.")
    }
    if (server.state === "needs-auth") {
      return decision(false, "auth-required", "MCP server requires authentication before reconnect.")
    }
    if (
      server.retryAt &&
      Number.isFinite(Date.parse(server.retryAt)) &&
      Date.parse(server.retryAt) > now
    ) {
      return decision(
        false,
        "backoff-active",
        "Canonical MCP owner backoff has not elapsed.",
        Date.parse(server.retryAt),
      )
    }
    if (state?.ownerId && state.ownerId !== server.ownerId) {
      return decision(false, "owner-changed", "MCP owner changed; refresh canonical projection.")
    }
    if (
      state &&
      state.capabilityRevision !== server.capabilities.revision &&
      state.attempt > 0
    ) {
      return decision(
        false,
        "capability-changed",
        "MCP capability revision changed; rebuild reconnect state.",
      )
    }
    if (state?.exhausted || (state?.attempt ?? 0) >= this.#policy.maximumAttempts) {
      return decision(
        false,
        "attempts-exhausted",
        "MCP reconnect attempt budget is exhausted.",
      )
    }
    const active = this.#plans.get(server.id)
    if (active?.state === "scheduled" && active.readyAtMs > now) {
      return decision(
        false,
        "backoff-active",
        "MCP reconnect schedule has not elapsed.",
        active.readyAtMs,
      )
    }
    return decision(true, "allow", "MCP server is eligible for owner reconnect.")
  }

  schedule(
    server: McpServerProjection,
    reason = "connection unavailable",
  ): McpReconnectPlan {
    this.#assertEnabled()
    const admission = this.admit(server)
    if (!admission.allowed && admission.code !== "backoff-active") {
      throw new Error(admission.reason)
    }
    const now = this.#now()
    const previous = this.#attempts.get(server.id)
    const shouldReset =
      previous?.lastConnectedAtMs !== undefined &&
      now - previous.lastConnectedAtMs >= this.#policy.resetAfterMs
    const attempt = shouldReset ? 1 : (previous?.attempt ?? 0) + 1
    if (attempt > this.#policy.maximumAttempts) {
      const exhausted: AttemptState = {
        serverId: server.id,
        ownerId: server.ownerId,
        connectionEpoch: server.connectionEpoch,
        capabilityRevision: server.capabilities.revision,
        attempt: this.#policy.maximumAttempts,
        lastAttemptAtMs: now,
        lastConnectedAtMs: previous?.lastConnectedAtMs,
        exhausted: true,
      }
      this.#attempts.set(server.id, exhausted)
      const plan = this.#plan(server, exhausted.attempt, now, 0, reason, "exhausted")
      this.#plans.set(server.id, plan)
      return plan
    }
    const delayMs = backoffDelay(
      server.id,
      attempt,
      this.#policy,
    )
    const attemptState: AttemptState = {
      serverId: server.id,
      ownerId: server.ownerId,
      connectionEpoch: server.connectionEpoch,
      capabilityRevision: server.capabilities.revision,
      attempt,
      lastAttemptAtMs: now,
      lastConnectedAtMs: previous?.lastConnectedAtMs,
      exhausted: false,
    }
    this.#attempts.set(server.id, attemptState)
    const plan = this.#plan(
      server,
      attempt,
      now,
      Math.max(delayMs, server.retryAfterMs ?? 0),
      reason,
      delayMs <= 0 ? "ready" : "scheduled",
    )
    this.#plans.set(server.id, plan)
    return plan
  }

  ready(serverId: string, nowMs = this.#now()): McpReconnectPlan | undefined {
    this.#assertEnabled()
    const plan = this.#plans.get(serverId)
    if (!plan) return undefined
    if (plan.state !== "scheduled") return plan
    if (plan.readyAtMs > nowMs) return plan
    const ready = Object.freeze({
      ...plan,
      state: "ready" as const,
    })
    this.#plans.set(serverId, ready)
    return ready
  }

  observe(server: McpServerProjection): void {
    this.#assertEnabled()
    const now = this.#now()
    const current = this.#attempts.get(server.id)
    if (current && current.ownerId !== server.ownerId) {
      this.#attempts.delete(server.id)
      this.cancel(server.id, "canonical MCP owner changed")
      return
    }
    if (
      current &&
      (current.connectionEpoch !== server.connectionEpoch ||
        current.capabilityRevision !== server.capabilities.revision)
    ) {
      this.#plans.delete(server.id)
    }
    if (server.state === "connected") {
      this.#plans.delete(server.id)
      this.#attempts.set(server.id, {
        serverId: server.id,
        ownerId: server.ownerId,
        connectionEpoch: server.connectionEpoch,
        capabilityRevision: server.capabilities.revision,
        attempt: 0,
        lastAttemptAtMs: current?.lastAttemptAtMs ?? now,
        lastConnectedAtMs: now,
        exhausted: false,
      })
      return
    }
    if (!server.enabled || server.state === "disabled") {
      this.cancel(server.id, server.disabledReason ?? "server disabled")
    }
  }

  settle(
    serverId: string,
    result: "submitted" | "failed" | "cancelled",
    reason?: string,
  ): McpReconnectPlan | undefined {
    this.#assertEnabled()
    const plan = this.#plans.get(serverId)
    if (!plan) return undefined
    if (result === "submitted") {
      const ready = Object.freeze({ ...plan, state: "ready" as const })
      this.#plans.set(serverId, ready)
      return ready
    }
    const cancelled = Object.freeze({
      ...plan,
      state: "cancelled" as const,
      reason: scalar(reason, result),
    })
    this.#plans.set(serverId, cancelled)
    return cancelled
  }

  cancel(serverId: string, reason = "reconnect cancelled"): McpReconnectPlan | undefined {
    const plan = this.#plans.get(serverId)
    if (!plan) return undefined
    const cancelled = Object.freeze({
      ...plan,
      state: "cancelled" as const,
      reason: scalar(reason, "reconnect cancelled"),
    })
    this.#plans.set(serverId, cancelled)
    return cancelled
  }

  list(): readonly McpReconnectPlan[] {
    this.#assertEnabled()
    return Object.freeze(
      [...this.#plans.values()].sort(
        (left, right) =>
          left.readyAtMs - right.readyAtMs ||
          left.serverId.localeCompare(right.serverId),
      ),
    )
  }

  disconnect(reason = "Browser projection disconnected."): void {
    this.#assertEnabled()
    for (const serverId of this.#plans.keys()) this.cancel(serverId, reason)
  }

  reset(serverId?: string): void {
    this.#assertEnabled()
    if (serverId) {
      this.#plans.delete(serverId)
      this.#attempts.delete(serverId)
      return
    }
    this.#plans.clear()
    this.#attempts.clear()
  }

  disable(reason = "MCP reconnect supervisor is disabled."): void {
    this.#disabledReason = scalar(reason, "MCP reconnect supervisor is disabled.")
    for (const serverId of this.#plans.keys()) this.cancel(serverId, this.#disabledReason)
    this.#enabled = false
  }

  enable(): void {
    this.#enabled = true
  }

  #plan(
    server: McpServerProjection,
    attempt: number,
    createdAtMs: number,
    delayMs: number,
    reason: string,
    state: McpReconnectPlan["state"],
  ): McpReconnectPlan {
    return Object.freeze({
      serverId: server.id,
      ownerId: server.ownerId,
      connectionEpoch: server.connectionEpoch,
      capabilityRevision: server.capabilities.revision,
      attempt,
      createdAtMs,
      delayMs,
      readyAtMs: createdAtMs + delayMs,
      maximumAttempts: this.#policy.maximumAttempts,
      reason: scalar(reason, "connection unavailable"),
      state,
    })
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

export function backoffDelay(
  serverId: string,
  attempt: number,
  policy: McpReconnectPolicy = DEFAULT_POLICY,
): number {
  const exponent = Math.max(0, Math.trunc(attempt) - 1)
  const raw = Math.min(
    policy.maximumDelayMs,
    policy.initialDelayMs * policy.multiplier ** exponent,
  )
  const jitterRange = raw * policy.jitterRatio
  const hash = fingerprint([serverId, attempt])
  const sample = Number.parseInt(hash.slice(-8), 16) / 0xffffffff
  const jitter = (sample * 2 - 1) * jitterRange
  return Math.max(0, Math.round(raw + jitter))
}

function normalizePolicy(
  source: Partial<McpReconnectPolicy> | undefined,
): McpReconnectPolicy {
  const initialDelayMs = clamp(
    Math.trunc(source?.initialDelayMs ?? DEFAULT_POLICY.initialDelayMs),
    0,
    60_000,
  )
  const maximumDelayMs = clamp(
    Math.trunc(source?.maximumDelayMs ?? DEFAULT_POLICY.maximumDelayMs),
    Math.max(1, initialDelayMs),
    24 * 60 * 60_000,
  )
  return Object.freeze({
    initialDelayMs,
    maximumDelayMs,
    multiplier: clamp(source?.multiplier ?? DEFAULT_POLICY.multiplier, 1, 10),
    jitterRatio: clamp(source?.jitterRatio ?? DEFAULT_POLICY.jitterRatio, 0, 1),
    maximumAttempts: clamp(
      Math.trunc(source?.maximumAttempts ?? DEFAULT_POLICY.maximumAttempts),
      1,
      100,
    ),
    resetAfterMs: clamp(
      Math.trunc(source?.resetAfterMs ?? DEFAULT_POLICY.resetAfterMs),
      1_000,
      24 * 60 * 60_000,
    ),
  })
}

function decision(
  allowed: boolean,
  code: McpReconnectDecision["code"],
  reason: string,
  retryAtMs?: number,
): McpReconnectDecision {
  return Object.freeze({ allowed, code, reason, retryAtMs })
}
