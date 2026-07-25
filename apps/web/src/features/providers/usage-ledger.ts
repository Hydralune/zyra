import type { ProviderConsoleProjection, ProviderUsage } from "./projection.ts"
import {
  bounded,
  compareNumber,
  compareText,
  fingerprint,
  mean,
  percentile,
  ratio,
  sortStable,
  sum,
  text,
  unique,
} from "../session/value.ts"

export interface UsageSample {
  id: string
  providerId: string
  modelId?: string
  inputTokens: number
  outputTokens: number
  cachedTokens: number
  costUsd: number
  latencyMs: number
  success: boolean
  failureCode?: string
  routeId?: string
  eventId?: string
  capturedAt: string
}

export interface UsageWindow {
  id: string
  providerId: string
  modelId?: string
  from: string
  to: string
  requestCount: number
  successCount: number
  failureCount: number
  inputTokens: number
  outputTokens: number
  cachedTokens: number
  cacheHitRate: number
  costUsd: number
  averageCostUsd: number
  latencyP50Ms: number
  latencyP95Ms: number
  latencyP99Ms: number
  throughputTokensPerSecond: number
  successRate: number
  failureCodes: Readonly<Record<string, number>>
  routeIds: readonly string[]
  eventIds: readonly string[]
}

export interface UsageBudget {
  maximumCostUsd?: number
  maximumRequests?: number
  maximumInputTokens?: number
  maximumOutputTokens?: number
  maximumAverageLatencyMs?: number
  minimumSuccessRate?: number
}

export interface UsageBudgetDecision {
  allowed: boolean
  reasons: readonly string[]
  warnings: readonly string[]
  projectedCostUsd: number
  projectedRequests: number
  projectedInputTokens: number
  projectedOutputTokens: number
  projectedAverageLatencyMs: number
  projectedSuccessRate: number
}

export class ProviderUsageLedger {
  readonly #samples: UsageSample[] = []
  #enabled = true
  #disabledReason = "Provider usage ledger is disabled."

  append(input: Omit<UsageSample, "id"> & { id?: string }): UsageSample {
    this.#assertEnabled()
    const sample: UsageSample = Object.freeze({
      ...input,
      id: input.id ?? fingerprint([
        input.providerId,
        input.modelId,
        input.routeId,
        input.eventId,
        input.capturedAt,
        this.#samples.length,
      ]),
      inputTokens: Math.max(0, Math.floor(input.inputTokens)),
      outputTokens: Math.max(0, Math.floor(input.outputTokens)),
      cachedTokens: Math.max(0, Math.floor(input.cachedTokens)),
      costUsd: Math.max(0, input.costUsd),
      latencyMs: Math.max(0, input.latencyMs),
    })
    const existing = this.#samples.find((entry) => entry.id === sample.id)
    if (existing) {
      if (fingerprint([existing]) !== fingerprint([sample])) {
        throw new Error("Usage sample identity conflicts with existing payload.")
      }
      return existing
    }
    this.#samples.push(sample)
    if (this.#samples.length > 100000) {
      this.#samples.splice(0, this.#samples.length - 100000)
    }
    return sample
  }

  ingest(projection: ProviderConsoleProjection, capturedAt = new Date().toISOString()): readonly UsageSample[] {
    this.#assertEnabled()
    const samples = projection.usage.map((usage, index) =>
      this.append(sampleFromUsage(usage, capturedAt, projection.revision, index)))
    return Object.freeze(samples)
  }

  window(input: {
    providerId: string
    modelId?: string
    from?: string
    to?: string
  }): UsageWindow {
    this.#assertEnabled()
    const fromMs = input.from ? Date.parse(input.from) : Number.NEGATIVE_INFINITY
    const toMs = input.to ? Date.parse(input.to) : Number.POSITIVE_INFINITY
    const samples = this.#samples.filter((sample) => {
      const timestamp = Date.parse(sample.capturedAt)
      return sample.providerId === input.providerId &&
        (!input.modelId || sample.modelId === input.modelId) &&
        timestamp >= fromMs &&
        timestamp <= toMs
    })
    const latencies = samples.map((sample) => sample.latencyMs)
    const inputTokens = sum(samples.map((sample) => sample.inputTokens))
    const outputTokens = sum(samples.map((sample) => sample.outputTokens))
    const cachedTokens = sum(samples.map((sample) => sample.cachedTokens))
    const costUsd = sum(samples.map((sample) => sample.costUsd))
    const successes = samples.filter((sample) => sample.success)
    const failures = samples.filter((sample) => !sample.success)
    const durationSeconds = Math.max(
      0.001,
      (Math.max(...samples.map((sample) => Date.parse(sample.capturedAt)), 0) -
        Math.min(...samples.map((sample) => Date.parse(sample.capturedAt)), 0)) / 1000,
    )
    const failureCodes: Record<string, number> = {}
    for (const sample of failures) {
      const code = sample.failureCode ?? "unknown"
      failureCodes[code] = (failureCodes[code] ?? 0) + 1
    }
    return Object.freeze({
      id: fingerprint([input, samples.map((sample) => sample.id)]),
      providerId: input.providerId,
      modelId: input.modelId,
      from: input.from ?? samples[0]?.capturedAt ?? "",
      to: input.to ?? samples.at(-1)?.capturedAt ?? "",
      requestCount: samples.length,
      successCount: successes.length,
      failureCount: failures.length,
      inputTokens,
      outputTokens,
      cachedTokens,
      cacheHitRate: ratio(cachedTokens, Math.max(1, inputTokens)),
      costUsd,
      averageCostUsd: samples.length ? costUsd / samples.length : 0,
      latencyP50Ms: percentile(latencies, 0.5),
      latencyP95Ms: percentile(latencies, 0.95),
      latencyP99Ms: percentile(latencies, 0.99),
      throughputTokensPerSecond: (inputTokens + outputTokens) / durationSeconds,
      successRate: ratio(successes.length, samples.length),
      failureCodes: Object.freeze(failureCodes),
      routeIds: Object.freeze(unique(samples.map((sample) => sample.routeId ?? "").filter(Boolean))),
      eventIds: Object.freeze(unique(samples.map((sample) => sample.eventId ?? "").filter(Boolean))),
    })
  }

  windows(): readonly UsageWindow[] {
    this.#assertEnabled()
    const keys = unique(this.#samples.map((sample) =>
      `${sample.providerId}\u001f${sample.modelId ?? ""}`))
    return Object.freeze(sortStable(keys.map((key) => {
      const [providerId, modelId] = key.split("\u001f")
      return this.window({ providerId: providerId!, modelId: modelId || undefined })
    }), (left, right) =>
      compareNumber(right.costUsd, left.costUsd) ||
      compareNumber(right.requestCount, left.requestCount) ||
      compareText(left.id, right.id)))
  }

  admit(
    window: UsageWindow,
    budget: UsageBudget,
    next: {
      inputTokens?: number
      outputTokens?: number
      costUsd?: number
      latencyMs?: number
    } = {},
  ): UsageBudgetDecision {
    this.#assertEnabled()
    const projectedCostUsd = window.costUsd + Math.max(0, next.costUsd ?? window.averageCostUsd)
    const projectedRequests = window.requestCount + 1
    const projectedInputTokens = window.inputTokens + Math.max(0, next.inputTokens ?? 0)
    const projectedOutputTokens = window.outputTokens + Math.max(0, next.outputTokens ?? 0)
    const projectedAverageLatencyMs = mean([
      window.latencyP50Ms,
      Math.max(0, next.latencyMs ?? window.latencyP50Ms),
    ])
    const projectedSuccessRate = ratio(window.successCount + 1, projectedRequests)
    const reasons: string[] = []
    const warnings: string[] = []
    if (budget.maximumCostUsd && projectedCostUsd > budget.maximumCostUsd) reasons.push("cost budget exceeded")
    if (budget.maximumRequests && projectedRequests > budget.maximumRequests) reasons.push("request budget exceeded")
    if (budget.maximumInputTokens && projectedInputTokens > budget.maximumInputTokens) reasons.push("input token budget exceeded")
    if (budget.maximumOutputTokens && projectedOutputTokens > budget.maximumOutputTokens) reasons.push("output token budget exceeded")
    if (
      budget.maximumAverageLatencyMs &&
      projectedAverageLatencyMs > budget.maximumAverageLatencyMs
    ) reasons.push("latency budget exceeded")
    if (budget.minimumSuccessRate && projectedSuccessRate < budget.minimumSuccessRate) {
      reasons.push("success rate below minimum")
    }
    if (budget.maximumCostUsd && projectedCostUsd >= budget.maximumCostUsd * 0.9) warnings.push("cost budget above ninety percent")
    if (budget.maximumRequests && projectedRequests >= budget.maximumRequests * 0.9) warnings.push("request budget above ninety percent")
    if (window.failureCount > 0 && window.successRate < 0.9) warnings.push("provider reliability is degraded")
    if (window.cacheHitRate < 0.1 && window.cachedTokens === 0) warnings.push("provider cache is unused")
    return Object.freeze({
      allowed: reasons.length === 0,
      reasons: Object.freeze(reasons),
      warnings: Object.freeze(warnings),
      projectedCostUsd,
      projectedRequests,
      projectedInputTokens,
      projectedOutputTokens,
      projectedAverageLatencyMs,
      projectedSuccessRate,
    })
  }

  samples(providerId?: string): readonly UsageSample[] {
    this.#assertEnabled()
    return Object.freeze(this.#samples.filter((sample) =>
      !providerId || sample.providerId === providerId))
  }

  disable(reason = "Provider usage ledger is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Provider usage ledger is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  clear(): void {
    this.#assertEnabled()
    this.#samples.splice(0)
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

function sampleFromUsage(
  usage: ProviderUsage,
  capturedAt: string,
  revision: number,
  index: number,
): Omit<UsageSample, "id"> {
  return Object.freeze({
    providerId: usage.providerId,
    modelId: usage.modelId,
    inputTokens: usage.inputTokens,
    outputTokens: usage.outputTokens,
    cachedTokens: usage.cachedTokens,
    costUsd: usage.costUsd,
    latencyMs: usage.averageLatencyMs,
    success: usage.failureCount === 0,
    failureCode: usage.failureCount ? "projected_failure" : undefined,
    eventId: usage.sourceEventIds.at(-1),
    capturedAt,
    routeId: `projection:${revision}:${index}`,
  })
}
