import type {
  ExperimentRegistryProjection,
  ExperimentRunProjection,
  ExperimentStatusProjection,
} from "../../api/experiment-api.ts"

export const REQUIRED_BASELINES = Object.freeze([
  "single_agent",
  "static_full_connect_multi_agent",
  "dynamic_heterogeneous_swarm",
])

export const REQUIRED_ABLATIONS = Object.freeze([
  "no_scheduler",
  "no_memory_compact",
  "no_recovery",
  "no_low_entropy_communication",
])

export const REQUIRED_REQUIREMENTS = Object.freeze([
  "REQ-CLOSE-01",
  "REQ-MEM-01",
  "REQ-TOPO-01",
  "REQ-COMM-01",
  "REQ-EDGE-01",
  "REQ-FAULT-01",
  "REQ-TRACE-01",
  "SCORE-LOOP",
  "SCORE-ORG",
  "SCORE-TASKS",
  "SCORE-SCENE",
  "SCORE-VALUE",
  "SCORE-UX",
  "SCORE-NOISE",
  "SCORE-ALGO",
  "SCORE-ROBUST",
  "SCORE-EFF",
  "SCORE-COMPAT",
])

export interface ExperimentAdmissionFinding {
  code: string
  message: string
}

export interface ExperimentAdmission {
  valid: boolean
  findings: readonly ExperimentAdmissionFinding[]
}

function finding(code: string, message: string): ExperimentAdmissionFinding {
  return Object.freeze({ code, message })
}

function records(value: unknown): readonly Readonly<Record<string, any>>[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is Readonly<Record<string, any>> =>
          Boolean(item) && typeof item === "object",
      )
    : []
}

export function assessRegistry(
  registry: ExperimentRegistryProjection,
): ExperimentAdmission {
  const findings: ExperimentAdmissionFinding[] = []
  const variants = new Set(registry.variants.map((item) => item.variant_id))
  for (const variantId of [...REQUIRED_BASELINES, ...REQUIRED_ABLATIONS]) {
    if (!variants.has(variantId)) {
      findings.push(finding("variant_missing", `Missing matrix variant ${variantId}.`))
    }
  }
  if (registry.variants.length !== 7) {
    findings.push(
      finding("variant_count_invalid", "The formal matrix must contain exactly seven variants."),
    )
  }
  const requirementIds = new Set(
    registry.requirements.map((item) => item.requirement_id),
  )
  for (const requirementId of REQUIRED_REQUIREMENTS) {
    if (!requirementIds.has(requirementId)) {
      findings.push(
        finding(
          "requirement_missing",
          `Requirement ${requirementId} is absent from the authoritative registry.`,
        ),
      )
    }
  }
  const score = registry.requirements.reduce((total, item) => total + item.score, 0)
  if (score !== 100) {
    findings.push(finding("score_total_invalid", `Requirement score is ${score}, not 100.`))
  }
  if (registry.metrics.length < 20) {
    findings.push(
      finding("metric_catalog_incomplete", "The formal metric catalog is incomplete."),
    )
  }
  const requiredCapabilities = [
    "baseline_matrix",
    "ablation_matrix",
    "raw_samples",
    "p50_p95",
    "dispersion",
    "confidence",
    "tamper_evident_bundle",
    "reviewer_navigation",
  ]
  for (const capability of requiredCapabilities) {
    if (registry.capabilities[capability] !== true) {
      findings.push(
        finding("capability_disabled", `Registry capability ${capability} is disabled.`),
      )
    }
  }
  if (registry.capabilities.authenticated_provider_cli_allowed !== false) {
    findings.push(
      finding(
        "provider_cli_boundary_invalid",
        "The M2 exit registry must prohibit authenticated provider CLI invocation.",
      ),
    )
  }
  return Object.freeze({
    valid: findings.length === 0,
    findings: Object.freeze(findings),
  })
}

export function assessRun(run: ExperimentRunProjection): ExperimentAdmission {
  const findings: ExperimentAdmissionFinding[] = []
  const expectedCells = run.variants.length * run.repetitions
  if (run.repetitions < 3) {
    findings.push(finding("repetitions_insufficient", "At least three repetitions are required."))
  }
  if (run.cells.length !== expectedCells) {
    findings.push(
      finding(
        "cell_count_invalid",
        `Expected ${expectedCells} matrix cells but received ${run.cells.length}.`,
      ),
    )
  }
  if (run.envelope.seed_plan.length !== run.repetitions) {
    findings.push(finding("seed_plan_invalid", "Seed plan and repetition count differ."))
  }
  if (new Set(run.envelope.seed_plan).size !== run.envelope.seed_plan.length) {
    findings.push(finding("seed_plan_duplicate", "Experiment seeds must be unique."))
  }
  const digests = [
    run.envelope.envelope_digest,
    run.envelope.source_evidence_digest,
    run.envelope.sealed_policy_digest,
    run.envelope.environment_digest,
  ]
  if (digests.some((item) => !/^[a-f0-9]{64}$/i.test(item))) {
    findings.push(finding("envelope_digest_invalid", "One or more immutable digests are invalid."))
  }
  if (run.phase === "succeeded") {
    const incomplete = run.cells.filter(
      (cell) =>
        cell.phase !== "succeeded"
        || !cell.observation_digest
        || cell.sample_ids.length === 0,
    )
    if (incomplete.length) {
      findings.push(
        finding(
          "succeeded_cell_incomplete",
          `${incomplete.length} succeeded-run cells lack observations or samples.`,
        ),
      )
    }
    if (!run.report || !run.bundle_manifest || !run.verification_receipt) {
      findings.push(
        finding(
          "terminal_evidence_missing",
          "Succeeded experiment lacks report, bundle manifest, or verification receipt.",
        ),
      )
    }
  }
  return Object.freeze({
    valid: findings.length === 0,
    findings: Object.freeze(findings),
  })
}

export function assessStatus(
  status: ExperimentStatusProjection,
): ExperimentAdmission {
  const findings = [...assessRun(status.run).findings]
  if (status.browser_connection_required !== false) {
    findings.push(
      finding(
        "browser_custody_invalid",
        "Backend experiment custody must not depend on the browser connection.",
      ),
    )
  }
  const receiptKinds = new Set(
    status.receipts.map((item) => String(item.schema || "")),
  )
  if (
    status.run.phase === "succeeded"
    && ![...receiptKinds].some((item) => item.includes("verification"))
  ) {
    findings.push(finding("verification_receipt_missing", "Verification receipt is absent."))
  }
  const invalid = status.receipts.filter((item) => item.valid === false)
  if (status.run.phase === "succeeded" && invalid.length) {
    findings.push(
      finding(
        "invalid_receipt",
        `${invalid.length} receipt(s) explicitly failed verification.`,
      ),
    )
  }
  return Object.freeze({
    valid: findings.length === 0,
    findings: Object.freeze(findings),
  })
}

export function assessReport(value: unknown): ExperimentAdmission {
  const findings: ExperimentAdmissionFinding[] = []
  const response =
    value && typeof value === "object" ? value as Readonly<Record<string, any>> : {}
  const report =
    response.report && typeof response.report === "object"
      ? response.report as Readonly<Record<string, any>>
      : response
  const matrix =
    report.matrix && typeof report.matrix === "object"
      ? report.matrix as Readonly<Record<string, any>>
      : {}
  const statistics =
    report.statistics && typeof report.statistics === "object"
      ? report.statistics as Readonly<Record<string, any>>
      : {}
  const requirements =
    report.requirements && typeof report.requirements === "object"
      ? report.requirements as Readonly<Record<string, any>>
      : {}
  if (records(matrix.variant_cards).length !== 7) {
    findings.push(finding("report_matrix_invalid", "Report does not contain seven variant cards."))
  }
  for (const key of [
    "raw_sample_count",
    "p50_present_count",
    "p95_present_count",
    "dispersion_present_count",
    "confidence_present_count",
  ]) {
    if (Number(statistics[key] || 0) <= 0) {
      findings.push(finding("report_statistic_missing", `Report statistic ${key} is absent.`))
    }
  }
  if (Number(requirements.score_total || 0) !== 100) {
    findings.push(finding("report_score_invalid", "Report score total is not 100."))
  }
  if (
    Number(requirements.verified_count || 0)
    !== Number(requirements.requirement_count || -1)
  ) {
    findings.push(
      finding("report_requirement_incomplete", "Not every requirement is verified."),
    )
  }
  if (!report.reviewer_navigation || !report.report_digest) {
    findings.push(
      finding("reviewer_navigation_missing", "Report reviewer navigation is incomplete."),
    )
  }
  return Object.freeze({
    valid: findings.length === 0,
    findings: Object.freeze(findings),
  })
}
