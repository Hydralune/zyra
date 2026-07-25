import type { ScenarioRunProjection } from "../../api/scenario-api.ts"

export interface EvidenceFinding {
  code: string
  message: string
  path: string
  severity: "error" | "warning"
}
export interface EvidenceAssessment {
  valid: boolean
  manifestId?: string
  manifestDigest?: string
  effectiveStepCount: number
  excludedStepCount: number
  invalidStepCount: number
  artifactCount: number
  canonicalEventCount: number
  rawSampleCount: number
  effects: Readonly<Record<string, number>>
  stages: Readonly<Record<string, number>>
  providers: Readonly<Record<string, number>>
  findings: readonly EvidenceFinding[]
}

const SHA256 = /^[a-f0-9]{64}$/

function object(value: unknown): Readonly<Record<string, any>> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Readonly<Record<string, any>>
    : {}
}

function array(value: unknown): readonly any[] {
  return Array.isArray(value) ? value : []
}

function finding(
  code: string,
  message: string,
  path: string,
  severity: EvidenceFinding["severity"] = "error",
): EvidenceFinding {
  return Object.freeze({ code, message, path, severity })
}

export function assessEvidence(run: ScenarioRunProjection): EvidenceAssessment {
  const manifest = object(run.evidence_manifest)
  const steps = object(manifest.effective_steps)
  const admitted = array(steps.admitted_step_ids)
  const excluded = array(steps.excluded_step_ids)
  const invalid = array(steps.invalid_step_ids)
  const artifacts = array(manifest.artifacts)
  const events = array(manifest.canonical_events)
  const metrics = object(manifest.metrics)
  const summary = object(metrics.summary)
  const findings: EvidenceFinding[] = []
  const manifestId = String(manifest.manifest_id || "") || undefined
  const manifestDigest = String(manifest.manifest_digest || "") || undefined
  if (!manifestId) {
    findings.push(
      finding(
        "manifest_identity_missing",
        "Evidence manifest identity is missing.",
        "evidence_manifest.manifest_id",
      ),
    )
  }
  if (!manifestDigest || !SHA256.test(manifestDigest)) {
    findings.push(
      finding(
        "manifest_digest_invalid",
        "Evidence manifest SHA-256 digest is invalid.",
        "evidence_manifest.manifest_digest",
      ),
    )
  }
  if (run.verification_receipt?.valid !== true) {
    findings.push(
      finding(
        "verification_receipt_invalid",
        "Backend evidence verification is missing or invalid.",
        "verification_receipt",
      ),
    )
  }
  if (steps.formal_valid !== true) {
    findings.push(
      finding(
        "effective_step_batch_invalid",
        "Effective-step batch did not pass formal validation.",
        "evidence_manifest.effective_steps",
      ),
    )
  }
  if (!admitted.length) {
    findings.push(
      finding(
        "effective_steps_missing",
        "Evidence contains no admitted semantic transition.",
        "evidence_manifest.effective_steps.admitted_step_ids",
      ),
    )
  }
  if (invalid.length) {
    findings.push(
      finding(
        "invalid_steps_present",
        "Evidence contains invalid effective-step candidates.",
        "evidence_manifest.effective_steps.invalid_step_ids",
      ),
    )
  }
  if (!artifacts.length) {
    findings.push(
      finding(
        "artifact_evidence_missing",
        "Evidence contains no owner artifact.",
        "evidence_manifest.artifacts",
      ),
    )
  }
  for (const [index, raw] of artifacts.entries()) {
    const artifact = object(raw)
    if (!artifact.artifact_id || !SHA256.test(String(artifact.sha256 || ""))) {
      findings.push(
        finding(
          "artifact_receipt_invalid",
          "Artifact receipt lacks identity or checksum.",
          `evidence_manifest.artifacts[${index}]`,
        ),
      )
    }
    if (!Number.isSafeInteger(artifact.size) || artifact.size < 0) {
      findings.push(
        finding(
          "artifact_size_invalid",
          "Artifact receipt byte size is invalid.",
          `evidence_manifest.artifacts[${index}].size`,
        ),
      )
    }
  }
  let previous = ""
  for (const [index, raw] of events.entries()) {
    const event = object(raw)
    if (event.sequence !== index + 1) {
      findings.push(
        finding(
          "event_sequence_invalid",
          "Canonical event evidence sequence is not contiguous.",
          `evidence_manifest.canonical_events[${index}].sequence`,
        ),
      )
    }
    if (String(event.previous_digest || "") !== previous) {
      findings.push(
        finding(
          "event_chain_broken",
          "Canonical event evidence chain is broken.",
          `evidence_manifest.canonical_events[${index}].previous_digest`,
        ),
      )
    }
    if (
      !event.event_id
      || !SHA256.test(String(event.event_digest || ""))
      || !SHA256.test(String(event.chain_digest || ""))
    ) {
      findings.push(
        finding(
          "event_receipt_invalid",
          "Canonical event receipt is incomplete.",
          `evidence_manifest.canonical_events[${index}]`,
        ),
      )
    }
    previous = String(event.chain_digest || "")
  }
  const claims = object(manifest.claims)
  if (Number(claims.human_intervention_count || 0) !== 0) {
    findings.push(
      finding(
        "human_intervention_claim_invalid",
        "Evidence claims non-zero human intervention.",
        "evidence_manifest.claims.human_intervention_count",
      ),
    )
  }
  if (claims.legacy_demo_fallback !== false) {
    findings.push(
      finding(
        "legacy_fallback_claim_invalid",
        "Formal evidence did not explicitly exclude legacy fallback.",
        "evidence_manifest.claims.legacy_demo_fallback",
      ),
    )
  }
  if (
    claims.long_live_scenario_complete !== false
    || claims.two_thousand_step_gate_complete !== false
    || claims.edge_cloud_dispatch_complete !== false
    || claims.m2_exit_complete !== false
  ) {
    findings.push(
      finding(
        "premature_completion_claim",
        "Foundation evidence claims a later M2-S05 gate.",
        "evidence_manifest.claims",
      ),
    )
  }
  const dimensions = object(summary)
  return Object.freeze({
    valid: findings.every((item) => item.severity !== "error"),
    manifestId,
    manifestDigest,
    effectiveStepCount: admitted.length,
    excludedStepCount: excluded.length,
    invalidStepCount: invalid.length,
    artifactCount: artifacts.length,
    canonicalEventCount: events.length,
    rawSampleCount: Number(dimensions.raw_sample_count || 0),
    effects: Object.freeze({ ...object(dimensions.effective_by_effect) }),
    stages: Object.freeze({ ...object(dimensions.effective_by_stage) }),
    providers: Object.freeze({ ...object(dimensions.effective_by_provider) }),
    findings: Object.freeze(findings),
  })
}

export function evidenceReceiptsByKind(
  status: Readonly<Record<string, any>>,
): ReadonlyMap<string, readonly Readonly<Record<string, any>>[]> {
  const grouped = new Map<string, Readonly<Record<string, any>>[]>()
  for (const raw of array(status.receipts)) {
    const receipt = object(raw)
    const kind = String(receipt.kind || "unknown")
    const values = grouped.get(kind) ?? []
    values.push(receipt)
    grouped.set(kind, values)
  }
  return new Map(
    [...grouped.entries()]
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([kind, values]) => [
        kind,
        Object.freeze(
          [...values].sort((left, right) =>
            String(left.created_at || "").localeCompare(
              String(right.created_at || ""),
            ),
          ),
        ),
      ]),
  )
}

export function effectCoverage(
  run: ScenarioRunProjection,
  expected: readonly string[],
): {
  complete: boolean
  missing: readonly string[]
  observed: readonly string[]
} {
  const assessment = assessEvidence(run)
  const observed = Object.keys(assessment.effects)
    .filter((effect) => Number(assessment.effects[effect]) > 0)
    .sort()
  const available = new Set(observed)
  const missing = [...new Set(expected)]
    .filter((effect) => !available.has(effect))
    .sort()
  return Object.freeze({
    complete: missing.length === 0,
    missing: Object.freeze(missing),
    observed: Object.freeze(observed),
  })
}
