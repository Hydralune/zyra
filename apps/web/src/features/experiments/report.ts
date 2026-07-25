import type { ExperimentRegistryProjection } from "../../api/experiment-api.ts"

export interface MetricRow {
  metric: string
  variantId: string
  unit: string
  count: number
  p50?: number
  p95?: number
  mean?: number
  standardDeviation?: number
  medianAbsoluteDeviation?: number
  interquartileRange?: number
  confidenceLower?: number
  confidenceUpper?: number
}

export interface ComparisonRow {
  metric: string
  baselineVariantId: string
  comparedVariantId: string
  absoluteDelta?: number
  relativeDelta?: number
  effectDirection: string
  confidenceOverlap?: boolean
}

function record(value: unknown): Readonly<Record<string, any>> {
  return value && typeof value === "object"
    ? value as Readonly<Record<string, any>>
    : {}
}

function records(value: unknown): readonly Readonly<Record<string, any>>[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is Readonly<Record<string, any>> =>
          Boolean(item) && typeof item === "object",
      )
    : []
}

function optionalNumber(value: unknown): number | undefined {
  const selected = Number(value)
  return Number.isFinite(selected) ? selected : undefined
}

export function unwrapReport(value: unknown): Readonly<Record<string, any>> {
  const response = record(value)
  return record(response.report ?? response)
}

export function metricRows(value: unknown): readonly MetricRow[] {
  const report = unwrapReport(value)
  const statistics = record(report.statistics)
  return Object.freeze(records(statistics.summaries).map((item) => {
    const confidence = record(item.confidence)
    return Object.freeze({
      metric: String(item.metric || ""),
      variantId: String(item.variant_id || ""),
      unit: String(item.unit || ""),
      count: Number(item.count || 0),
      p50: optionalNumber(item.p50),
      p95: optionalNumber(item.p95),
      mean: optionalNumber(item.mean),
      standardDeviation: optionalNumber(item.standard_deviation),
      medianAbsoluteDeviation: optionalNumber(item.median_absolute_deviation),
      interquartileRange: optionalNumber(item.interquartile_range),
      confidenceLower: optionalNumber(confidence.lower),
      confidenceUpper: optionalNumber(confidence.upper),
    })
  }))
}

export function comparisonRows(value: unknown): readonly ComparisonRow[] {
  const report = unwrapReport(value)
  const statistics = record(report.statistics)
  return Object.freeze(records(statistics.comparisons).map((item) =>
    Object.freeze({
      metric: String(item.metric || ""),
      baselineVariantId: String(item.baseline_variant_id || ""),
      comparedVariantId: String(item.compared_variant_id || ""),
      absoluteDelta: optionalNumber(item.absolute_delta),
      relativeDelta: optionalNumber(item.relative_delta),
      effectDirection: String(item.effect_direction || item.interpretation || ""),
      confidenceOverlap:
        typeof item.confidence_overlap === "boolean"
          ? item.confidence_overlap
          : undefined,
    })
  ))
}

export interface RequirementRow {
  requirementId: string
  score: number
  verified: boolean
  title: string
  metrics: readonly string[]
  variants: readonly string[]
  evidenceIds: readonly string[]
  findings: readonly Readonly<Record<string, any>>[]
}

export function requirementRows(
  value: unknown,
  registry?: ExperimentRegistryProjection,
): readonly RequirementRow[] {
  const report = unwrapReport(value)
  const requirementBlock = record(report.requirements)
  const definitions = new Map(
    (registry?.requirements ?? []).map((item) => [item.requirement_id, item]),
  )
  return Object.freeze(records(requirementBlock.rows).map((item) => {
    const requirementId = String(item.requirement_id || "")
    const definition = definitions.get(requirementId)
    return Object.freeze({
      requirementId,
      score: Number(item.score ?? definition?.score ?? 0),
      verified: item.verified === true,
      title: definition?.title ?? requirementId,
      metrics: Object.freeze(
        (Array.isArray(item.metric_names) ? item.metric_names : [])
          .map(String),
      ),
      variants: Object.freeze(
        (Array.isArray(item.variant_ids) ? item.variant_ids : [])
          .map(String),
      ),
      evidenceIds: Object.freeze(
        (Array.isArray(item.evidence_ids) ? item.evidence_ids : [])
          .map(String),
      ),
      findings: Object.freeze(records(item.findings)),
    })
  }))
}

export function reportHeadline(value: unknown): {
  rawSamples: number
  summaries: number
  comparisons: number
  scoreTotal: number
  scoreVerified: number
  requirementsVerified: number
  requirementsTotal: number
} {
  const report = unwrapReport(value)
  const statistics = record(report.statistics)
  const requirements = record(report.requirements)
  return Object.freeze({
    rawSamples: Number(statistics.raw_sample_count || 0),
    summaries: Number(statistics.summary_count || 0),
    comparisons: Number(statistics.comparison_count || 0),
    scoreTotal: Number(requirements.score_total || 0),
    scoreVerified: Number(requirements.score_verified || 0),
    requirementsVerified: Number(requirements.verified_count || 0),
    requirementsTotal: Number(requirements.requirement_count || 0),
  })
}
