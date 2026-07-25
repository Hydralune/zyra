import type {
  ScenarioDefinitionProjection,
  ScenarioRegistryProjection,
  ScenarioRunProjection,
} from "../../api/scenario-api.ts"

export interface ScenarioAdmissionFinding {
  code: string
  severity: "error" | "warning" | "info"
  message: string
  field?: string
  detail: Readonly<Record<string, unknown>>
}

export interface ScenarioAdmissionAssessment {
  valid: boolean
  formal: boolean
  clean: boolean
  newInput: boolean
  sealed: boolean
  policyDigestMatches: boolean
  humanInterventionCount: number
  operatorAttemptCount: number
  findings: readonly ScenarioAdmissionFinding[]
}

const SHA256 = /^[a-f0-9]{64}$/
const REQUIRED_PREFLIGHT_KINDS = new Set([
  "database",
  "cache",
  "index",
  "artifact",
  "build",
])

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
  options: {
    severity?: ScenarioAdmissionFinding["severity"]
    field?: string
    detail?: Readonly<Record<string, unknown>>
  } = {},
): ScenarioAdmissionFinding {
  return Object.freeze({
    code,
    severity: options.severity ?? "error",
    message,
    field: options.field,
    detail: Object.freeze({ ...(options.detail ?? {}) }),
  })
}

export function assessScenarioAdmission(
  run: ScenarioRunProjection,
): ScenarioAdmissionAssessment {
  const configuration = object(run.configuration)
  const mode = String(configuration.mode || "")
  const formal = mode === "sealed"
  const sealed = formal && configuration.policy?.metadata?.sealed === true
  const preflight = object(run.preflight_receipt)
  const checks = array(preflight.checks)
  const kinds = new Set(checks.map((check) => String(object(check).kind || "")))
  const findings: ScenarioAdmissionFinding[] = []
  for (const kind of REQUIRED_PREFLIGHT_KINDS) {
    if (!kinds.has(kind)) {
      findings.push(
        finding(
          "preflight_kind_missing",
          `Formal preflight is missing ${kind}.`,
          { field: "preflight", detail: { kind } },
        ),
      )
    }
  }
  const dirty = checks.filter((check) => object(check).clean !== true)
  if (dirty.length) {
    findings.push(
      finding(
        "preflight_dirty",
        "One or more owner roots were dirty at admission.",
        {
          field: "preflight",
          detail: {
            kinds: dirty.map((check) => String(object(check).kind || "")),
          },
        },
      ),
    )
  }
  if (preflight.new_input !== true && formal) {
    findings.push(
      finding(
        "input_not_new",
        "Formal input was not proven new; replay is review-only.",
        { field: "input_digest" },
      ),
    )
  }
  const policy = object(configuration.policy)
  const expectedPolicyDigest = String(configuration.expected_policy_digest || "")
  const actualPolicyDigest = String(policy.policy_digest || "")
  const policyDigestMatches =
    SHA256.test(expectedPolicyDigest)
    && expectedPolicyDigest === actualPolicyDigest
  if (!policyDigestMatches) {
    findings.push(
      finding(
        "policy_digest_mismatch",
        "Registered and expected sealed policy digests differ.",
        {
          field: "policy_digest",
          detail: {
            expected: expectedPolicyDigest,
            actual: actualPolicyDigest,
          },
        },
      ),
    )
  }
  if (formal && !sealed) {
    findings.push(
      finding(
        "sealed_policy_missing",
        "Formal run does not carry the sealed policy marker.",
        { field: "mode" },
      ),
    )
  }
  if (
    formal
    && (
      policy.ask_disposition !== "deny_and_replan"
      || policy.unknown_disposition !== "deny_and_replan"
    )
  ) {
    findings.push(
      finding(
        "policy_not_fail_closed",
        "Formal ask and unknown actions must deny and replan.",
        { field: "policy" },
      ),
    )
  }
  const humanInterventionCount = Number(run.human_intervention_count || 0)
  const operatorAttemptCount = Number(
    run.operator_intervention_attempt_count || 0,
  )
  if (humanInterventionCount !== 0) {
    findings.push(
      finding(
        "human_intervention_present",
        "Formal evidence cannot include human intervention.",
        {
          field: "human_intervention_count",
          detail: { count: humanInterventionCount },
        },
      ),
    )
  }
  if (formal && operatorAttemptCount > 0) {
    findings.push(
      finding(
        "operator_attempt_present",
        "An operator mutation attempt invalidated the sealed run.",
        {
          field: "operator_intervention_attempt_count",
          detail: { count: operatorAttemptCount },
        },
      ),
    )
  }
  if (!SHA256.test(String(configuration.definition_digest || ""))) {
    findings.push(
      finding(
        "definition_digest_invalid",
        "Scenario definition digest is missing or invalid.",
        { field: "definition_digest" },
      ),
    )
  }
  if (!SHA256.test(String(configuration.input_digest || ""))) {
    findings.push(
      finding(
        "input_digest_invalid",
        "Scenario input digest is missing or invalid.",
        { field: "input_digest" },
      ),
    )
  }
  const phasesWithAdmission = new Set([
    "admitted",
    "queued",
    "running",
    "cancelling",
    "cancelled",
    "succeeded",
    "failed",
    "archived",
  ])
  if (phasesWithAdmission.has(run.phase) && !run.preflight_receipt) {
    findings.push(
      finding(
        "preflight_receipt_missing",
        "Scenario advanced without a durable preflight receipt.",
        { field: "preflight_receipt" },
      ),
    )
  }
  return Object.freeze({
    valid: findings.every((item) => item.severity !== "error"),
    formal,
    clean: dirty.length === 0 && checks.length >= REQUIRED_PREFLIGHT_KINDS.size,
    newInput: preflight.new_input === true,
    sealed,
    policyDigestMatches,
    humanInterventionCount,
    operatorAttemptCount,
    findings: Object.freeze(findings),
  })
}

export function validateScenarioRegistry(
  registry: ScenarioRegistryProjection,
): readonly ScenarioAdmissionFinding[] {
  const findings: ScenarioAdmissionFinding[] = []
  if (!SHA256.test(String(registry.registry_digest || ""))) {
    findings.push(
      finding(
        "registry_digest_invalid",
        "Scenario registry digest is missing or invalid.",
        { field: "registry_digest" },
      ),
    )
  }
  if (!Number.isSafeInteger(registry.revision) || registry.revision < 1) {
    findings.push(
      finding(
        "registry_revision_invalid",
        "Scenario registry revision must be positive.",
        { field: "revision" },
      ),
    )
  }
  const identities = new Set<string>()
  for (const definition of registry.definitions) {
    const key = `${definition.scenario_id}@${definition.version}`
    if (identities.has(key)) {
      findings.push(
        finding(
          "definition_duplicate",
          "Scenario registry contains a duplicate id and version.",
          { detail: { key } },
        ),
      )
    }
    identities.add(key)
    findings.push(...validateDefinition(definition))
  }
  if (!registry.definitions.length) {
    findings.push(
      finding(
        "registry_empty",
        "Scenario registry has no executable definitions.",
      ),
    )
  }
  return Object.freeze(findings)
}

export function validateDefinition(
  definition: ScenarioDefinitionProjection,
): readonly ScenarioAdmissionFinding[] {
  const findings: ScenarioAdmissionFinding[] = []
  const key = `${definition.scenario_id}@${definition.version}`
  if (!definition.scenario_id || !definition.version) {
    findings.push(
      finding(
        "definition_identity_missing",
        "Scenario definition identity is incomplete.",
        { detail: { key } },
      ),
    )
  }
  if (!SHA256.test(String(definition.definition_digest || ""))) {
    findings.push(
      finding(
        "definition_digest_invalid",
        "Scenario definition digest is invalid.",
        { detail: { key } },
      ),
    )
  }
  if (!definition.required_owner_stages.length) {
    findings.push(
      finding(
        "definition_owner_stages_missing",
        "Scenario definition has no canonical owner stages.",
        { detail: { key } },
      ),
    )
  }
  if (!definition.expected_effects.length) {
    findings.push(
      finding(
        "definition_effects_missing",
        "Scenario definition has no semantic effect expectations.",
        { detail: { key } },
      ),
    )
  }
  if (
    !Number.isSafeInteger(definition.minimum_effective_steps)
    || definition.minimum_effective_steps < 1
  ) {
    findings.push(
      finding(
        "definition_minimum_invalid",
        "Scenario effective-step minimum is invalid.",
        { detail: { key } },
      ),
    )
  }
  return findings
}

export function canStartScenario(run: ScenarioRunProjection): {
  allowed: boolean
  reason: string
} {
  const admission = assessScenarioAdmission(run)
  if (!admission.valid) {
    return {
      allowed: false,
      reason:
        admission.findings.find((item) => item.severity === "error")?.message
        ?? "Scenario admission is invalid.",
    }
  }
  if (!["admitted", "failed"].includes(run.phase)) {
    return {
      allowed: false,
      reason: `Scenario cannot start from ${run.phase}.`,
    }
  }
  if (run.phase === "failed" && run.failure?.retryable !== true) {
    return {
      allowed: false,
      reason: "Scenario failure is not retryable.",
    }
  }
  return { allowed: true, reason: "Scenario is admitted." }
}

export function formalActionPolicy(run: ScenarioRunProjection): {
  mayStart: boolean
  mayCancel: boolean
  mayArchive: boolean
  mayVerify: boolean
  warning?: string
} {
  const admission = assessScenarioAdmission(run)
  return Object.freeze({
    mayStart: canStartScenario(run).allowed,
    mayCancel: !run.terminal && !admission.formal,
    mayArchive: run.terminal && run.phase !== "archived",
    mayVerify:
      run.phase === "succeeded"
      && Boolean(run.evidence_manifest)
      && admission.humanInterventionCount === 0,
    warning:
      admission.formal && !run.terminal
        ? "Sealed runs reject operator cancellation or steering."
        : undefined,
  })
}

