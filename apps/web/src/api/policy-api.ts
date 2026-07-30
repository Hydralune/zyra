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
  if (
    !String(body.transition_id || "")
    || !String(body.event_id || "")
    || !String(body.receipt_id || "")
    || !Number.isSafeInteger(sequence)
    || sequence < 1
  ) {
    throw new TypeError("Policy evidence transition identity is incomplete.")
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
    contract_kind: String(body.contract_kind || ""),
    schema_version: String(body.schema_version || ""),
    contract_digest: String(body.contract_digest || ""),
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
    integrity: enumValue(body.integrity, INTEGRITY, "Policy integrity"),
    disposition: String(body.disposition || ""),
    constraints: records(body.constraints),
    graph_diff: records(body.graph_diff),
    causal_refs: Object.freeze(causalRefs),
    details: record(body.details ?? {}, "Policy evidence details"),
  })
}

export function admitPolicyEvidencePage(value: unknown): PolicyEvidencePage {
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
  const issues = records(body.issues).map((item) => Object.freeze({
    code: String(item.code || ""),
    message: String(item.message || ""),
    integrity: enumValue(item.integrity, INTEGRITY, "Policy issue integrity"),
    event_id: String(item.event_id || ""),
  }))
  const labels = record(body.labels, "Policy evidence labels")
  return Object.freeze({
    schema_version: "zyra.policy-evidence-projection/v1",
    projection_owner: "canonical_event_artifact_metric_read_model",
    canonical_write_allowed: false,
    status: String(body.status) as "ready" | "degraded",
    filters: record(body.filters, "Policy evidence filters") as Readonly<Record<string, string>>,
    filter_digest: String(body.filter_digest || ""),
    cursor: String(body.cursor || ""),
    next_cursor: String(body.next_cursor || ""),
    high_watermark: highWatermark,
    has_more: body.has_more === true,
    scanned,
    transition_count: transitionCount,
    transitions,
    metric_report: record(body.metric_report ?? {}, "Policy metric projection"),
    issues: Object.freeze(issues),
    labels: Object.freeze(
      Object.fromEntries(
        Object.entries(labels).map(([key, item]) => [key, strings(item)]),
      ),
    ),
    snapshot_digest: String(body.snapshot_digest || ""),
    evidence_digest: String(body.evidence_digest || ""),
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
    return admitPolicyEvidencePage(response.data)
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
    return Object.freeze({ ...record(response.data, "Policy metric report") })
  }
}
