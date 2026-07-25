import type { CausalEventProjection } from "../../state/contracts.ts"
import type {
  CompactPreview,
  CompactSegment,
  ContextBudget,
  ContextCategory,
} from "./projection.ts"
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
} from "./value.ts"

export interface ContextSample {
  id: string
  sessionId: string
  revision: number
  compactEpoch: number
  used: number
  limit: number
  reserve: number
  categoryTokens: Readonly<Record<string, number>>
  eventCount: number
  toolResultCount: number
  memoryCount: number
  artifactCount: number
  capturedAt: string
  source: string
}

export interface ContextTrend {
  sessionId: string
  samples: readonly ContextSample[]
  usedDelta: number
  limitDelta: number
  reserveDelta: number
  epochDelta: number
  tokensPerMinute: number
  eventsPerMinute: number
  estimatedMinutesToReserve: number
  estimatedMinutesToLimit: number
  categoryDeltas: Readonly<Record<string, number>>
  growingCategories: readonly string[]
  shrinkingCategories: readonly string[]
  pressureTransitions: readonly string[]
  warnings: readonly string[]
}

export interface CompactImpact {
  previewFingerprint: string
  currentTokens: number
  projectedTokens: number
  savedTokens: number
  savedPercentage: number
  retainedPercentage: number
  protectedPercentage: number
  stalePercentage: number
  sourceDiversity: number
  categoryImpacts: Readonly<Record<string, {
    before: number
    after: number
    saved: number
    decisionCounts: Readonly<Record<string, number>>
  }>>
  largestSavings: readonly CompactSegment[]
  highestRisk: readonly CompactSegment[]
  warnings: readonly string[]
  confidence: number
}

export interface ContextAnomaly {
  id: string
  code:
    | "token-jump"
    | "limit-change"
    | "epoch-regression"
    | "epoch-without-savings"
    | "reserve-exhausted"
    | "category-mismatch"
    | "source-loss"
    | "compact-ineffective"
    | "restore-inflation"
  severity: "info" | "warning" | "error"
  message: string
  sampleId?: string
  previewFingerprint?: string
  categoryId?: string
  observed: number
  expected: number
}

export class ContextAnalyzer {
  readonly #samples = new Map<string, ContextSample[]>()
  readonly #impacts = new Map<string, CompactImpact>()
  #enabled = true
  #disabledReason = "Context analyzer is disabled."

  capture(input: {
    budget: ContextBudget
    revision: number
    events?: readonly CausalEventProjection[]
    memoryCount?: number
    artifactCount?: number
    capturedAt?: string
    source?: string
  }): ContextSample {
    this.#assertEnabled()
    const events = input.events ?? []
    const sample: ContextSample = Object.freeze({
      id: fingerprint([
        input.budget.sessionId,
        input.revision,
        input.budget.compactEpoch,
        input.budget.used,
        input.capturedAt,
      ]),
      sessionId: input.budget.sessionId,
      revision: input.revision,
      compactEpoch: input.budget.compactEpoch,
      used: input.budget.used,
      limit: input.budget.limit,
      reserve: input.budget.reserve,
      categoryTokens: Object.freeze(Object.fromEntries(
        input.budget.categories.map((category) => [category.id, category.tokens]),
      )),
      eventCount: events.length,
      toolResultCount: events.filter((event) => /tool.*result/i.test(event.eventType)).length,
      memoryCount: Math.max(0, Math.floor(input.memoryCount ?? 0)),
      artifactCount: Math.max(0, Math.floor(input.artifactCount ?? 0)),
      capturedAt: input.capturedAt ?? new Date().toISOString(),
      source: text(input.source, "canonical-projection"),
    })
    const samples = this.#samples.get(sample.sessionId) ?? []
    const previous = samples.at(-1)
    if (previous) {
      if (sample.revision < previous.revision) throw new Error("Context sample revision regressed.")
      if (sample.compactEpoch < previous.compactEpoch) throw new Error("Context compact epoch regressed.")
      if (
        sample.revision === previous.revision &&
        sample.compactEpoch === previous.compactEpoch &&
        contextSampleFingerprint(sample) !== contextSampleFingerprint(previous)
      ) throw new Error("Context sample conflicts at the same revision.")
      if (contextSampleFingerprint(sample) === contextSampleFingerprint(previous)) return previous
    }
    samples.push(sample)
    this.#samples.set(sample.sessionId, samples.slice(-5000))
    return sample
  }

  trend(sessionId: string, limit = 100): ContextTrend {
    this.#assertEnabled()
    const samples = (this.#samples.get(sessionId) ?? []).slice(-Math.max(2, limit))
    const first = samples[0]
    const last = samples.at(-1)
    if (!first || !last) {
      return Object.freeze({
        sessionId,
        samples: Object.freeze(samples),
        usedDelta: 0,
        limitDelta: 0,
        reserveDelta: 0,
        epochDelta: 0,
        tokensPerMinute: 0,
        eventsPerMinute: 0,
        estimatedMinutesToReserve: Number.POSITIVE_INFINITY,
        estimatedMinutesToLimit: Number.POSITIVE_INFINITY,
        categoryDeltas: Object.freeze({}),
        growingCategories: Object.freeze([]),
        shrinkingCategories: Object.freeze([]),
        pressureTransitions: Object.freeze([]),
        warnings: Object.freeze(["insufficient context samples"]),
      })
    }
    const elapsedMinutes = Math.max(
      1 / 60000,
      (Date.parse(last.capturedAt) - Date.parse(first.capturedAt)) / 60000,
    )
    const usedDelta = last.used - first.used
    const tokensPerMinute = usedDelta / elapsedMinutes
    const eventsPerMinute = (last.eventCount - first.eventCount) / elapsedMinutes
    const categoryIds = unique(samples.flatMap((sample) => Object.keys(sample.categoryTokens)))
    const categoryDeltas = Object.fromEntries(categoryIds.map((id) => [
      id,
      (last.categoryTokens[id] ?? 0) - (first.categoryTokens[id] ?? 0),
    ]))
    const growingCategories = categoryIds
      .filter((id) => categoryDeltas[id]! > 0)
      .sort((left, right) => categoryDeltas[right]! - categoryDeltas[left]!)
    const shrinkingCategories = categoryIds
      .filter((id) => categoryDeltas[id]! < 0)
      .sort((left, right) => categoryDeltas[left]! - categoryDeltas[right]!)
    const pressureTransitions = unique(samples.map((sample) =>
      pressureFor(sample.used, sample.limit, sample.reserve)))
    const warnings: string[] = []
    if (tokensPerMinute > Math.max(1000, last.limit * 0.02)) warnings.push("context is growing rapidly")
    if (last.used >= last.limit - last.reserve) warnings.push("context entered compact reserve")
    if (last.limit !== first.limit) warnings.push("context limit changed during trend window")
    if (last.compactEpoch > first.compactEpoch && usedDelta >= 0) {
      warnings.push("compact epoch advanced without net token savings")
    }
    return Object.freeze({
      sessionId,
      samples: Object.freeze(samples),
      usedDelta,
      limitDelta: last.limit - first.limit,
      reserveDelta: last.reserve - first.reserve,
      epochDelta: last.compactEpoch - first.compactEpoch,
      tokensPerMinute,
      eventsPerMinute,
      estimatedMinutesToReserve: estimateMinutes(
        last.limit - last.reserve - last.used,
        tokensPerMinute,
      ),
      estimatedMinutesToLimit: estimateMinutes(last.limit - last.used, tokensPerMinute),
      categoryDeltas: Object.freeze(categoryDeltas),
      growingCategories: Object.freeze(growingCategories),
      shrinkingCategories: Object.freeze(shrinkingCategories),
      pressureTransitions: Object.freeze(pressureTransitions),
      warnings: Object.freeze(warnings),
    })
  }

  impact(preview: CompactPreview, categories: readonly ContextCategory[]): CompactImpact {
    this.#assertEnabled()
    const categoryBySource = new Map<string, string>()
    for (const category of categories) {
      for (const sourceId of category.sourceIds) categoryBySource.set(sourceId, category.id)
    }
    const categoryImpacts: Record<string, {
      before: number
      after: number
      saved: number
      decisionCounts: Readonly<Record<string, number>>
    }> = {}
    for (const category of categories) {
      categoryImpacts[category.id] = {
        before: category.tokens,
        after: category.tokens,
        saved: 0,
        decisionCounts: Object.freeze({}),
      }
    }
    for (const segment of preview.segments) {
      const categoryIds = unique(
        segment.sourceIds.map((sourceId) => categoryBySource.get(sourceId) ?? "").filter(Boolean),
      )
      const targets = categoryIds.length ? categoryIds : [segment.kind]
      for (const categoryId of targets) {
        const current = categoryImpacts[categoryId] ?? {
          before: 0,
          after: 0,
          saved: 0,
          decisionCounts: Object.freeze({}),
        }
        const weight = 1 / targets.length
        const saved = segmentSavings(segment) * weight
        const decisions = { ...current.decisionCounts }
        decisions[segment.decision] = (decisions[segment.decision] ?? 0) + 1
        categoryImpacts[categoryId] = {
          before: current.before || segment.tokens * weight,
          after: Math.max(0, current.after - saved),
          saved: current.saved + saved,
          decisionCounts: Object.freeze(decisions),
        }
      }
    }
    const largestSavings = sortStable(
      preview.segments.filter((segment) => segmentSavings(segment) > 0),
      (left, right) =>
        compareNumber(segmentSavings(right), segmentSavings(left)) ||
        compareText(left.id, right.id),
    ).slice(0, 20)
    const highestRisk = sortStable(
      preview.segments.filter((segment) =>
        segment.decision !== "retain" || segment.stale || segment.protected),
      (left, right) =>
        compareNumber(segmentRisk(right), segmentRisk(left)) ||
        compareText(left.id, right.id),
    ).slice(0, 20)
    const protectedTokens = sum(preview.segments.filter((segment) => segment.protected).map((segment) => segment.tokens))
    const staleTokens = sum(preview.segments.filter((segment) => segment.stale).map((segment) => segment.tokens))
    const sourceDiversity = new Set(preview.segments.flatMap((segment) => segment.sourceIds)).size
    const warnings: string[] = []
    if (preview.savedTokens > 0 && preview.savedTokens < preview.currentTokens * 0.05) {
      warnings.push("compact saves less than five percent")
    }
    if (highestRisk.some((segment) => segment.protected && segment.decision !== "retain")) {
      warnings.push("protected segment is not retained")
    }
    if (preview.droppedSourceIds.length && sourceDiversity < 3) {
      warnings.push("compact drops sources from a low-diversity context")
    }
    if (preview.projectedTokens > preview.targetTokens) warnings.push("compact does not reach target")
    const confidence = bounded(
      (preview.eligible ? 0.25 : 0) +
      (preview.fingerprint ? 0.2 : 0) +
      (preview.segments.length ? 0.2 : 0) +
      Math.min(0.2, sourceDiversity * 0.02) +
      (warnings.length ? 0 : 0.15),
      0,
      1,
    )
    const impact: CompactImpact = Object.freeze({
      previewFingerprint: preview.fingerprint,
      currentTokens: preview.currentTokens,
      projectedTokens: preview.projectedTokens,
      savedTokens: preview.savedTokens,
      savedPercentage: ratio(preview.savedTokens, preview.currentTokens),
      retainedPercentage: ratio(preview.retainedTokens, preview.currentTokens),
      protectedPercentage: ratio(protectedTokens, preview.currentTokens),
      stalePercentage: ratio(staleTokens, preview.currentTokens),
      sourceDiversity,
      categoryImpacts: Object.freeze(categoryImpacts),
      largestSavings: Object.freeze(largestSavings),
      highestRisk: Object.freeze(highestRisk),
      warnings: Object.freeze(warnings),
      confidence,
    })
    this.#impacts.set(impact.previewFingerprint, impact)
    return impact
  }

  anomalies(sessionId: string, preview?: CompactPreview): readonly ContextAnomaly[] {
    this.#assertEnabled()
    const samples = this.#samples.get(sessionId) ?? []
    const anomalies: ContextAnomaly[] = []
    for (let index = 1; index < samples.length; index += 1) {
      const previous = samples[index - 1]!
      const current = samples[index]!
      const typicalDeltas = samples.slice(1, index + 1).map((sample, offset) =>
        sample.used - samples[offset]!.used)
      const threshold = Math.max(
        1000,
        percentile(typicalDeltas.map(Math.abs), 0.95) * 2,
        previous.limit * 0.05,
      )
      const tokenDelta = current.used - previous.used
      if (Math.abs(tokenDelta) > threshold) {
        anomalies.push(anomaly("token-jump", "warning", `Context changed by ${tokenDelta} tokens.`, current.id, tokenDelta, threshold))
      }
      if (current.limit !== previous.limit) {
        anomalies.push(anomaly("limit-change", "info", "Context limit changed.", current.id, current.limit, previous.limit))
      }
      if (current.compactEpoch < previous.compactEpoch) {
        anomalies.push(anomaly("epoch-regression", "error", "Compact epoch regressed.", current.id, current.compactEpoch, previous.compactEpoch))
      }
      if (current.compactEpoch > previous.compactEpoch && tokenDelta >= 0) {
        anomalies.push(anomaly("epoch-without-savings", "warning", "Compact epoch advanced without savings.", current.id, tokenDelta, -1))
      }
      if (current.used >= current.limit - current.reserve) {
        anomalies.push(anomaly("reserve-exhausted", "error", "Context entered reserved compact capacity.", current.id, current.used, current.limit - current.reserve))
      }
      const categoryTotal = sum(Object.values(current.categoryTokens))
      if (Math.abs(categoryTotal - current.used) > Math.max(1000, current.used * 0.1)) {
        anomalies.push(anomaly("category-mismatch", "warning", "Category tokens differ from total context.", current.id, categoryTotal, current.used))
      }
    }
    if (preview) {
      if (preview.savedTokens <= 0) {
        anomalies.push(anomaly("compact-ineffective", "warning", "Compact preview saves no tokens.", undefined, preview.savedTokens, 1, preview.fingerprint))
      }
      if (preview.restoreTokens > preview.savedTokens) {
        anomalies.push(anomaly("restore-inflation", "warning", "Restore tokens exceed compact savings.", undefined, preview.restoreTokens, preview.savedTokens, preview.fingerprint))
      }
      if (!preview.retainedSourceIds.length && preview.currentTokens > 0) {
        anomalies.push(anomaly("source-loss", "error", "Compact preview retains no source references.", undefined, 0, 1, preview.fingerprint))
      }
    }
    return Object.freeze(anomalies)
  }

  latest(sessionId: string): ContextSample | undefined {
    this.#assertEnabled()
    return this.#samples.get(sessionId)?.at(-1)
  }

  samples(sessionId: string): readonly ContextSample[] {
    this.#assertEnabled()
    return Object.freeze([...(this.#samples.get(sessionId) ?? [])])
  }

  getImpact(previewFingerprint: string): CompactImpact | undefined {
    this.#assertEnabled()
    return this.#impacts.get(previewFingerprint)
  }

  disable(reason = "Context analyzer is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Context analyzer is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  clear(sessionId?: string): void {
    this.#assertEnabled()
    if (sessionId) this.#samples.delete(sessionId)
    else this.#samples.clear()
    if (!sessionId) this.#impacts.clear()
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

function pressureFor(used: number, limit: number, reserve: number): string {
  if (used >= limit) return "exceeded"
  if (limit - used <= Math.max(512, reserve * 0.25)) return "critical"
  if (limit - used <= reserve) return "compact-soon"
  if (ratio(used, limit) >= 0.7) return "watch"
  return "nominal"
}

function estimateMinutes(available: number, rate: number): number {
  if (available <= 0) return 0
  if (rate <= 0) return Number.POSITIVE_INFINITY
  return available / rate
}

function segmentSavings(segment: CompactSegment): number {
  if (segment.decision === "drop") return segment.tokens
  if (segment.decision === "summarize") return Math.max(0, segment.tokens - Math.max(64, Math.round(segment.tokens * 0.18)))
  return 0
}

function segmentRisk(segment: CompactSegment): number {
  let risk = 0
  if (segment.protected && segment.decision !== "retain") risk += 1
  if (segment.decision === "drop") risk += 0.5
  if (segment.decision === "summarize") risk += 0.25
  if (segment.stale) risk -= 0.1
  if (segment.sourceIds.length === 0) risk += 0.2
  return risk
}

function contextSampleFingerprint(sample: ContextSample): string {
  return fingerprint([
    sample.sessionId,
    sample.revision,
    sample.compactEpoch,
    sample.used,
    sample.limit,
    sample.reserve,
    sample.categoryTokens,
    sample.eventCount,
    sample.toolResultCount,
    sample.memoryCount,
    sample.artifactCount,
    sample.source,
  ])
}

function anomaly(
  code: ContextAnomaly["code"],
  severity: ContextAnomaly["severity"],
  message: string,
  sampleId: string | undefined,
  observed: number,
  expected: number,
  previewFingerprint?: string,
): ContextAnomaly {
  return Object.freeze({
    id: fingerprint([code, sampleId, previewFingerprint, observed, expected]),
    code,
    severity,
    message,
    sampleId,
    previewFingerprint,
    observed,
    expected,
  })
}
