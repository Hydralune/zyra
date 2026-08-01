import {
  OPERATION_NAMES,
} from "../../../../packages/core/typed-api-client/src/index.ts"
import type { ZyraApiClient } from "./client.ts"

export type PolicyLifecycle =
  | "validation"
  | "diagnostic"
  | "default"
  | "baseline"
  | "retired"

export type PolicyReadiness =
  | "deterministic_ready"
  | "evidence_only"
  | "unavailable"

export type PolicyExecution =
  | "real"
  | "simulated"
  | "degraded"
  | "not_applicable"

export type PolicyIntegrity =
  | "verified"
  | "pending"
  | "missing"
  | "stale"
  | "inconsistent"

export interface PolicyCausalReference {
  readonly kind: string
  readonly id: string
  readonly route: string
}

export interface PolicyEvidenceTransition {
  readonly transition_id: string
  readonly sequence: number
  readonly event_id: string
  readonly event_type: string
  readonly occurred_at: string
  readonly run_id: string
  readonly task_id: string
  readonly receipt_id: string
  readonly contract_kind: string
  readonly schema_version: string
  readonly contract_digest: string
  readonly mechanism: {
    readonly id: string
    readonly version: string
    readonly lifecycle: PolicyLifecycle
    readonly readiness: PolicyReadiness
  }
  readonly execution: PolicyExecution
  readonly integrity: PolicyIntegrity
  readonly disposition: string
  readonly constraints: readonly Readonly<Record<string, unknown>>[]
  readonly graph_diff: readonly Readonly<Record<string, unknown>>[]
  readonly causal_refs: readonly PolicyCausalReference[]
  readonly details: Readonly<Record<string, unknown>>
}

export interface PolicyEvidenceIssue {
  readonly code: string
  readonly message: string
  readonly integrity: PolicyIntegrity
  readonly event_id: string
}

export interface PolicyEvidencePage {
  readonly schema_version: "zyra.policy-evidence-projection/v1"
  readonly projection_owner: "canonical_event_artifact_metric_read_model"
  readonly canonical_write_allowed: false
  readonly status: "ready" | "degraded"
  readonly filters: Readonly<Record<string, string>>
  readonly filter_digest: string
  readonly cursor: string
  readonly next_cursor: string
  readonly high_watermark: number
  readonly has_more: boolean
  readonly scanned: number
  readonly transition_count: number
  readonly transitions: readonly PolicyEvidenceTransition[]
  readonly metric_report: Readonly<Record<string, unknown>>
  readonly issues: readonly PolicyEvidenceIssue[]
  readonly labels: Readonly<Record<string, readonly string[]>>
  readonly snapshot_digest: string
  readonly evidence_digest_payload: string
  readonly evidence_digest: string
}

export interface PolicyEvidenceQuery {
  readonly runId?: string
  readonly taskId?: string
  readonly mechanismVersion?: string
  readonly receiptId?: string
  readonly reportId?: string
  readonly cursor?: string
  readonly limit?: number
  readonly signal?: AbortSignal
}

const LIFECYCLES = new Set<PolicyLifecycle>([
  "validation",
  "diagnostic",
  "default",
  "baseline",
  "retired",
])
const READINESS = new Set<PolicyReadiness>([
  "deterministic_ready",
  "evidence_only",
  "unavailable",
])
const EXECUTIONS = new Set<PolicyExecution>([
  "real",
  "simulated",
  "degraded",
  "not_applicable",
])
const INTEGRITY = new Set<PolicyIntegrity>([
  "verified",
  "pending",
  "missing",
  "stale",
  "inconsistent",
])
const VERIFIED_CONTRACT_SCHEMAS = new Map<string, string>([
  ["topology_proposal_artifact", "zyra.topology-proposal-artifact/v1"],
  ["policy_decision_receipt", "zyra.policy-decision-receipt/v1"],
  ["policy_outcome", "zyra.policy-outcome/v1"],
  ["memory_continuity_receipt", "zyra.memory-continuity-receipt/v1"],
  ["neuro_symbolic_evidence_bundle", "zyra.neuro-symbolic-evidence-bundle/v1"],
  ["physical_dispatch_receipt", "zyra.physical-dispatch-receipt/v2"],
])

function digest(value: unknown, label: string): string {
  const selected = String(value || "").toLowerCase()
  if (!/^[0-9a-f]{64}$/.test(selected)) {
    throw new TypeError(`${label} must be a SHA-256 digest.`)
  }
  return selected
}

function canonicalValue(value: unknown): unknown {
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return value
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError("Policy digest rejects non-finite numbers.")
    return Object.is(value, -0) ? 0 : value
  }
  if (Array.isArray(value)) return value.map(canonicalValue)
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .filter(([, item]) => item !== undefined)
        .map(([key, item]) => [key, canonicalValue(item)]),
    )
  }
  if (value === undefined) return undefined
  throw new TypeError(`Policy digest cannot encode ${typeof value}.`)
}

function canonicalJson(value: unknown): string {
  const encoded = JSON.stringify(canonicalValue(value))
  if (encoded === undefined) throw new TypeError("Policy digest payload is undefined.")
  return encoded
}

async function sha256Text(value: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle
  if (!subtle) throw new TypeError("Policy SHA-256 verifier is unavailable.")
  const observed = await subtle.digest("SHA-256", new TextEncoder().encode(value))
  return [...new Uint8Array(observed)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("")
}

async function verifyDigestPayload(
  payload: unknown,
  suppliedDigest: unknown,
  expectedValue: unknown,
  label: string,
): Promise<void> {
  if (typeof payload !== "string" || !payload) {
    throw new TypeError(`${label} canonical payload is missing.`)
  }
  const expectedDigest = digest(suppliedDigest, `${label} digest`)
  if (await sha256Text(payload) !== expectedDigest) {
    throw new TypeError(`${label} canonical payload digest is inconsistent.`)
  }
  let decoded: unknown
  try {
    decoded = JSON.parse(payload)
  } catch {
    throw new TypeError(`${label} canonical payload is invalid JSON.`)
  }
  if (canonicalJson(decoded) !== canonicalJson(expectedValue)) {
    throw new TypeError(`${label} canonical payload differs from the projection.`)
  }
}

function record(value: unknown, label: string): Readonly<Record<string, unknown>> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object.`)
  }
  return value as Readonly<Record<string, unknown>>
}

function strings(value: unknown): readonly string[] {
  return Array.isArray(value) ? Object.freeze(value.map(String)) : Object.freeze([])
}

function records(value: unknown): readonly Readonly<Record<string, unknown>>[] {
  return Array.isArray(value)
    ? Object.freeze(value.map((item) => record(item, "Policy evidence item")))
    : Object.freeze([])
}

function enumValue<T extends string>(
  value: unknown,
  allowed: ReadonlySet<T>,
  label: string,
): T {
  const selected = String(value || "") as T
  if (!allowed.has(selected)) throw new TypeError(`${label} is unsupported.`)
  return selected
}

function transition(value: unknown): PolicyEvidenceTransition {
  const body = record(value, "Policy evidence transition")
  const mechanism = record(body.mechanism, "Policy mechanism")
  const causalRefs = records(body.causal_refs).map((item) => Object.freeze({
    kind: String(item.kind || ""),
    id: String(item.id || ""),
    route: String(item.route || ""),
  }))
  const sequence = Number(body.sequence)
  const integrity = enumValue(body.integrity, INTEGRITY, "Policy integrity")
  const contractKind = String(body.contract_kind || "")
  const schemaVersion = String(body.schema_version || "")
  const contractDigest = String(body.contract_digest || "")
  if (
    !String(body.transition_id || "")
    || !String(body.event_id || "")
    || !String(body.receipt_id || "")
    || !Number.isSafeInteger(sequence)
    || sequence < 1
  ) {
    throw new TypeError("Policy evidence transition identity is incomplete.")
  }
  if (integrity === "verified") {
    if (
      !String(body.run_id || "")
      || !String(body.task_id || "")
      || VERIFIED_CONTRACT_SCHEMAS.get(contractKind) !== schemaVersion
    ) {
      throw new TypeError("Verified policy evidence contract identity is invalid.")
    }
    digest(contractDigest, "Verified policy contract digest")
    if (causalRefs.some((item) => !item.kind || !item.id || !item.route)) {
      throw new TypeError("Verified policy causal references are incomplete.")
    }
  }
  return Object.freeze({
    transition_id: String(body.transition_id),
    sequence,
    event_id: String(body.event_id),
    event_type: String(body.event_type || ""),
    occurred_at: String(body.occurred_at || ""),
    run_id: String(body.run_id || ""),
    task_id: String(body.task_id || ""),
    receipt_id: String(body.receipt_id),
    contract_kind: contractKind,
    schema_version: schemaVersion,
    contract_digest: contractDigest,
    mechanism: Object.freeze({
      id: String(mechanism.id || ""),
      version: String(mechanism.version || ""),
      lifecycle: enumValue(
        mechanism.lifecycle,
        LIFECYCLES,
        "Policy lifecycle",
      ),
      readiness: enumValue(
        mechanism.readiness,
        READINESS,
        "Policy readiness",
      ),
    }),
    execution: enumValue(body.execution, EXECUTIONS, "Policy execution"),
    integrity,
    disposition: String(body.disposition || ""),
    constraints: records(body.constraints),
    graph_diff: records(body.graph_diff),
    causal_refs: Object.freeze(causalRefs),
    details: record(body.details ?? {}, "Policy evidence details"),
  })
}

export async function admitPolicyEvidencePage(value: unknown): Promise<PolicyEvidencePage> {
  const body = record(value, "Policy evidence response")
  if (
    body.schema_version !== "zyra.policy-evidence-projection/v1"
    || body.projection_owner !== "canonical_event_artifact_metric_read_model"
    || body.canonical_write_allowed !== false
    || !["ready", "degraded"].includes(String(body.status))
  ) {
    throw new TypeError("Policy evidence response violates v1 ownership.")
  }
  const highWatermark = Number(body.high_watermark)
  const scanned = Number(body.scanned)
  const transitionCount = Number(body.transition_count)
  if (
    !Number.isSafeInteger(highWatermark)
    || highWatermark < 0
    || !Number.isSafeInteger(scanned)
    || scanned < 0
    || !Number.isSafeInteger(transitionCount)
    || transitionCount < 0
  ) {
    throw new TypeError("Policy evidence counters are invalid.")
  }
  const transitions = Array.isArray(body.transitions)
    ? Object.freeze(body.transitions.map(transition))
    : Object.freeze([])
  if (transitions.length !== transitionCount) {
    throw new TypeError("Policy evidence transition count is inconsistent.")
  }
  if (
    transitions.some((item, index) => (
      item.sequence > highWatermark
      || (index > 0 && item.sequence <= transitions[index - 1].sequence)
    ))
  ) {
    throw new TypeError("Policy evidence transition sequence is inconsistent.")
  }
  const issues = records(body.issues).map((item) => Object.freeze({
    code: String(item.code || ""),
    message: String(item.message || ""),
    integrity: enumValue(item.integrity, INTEGRITY, "Policy issue integrity"),
    event_id: String(item.event_id || ""),
  }))
  const labels = record(body.labels, "Policy evidence labels")
  const filterDigest = digest(body.filter_digest, "Policy filter digest")
  const snapshotDigest = digest(body.snapshot_digest, "Policy snapshot digest")
  const evidenceDigest = digest(body.evidence_digest, "Policy evidence digest")
  const metricReport = record(body.metric_report ?? {}, "Policy metric projection")
  if (metricReport.status === "verified") {
    if (
      metricReport.schema_version !== "zyra.phase2-metric-report/v1"
      || !String(metricReport.report_id || "")
    ) {
      throw new TypeError("Verified policy metric report identity is invalid.")
    }
    digest(metricReport.digest, "Verified policy metric report digest")
    const projectedReport = { ...metricReport }
    delete projectedReport.report_id
    delete projectedReport.status
    delete projectedReport.digest
    delete projectedReport.digest_payload
    await verifyDigestPayload(
      metricReport.digest_payload,
      metricReport.digest,
      projectedReport,
      "Verified policy metric report",
    )
  }
  if (await sha256Text(canonicalJson(body.filters)) !== filterDigest) {
    throw new TypeError("Policy filter digest is inconsistent.")
  }
  if (
    await sha256Text(canonicalJson({
      filter_digest: filterDigest,
      high_watermark: highWatermark,
    })) !== snapshotDigest
  ) {
    throw new TypeError("Policy snapshot digest is inconsistent.")
  }
  const evidenceBody = { ...body }
  delete evidenceBody.evidence_digest
  delete evidenceBody.evidence_digest_payload
  await verifyDigestPayload(
    body.evidence_digest_payload,
    evidenceDigest,
    evidenceBody,
    "Policy evidence",
  )
  return Object.freeze({
    schema_version: "zyra.policy-evidence-projection/v1",
    projection_owner: "canonical_event_artifact_metric_read_model",
    canonical_write_allowed: false,
    status: String(body.status) as "ready" | "degraded",
    filters: record(body.filters, "Policy evidence filters") as Readonly<Record<string, string>>,
    filter_digest: filterDigest,
    cursor: String(body.cursor || ""),
    next_cursor: String(body.next_cursor || ""),
    high_watermark: highWatermark,
    has_more: body.has_more === true,
    scanned,
    transition_count: transitionCount,
    transitions,
    metric_report: metricReport,
    issues: Object.freeze(issues),
    labels: Object.freeze(
      Object.fromEntries(
        Object.entries(labels).map(([key, item]) => [key, strings(item)]),
      ),
    ),
    snapshot_digest: snapshotDigest,
    evidence_digest_payload: String(body.evidence_digest_payload),
    evidence_digest: evidenceDigest,
  })
}

function optionalToken(value: string | undefined, label: string): string | undefined {
  const selected = value?.trim()
  if (!selected) return undefined
  if (
    selected.length > 256
    || !/^[A-Za-z0-9][A-Za-z0-9._:/-]*$/.test(selected)
  ) {
    throw new TypeError(`${label} is invalid.`)
  }
  return selected
}

export class PolicyApi {
  readonly #client: ZyraApiClient

  constructor(client: ZyraApiClient) {
    this.#client = client
  }

  async evidence(query: PolicyEvidenceQuery = {}): Promise<PolicyEvidencePage> {
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.policyEvidence,
      {
        query: {
          run_id: optionalToken(query.runId, "Run id"),
          task_id: optionalToken(query.taskId, "Task id"),
          mechanism_version: optionalToken(
            query.mechanismVersion,
            "Mechanism version",
          ),
          receipt_id: optionalToken(query.receiptId, "Receipt id"),
          report_id: optionalToken(query.reportId, "Report id"),
          cursor: query.cursor,
          limit: query.limit,
        },
        binding: {
          runId: query.runId,
          taskId: query.taskId,
        },
        signal: query.signal,
        coordinationKey: [
          "policy.evidence",
          query.runId,
          query.taskId,
          query.mechanismVersion,
          query.receiptId,
          query.reportId,
          query.cursor,
        ].join(":"),
        latestWins: true,
      },
    )
    return await admitPolicyEvidencePage(response.data)
  }

  async metricSpecs(signal?: AbortSignal): Promise<Readonly<Record<string, unknown>>> {
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.policyMetricSpecs,
      { signal, coordinationKey: "policy.metrics.specs", deduplicate: true },
    )
    return Object.freeze({ ...record(response.data, "Policy metric registry") })
  }

  async metricReport(
    reportId: string,
    signal?: AbortSignal,
  ): Promise<Readonly<Record<string, unknown>>> {
    const selected = optionalToken(reportId, "Report id")
    if (!selected) throw new TypeError("Report id is required.")
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.policyMetricReport,
      {
        path: { report_id: selected },
        signal,
        coordinationKey: `policy.metrics.report:${selected}`,
        latestWins: true,
      },
    )
    const report = { ...record(response.data, "Policy metric report") }
    const projected = { ...report }
    delete projected.digest
    delete projected.digest_payload
    await verifyDigestPayload(
      report.digest_payload,
      report.digest,
      projected,
      "Policy metric report",
    )
    return Object.freeze(report)
  }
}
