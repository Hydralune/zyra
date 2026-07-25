import type {
  ModelRow,
  ProviderConsoleProjection,
  ProviderHealth,
  ProviderRoute,
  ProviderRouteCandidate,
  ProviderUsage,
} from "./projection.ts"
import {
  bounded,
  compareNumber,
  compareText,
  fingerprint,
  mean,
  ratio,
  sortStable,
  sum,
  text,
  unique,
} from "../session/value.ts"

export interface FailoverPolicy {
  requiredCapabilities: readonly string[]
  excludedProviderIds: readonly string[]
  excludedModelIds: readonly string[]
  preferredProviderIds: readonly string[]
  preferredModelIds: readonly string[]
  maximumLatencyMs?: number
  maximumCostUsd?: number
  minimumContextWindow?: number
  requireCredential: boolean
  allowDegraded: boolean
  allowRateLimited: boolean
  allowUnknownHealth: boolean
  preserveProvider: boolean
  preserveModelFamily: boolean
  maximumAttempts: number
  backoffBaseMs: number
}

export interface FailoverFailure {
  id: string
  providerId: string
  modelId?: string
  routeId?: string
  code: string
  reason: string
  retryable: boolean
  rateLimited: boolean
  quotaExhausted: boolean
  credentialFailure: boolean
  capabilityFailure: boolean
  occurredAt: string
  sourceEventId?: string
}

export interface FailoverAttempt {
  id: string
  order: number
  candidate: ProviderRouteCandidate
  allowed: boolean
  reasons: readonly string[]
  score: number
  delayMs: number
  previousProviderId?: string
  previousModelId?: string
  preservesProvider: boolean
  preservesFamily: boolean
  expectedCostUsd: number
  expectedLatencyMs: number
}

export interface FailoverPlan {
  id: string
  policy: FailoverPolicy
  failure: FailoverFailure
  currentRoute?: ProviderRoute
  attempts: readonly FailoverAttempt[]
  selected?: FailoverAttempt
  exhausted: boolean
  reason: string
  fallbackProviderIds: readonly string[]
  fallbackModelIds: readonly string[]
  estimatedRecoveryMs: number
}

export interface ProviderCircuit {
  providerId: string
  state: "closed" | "open" | "half-open"
  failureCount: number
  successCount: number
  consecutiveFailures: number
  openedAt?: string
  retryAt?: string
  lastFailureCode?: string
  lastFailureReason?: string
  health: ProviderHealth
}

export class ProviderFailoverPlanner {
  readonly #failures: FailoverFailure[] = []
  readonly #plans = new Map<string, FailoverPlan>()
  readonly #circuits = new Map<string, ProviderCircuit>()
  #enabled = true
  #disabledReason = "Provider failover planner is disabled."

  recordFailure(input: Omit<FailoverFailure, "id"> & { id?: string }): FailoverFailure {
    this.#assertEnabled()
    const failure: FailoverFailure = Object.freeze({
      ...input,
      id: input.id ?? fingerprint([
        input.providerId,
        input.modelId,
        input.routeId,
        input.code,
        input.occurredAt,
        this.#failures.length,
      ]),
    })
    this.#failures.push(failure)
    if (this.#failures.length > 1000) this.#failures.splice(0, this.#failures.length - 1000)
    this.#failureCircuit(failure)
    return failure
  }

  recordSuccess(providerId: string, health: ProviderHealth = "healthy"): ProviderCircuit {
    this.#assertEnabled()
    const current = this.#circuits.get(providerId) ?? emptyCircuit(providerId, health)
    const next: ProviderCircuit = Object.freeze({
      ...current,
      state: "closed",
      successCount: current.successCount + 1,
      consecutiveFailures: 0,
      openedAt: undefined,
      retryAt: undefined,
      health,
    })
    this.#circuits.set(providerId, next)
    return next
  }

  halfOpen(providerId: string, now = new Date()): ProviderCircuit {
    this.#assertEnabled()
    const current = this.#circuits.get(providerId)
    if (!current) throw new Error("Provider circuit does not exist.")
    if (current.state !== "open") return current
    if (current.retryAt && Date.parse(current.retryAt) > now.getTime()) return current
    const next: ProviderCircuit = Object.freeze({
      ...current,
      state: "half-open",
    })
    this.#circuits.set(providerId, next)
    return next
  }

  plan(
    projection: ProviderConsoleProjection,
    failure: FailoverFailure,
    input: Partial<FailoverPolicy> = {},
  ): FailoverPlan {
    this.#assertEnabled()
    const policy = normalizePolicy(input)
    const currentRoute = projection.selectedRoute
    const modelById = new Map(projection.models.map((model) => [model.id, model]))
    const candidates = projection.candidates
      .filter((candidate) =>
        candidate.providerId !== failure.providerId ||
        candidate.modelId !== failure.modelId)
      .map((candidate) => this.#attempt(
        candidate,
        projection,
        modelById,
        currentRoute,
        failure,
        policy,
      ))
    const ordered = sortStable(candidates, (left, right) =>
      Number(right.allowed) - Number(left.allowed) ||
      compareNumber(right.score, left.score) ||
      compareNumber(left.delayMs, right.delayMs) ||
      compareText(left.id, right.id))
      .slice(0, policy.maximumAttempts)
      .map((attempt, index) => Object.freeze({
        ...attempt,
        order: index + 1,
      }))
    const selected = ordered.find((attempt) => attempt.allowed)
    const exhausted = !selected
    const estimatedRecoveryMs = selected
      ? sum(ordered.slice(0, selected.order).map((attempt) => attempt.delayMs)) +
        selected.expectedLatencyMs
      : sum(ordered.map((attempt) => attempt.delayMs))
    const plan: FailoverPlan = Object.freeze({
      id: fingerprint([
        failure,
        policy,
        projection.revision,
        ordered.map((attempt) => [attempt.id, attempt.allowed, attempt.score]),
      ]),
      policy,
      failure,
      currentRoute,
      attempts: Object.freeze(ordered),
      selected,
      exhausted,
      reason: selected
        ? `Selected ${selected.candidate.providerId}/${selected.candidate.modelId}.`
        : "No provider/model candidate satisfies failover policy.",
      fallbackProviderIds: Object.freeze(unique(
        ordered.filter((attempt) => attempt.allowed).map((attempt) => attempt.candidate.providerId),
      )),
      fallbackModelIds: Object.freeze(unique(
        ordered.filter((attempt) => attempt.allowed).map((attempt) => attempt.candidate.modelId),
      )),
      estimatedRecoveryMs,
    })
    this.#plans.set(plan.id, plan)
    return plan
  }

  get(planId: string): FailoverPlan | undefined {
    this.#assertEnabled()
    return this.#plans.get(planId)
  }

  listPlans(): readonly FailoverPlan[] {
    this.#assertEnabled()
    return Object.freeze([...this.#plans.values()])
  }

  failures(providerId?: string): readonly FailoverFailure[] {
    this.#assertEnabled()
    return Object.freeze(this.#failures.filter((failure) =>
      !providerId || failure.providerId === providerId))
  }

  circuit(providerId: string): ProviderCircuit | undefined {
    this.#assertEnabled()
    return this.#circuits.get(providerId)
  }

  circuits(): readonly ProviderCircuit[] {
    this.#assertEnabled()
    return Object.freeze(sortStable([...this.#circuits.values()], (left, right) =>
      circuitRank(left.state) - circuitRank(right.state) ||
      compareNumber(right.consecutiveFailures, left.consecutiveFailures) ||
      compareText(left.providerId, right.providerId)))
  }

  audit(projection: ProviderConsoleProjection): Readonly<Record<string, unknown>> {
    this.#assertEnabled()
    const open = this.circuits().filter((circuit) => circuit.state === "open")
    const exhausted = this.listPlans().filter((plan) => plan.exhausted)
    const uncredentialed = projection.providers
      .filter((provider) => !provider.credentialPresent)
      .map((provider) => provider.id)
    const unavailableModels = projection.models
      .filter((model) => !model.available || model.deprecated)
      .map((model) => model.id)
    return Object.freeze({
      failureCount: this.#failures.length,
      planCount: this.#plans.size,
      exhaustedPlanIds: Object.freeze(exhausted.map((plan) => plan.id)),
      openProviderIds: Object.freeze(open.map((circuit) => circuit.providerId)),
      uncredentialedProviderIds: Object.freeze(uncredentialed),
      unavailableModelIds: Object.freeze(unavailableModels),
      fallbackDepth: projection.fallbackChain.length,
      secretLeakCount: projection.secretLeakCount,
      totalFailures: projection.totalFailures,
      healthyProviderCount: projection.providers.filter((provider) =>
        provider.health === "healthy").length,
    })
  }

  disable(reason = "Provider failover planner is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Provider failover planner is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  clear(): void {
    this.#assertEnabled()
    this.#failures.splice(0)
    this.#plans.clear()
    this.#circuits.clear()
  }

  #attempt(
    candidate: ProviderRouteCandidate,
    projection: ProviderConsoleProjection,
    modelById: ReadonlyMap<string, ModelRow>,
    currentRoute: ProviderRoute | undefined,
    failure: FailoverFailure,
    policy: FailoverPolicy,
  ): FailoverAttempt {
    const reasons: string[] = []
    const provider = projection.providers.find((entry) => entry.id === candidate.providerId)
    const model = modelById.get(candidate.modelId)
    const circuit = this.#circuits.get(candidate.providerId)
    if (policy.excludedProviderIds.includes(candidate.providerId)) reasons.push("provider excluded")
    if (policy.excludedModelIds.includes(candidate.modelId)) reasons.push("model excluded")
    if (candidate.admission === "rejected" || candidate.admission === "failed") reasons.push("candidate rejected")
    if (candidate.missingCapabilities.length) reasons.push("required capability missing")
    if (policy.requireCredential && !candidate.credentialPresent) reasons.push("credential absent")
    if (!candidate.quotaAvailable) reasons.push("quota unavailable")
    if (provider?.health === "offline" || provider?.health === "exhausted") reasons.push(`provider ${provider.health}`)
    if (provider?.health === "degraded" && !policy.allowDegraded) reasons.push("degraded provider forbidden")
    if (provider?.health === "rate-limited" && !policy.allowRateLimited) reasons.push("rate-limited provider forbidden")
    if (provider?.health === "unknown" && !policy.allowUnknownHealth) reasons.push("unknown provider health forbidden")
    if (circuit?.state === "open") reasons.push("provider circuit open")
    if (policy.maximumLatencyMs && (candidate.latencyEstimateMs ?? 0) > policy.maximumLatencyMs) {
      reasons.push("latency exceeds failover policy")
    }
    if (policy.maximumCostUsd && (candidate.costEstimateUsd ?? 0) > policy.maximumCostUsd) {
      reasons.push("cost exceeds failover policy")
    }
    if (policy.minimumContextWindow && (model?.contextWindow ?? 0) < policy.minimumContextWindow) {
      reasons.push("context window below policy")
    }
    const preservesProvider = candidate.providerId === currentRoute?.providerId
    const currentModel = modelById.get(currentRoute?.modelId ?? "")
    const preservesFamily = Boolean(
      model?.family &&
      currentModel?.family &&
      model.family === currentModel.family,
    )
    if (policy.preserveProvider && !preservesProvider) reasons.push("provider preservation required")
    if (policy.preserveModelFamily && !preservesFamily) reasons.push("model family preservation required")
    const preference = preferenceScore(candidate, policy)
    const reliability = providerReliability(
      projection.usage.filter((usage) => usage.providerId === candidate.providerId),
    )
    const transition = transitionScore(
      candidate,
      currentRoute,
      currentModel,
      model,
      failure,
    )
    const score = bounded(
      candidate.score * 0.45 +
      preference * 0.2 +
      reliability * 0.2 +
      transition * 0.15,
      0,
      1,
    )
    const failureIndex = this.#failures
      .filter((entry) => entry.providerId === candidate.providerId)
      .slice(-10)
      .length
    const delayMs = Math.min(
      300000,
      Math.round(policy.backoffBaseMs * 2 ** Math.min(10, failureIndex)),
    )
    return Object.freeze({
      id: fingerprint([
        candidate.id,
        failure.id,
        policy,
      ]),
      order: 0,
      candidate,
      allowed: reasons.length === 0,
      reasons: Object.freeze(reasons),
      score,
      delayMs,
      previousProviderId: currentRoute?.providerId,
      previousModelId: currentRoute?.modelId,
      preservesProvider,
      preservesFamily,
      expectedCostUsd: candidate.costEstimateUsd ?? 0,
      expectedLatencyMs: candidate.latencyEstimateMs ?? 0,
    })
  }

  #failureCircuit(failure: FailoverFailure): void {
    const current = this.#circuits.get(failure.providerId) ??
      emptyCircuit(failure.providerId, "unknown")
    const consecutiveFailures = current.consecutiveFailures + 1
    const shouldOpen =
      consecutiveFailures >= 3 ||
      failure.credentialFailure ||
      failure.quotaExhausted ||
      !failure.retryable
    const retryDelay = Math.min(300000, 1000 * 2 ** Math.min(8, consecutiveFailures))
    const next: ProviderCircuit = Object.freeze({
      ...current,
      state: shouldOpen ? "open" : current.state,
      failureCount: current.failureCount + 1,
      consecutiveFailures,
      openedAt: shouldOpen ? failure.occurredAt : current.openedAt,
      retryAt: shouldOpen
        ? new Date(Date.parse(failure.occurredAt) + retryDelay).toISOString()
        : current.retryAt,
      lastFailureCode: failure.code,
      lastFailureReason: failure.reason,
      health: failure.rateLimited
        ? "rate-limited"
        : failure.quotaExhausted
          ? "exhausted"
          : "degraded",
    })
    this.#circuits.set(failure.providerId, next)
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

export function inferProviderFailure(input: {
  providerId: string
  modelId?: string
  routeId?: string
  code?: string
  reason?: string
  occurredAt?: string
  sourceEventId?: string
}): Omit<FailoverFailure, "id"> {
  const code = text(input.code, "provider_failure").toLowerCase()
  const reason = text(input.reason, "Provider route failed.")
  return Object.freeze({
    providerId: input.providerId,
    modelId: input.modelId,
    routeId: input.routeId,
    code,
    reason,
    retryable: !/credential|auth|forbidden|invalid_request|policy/.test(code),
    rateLimited: /rate.?limit|429/.test(code + reason.toLowerCase()),
    quotaExhausted: /quota|exhaust|insufficient_credit/.test(code + reason.toLowerCase()),
    credentialFailure: /credential|auth|unauthorized|401|403/.test(code + reason.toLowerCase()),
    capabilityFailure: /capability|unsupported|model_not_found/.test(code + reason.toLowerCase()),
    occurredAt: input.occurredAt ?? new Date().toISOString(),
    sourceEventId: input.sourceEventId,
  })
}

function normalizePolicy(input: Partial<FailoverPolicy>): FailoverPolicy {
  return Object.freeze({
    requiredCapabilities: Object.freeze(unique(
      (input.requiredCapabilities ?? []).map((item) => text(item)).filter(Boolean),
    )),
    excludedProviderIds: Object.freeze(unique(
      (input.excludedProviderIds ?? []).map((item) => text(item)).filter(Boolean),
    )),
    excludedModelIds: Object.freeze(unique(
      (input.excludedModelIds ?? []).map((item) => text(item)).filter(Boolean),
    )),
    preferredProviderIds: Object.freeze(unique(
      (input.preferredProviderIds ?? []).map((item) => text(item)).filter(Boolean),
    )),
    preferredModelIds: Object.freeze(unique(
      (input.preferredModelIds ?? []).map((item) => text(item)).filter(Boolean),
    )),
    maximumLatencyMs: positive(input.maximumLatencyMs),
    maximumCostUsd: positive(input.maximumCostUsd),
    minimumContextWindow: positive(input.minimumContextWindow),
    requireCredential: input.requireCredential !== false,
    allowDegraded: input.allowDegraded === true,
    allowRateLimited: input.allowRateLimited === true,
    allowUnknownHealth: input.allowUnknownHealth === true,
    preserveProvider: input.preserveProvider === true,
    preserveModelFamily: input.preserveModelFamily === true,
    maximumAttempts: bounded(Math.floor(input.maximumAttempts ?? 5), 1, 50),
    backoffBaseMs: bounded(Math.floor(input.backoffBaseMs ?? 250), 0, 60000),
  })
}

function preferenceScore(
  candidate: ProviderRouteCandidate,
  policy: FailoverPolicy,
): number {
  let score = 0.5
  const providerIndex = policy.preferredProviderIds.indexOf(candidate.providerId)
  const modelIndex = policy.preferredModelIds.indexOf(candidate.modelId)
  if (providerIndex >= 0) {
    score += 0.3 * (1 - providerIndex / Math.max(1, policy.preferredProviderIds.length))
  }
  if (modelIndex >= 0) {
    score += 0.2 * (1 - modelIndex / Math.max(1, policy.preferredModelIds.length))
  }
  return bounded(score, 0, 1)
}

function providerReliability(usage: readonly ProviderUsage[]): number {
  const requests = sum(usage.map((item) => item.requestCount))
  const failures = sum(usage.map((item) => item.failureCount))
  if (!requests) return 0.5
  return bounded(1 - ratio(failures, requests), 0, 1)
}

function transitionScore(
  candidate: ProviderRouteCandidate,
  currentRoute: ProviderRoute | undefined,
  currentModel: ModelRow | undefined,
  nextModel: ModelRow | undefined,
  failure: FailoverFailure,
): number {
  if (!currentRoute) return 0.75
  let score = 0
  if (candidate.providerId === currentRoute.providerId && !failure.credentialFailure) score += 0.25
  if (candidate.modelId === currentRoute.modelId && !failure.capabilityFailure) score += 0.25
  if (currentModel?.family && nextModel?.family === currentModel.family) score += 0.25
  if ((nextModel?.contextWindow ?? 0) >= (currentModel?.contextWindow ?? 0)) score += 0.25
  return score
}

function emptyCircuit(providerId: string, health: ProviderHealth): ProviderCircuit {
  return Object.freeze({
    providerId,
    state: "closed",
    failureCount: 0,
    successCount: 0,
    consecutiveFailures: 0,
    health,
  })
}

function positive(value: number | undefined): number | undefined {
  return value !== undefined && Number.isFinite(value) && value > 0 ? value : undefined
}

function circuitRank(state: ProviderCircuit["state"]): number {
  return ["open", "half-open", "closed"].indexOf(state)
}
