export interface SourceAuditRow {
  source: string
  capability: string
  role: string
  status: "active" | "inactive"
  landing: readonly string[]
  reason: string
}
export interface SourceAuditProjection {
  valid: boolean
  openclaw: string
  rows: readonly SourceAuditRow[]
  active: readonly SourceAuditRow[]
  inactiveNotMissing: readonly SourceAuditRow[]
  findings: readonly Readonly<Record<string, unknown>>[]
}

const ACTIVE_ROLES = new Set([
  "primary_implementation",
  "supplementary_implementation",
  "zyra_owned_primary",
  "existing_owner_integration",
])
const INACTIVE_ROLES = new Set([
  "conformance_only",
  "reference_only",
  "role_aware_audit_only",
  "experimental",
  "deferred",
  "rejected",
  "excluded_forward_only",
])

function object(value: unknown): Readonly<Record<string, any>> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Readonly<Record<string, any>>
    : {}
}

function normalizeRow(value: unknown): SourceAuditRow {
  const raw = object(value)
  const status = raw.status === "active" ? "active" : "inactive"
  return Object.freeze({
    source: String(raw.source || "unknown"),
    capability: String(raw.capability || "unknown"),
    role: String(raw.role || "unknown"),
    status,
    landing: Object.freeze(
      Array.isArray(raw.landing)
        ? raw.landing.map((item: unknown) => String(item))
        : [],
    ),
    reason: String(raw.reason || ""),
  })
}

export function projectSourceAudit(
  value: unknown,
): SourceAuditProjection {
  const report = object(value)
  const rows = Object.freeze(
    (Array.isArray(report.rows) ? report.rows : [])
      .map(normalizeRow)
      .sort((left, right) =>
        left.source.localeCompare(right.source)
        || left.capability.localeCompare(right.capability)
      ),
  )
  const findings: Readonly<Record<string, unknown>>[] = [
    ...(Array.isArray(report.findings) ? report.findings : []),
  ]
  for (const row of rows) {
    if (ACTIVE_ROLES.has(row.role) && row.status !== "active") {
      findings.push(
        Object.freeze({
          code: "active_role_inactive",
          source: row.source,
          capability: row.capability,
        }),
      )
    }
    if (INACTIVE_ROLES.has(row.role) && row.status !== "inactive") {
      findings.push(
        Object.freeze({
          code: "inactive_role_active",
          source: row.source,
          capability: row.capability,
        }),
      )
    }
    if (
      row.source === "openclaw"
      && (
        row.role !== "excluded_forward_only"
        || row.status !== "inactive"
        || row.landing.length !== 0
      )
    ) {
      findings.push(
        Object.freeze({
          code: "openclaw_forward_exclusion_broken",
        }),
      )
    }
  }
  if (report.openclaw !== "excluded_forward_only") {
    findings.push(
      Object.freeze({
        code: "openclaw_report_status_invalid",
        observed: report.openclaw,
      }),
    )
  }
  return Object.freeze({
    valid: report.valid === true && findings.length === 0,
    openclaw: String(report.openclaw || ""),
    rows,
    active: Object.freeze(rows.filter((row) => row.status === "active")),
    inactiveNotMissing: Object.freeze(
      rows.filter((row) => row.status === "inactive"),
    ),
    findings: Object.freeze(findings),
  })
}

export function sourceLandingMatrix(
  projection: SourceAuditProjection,
): ReadonlyMap<string, readonly SourceAuditRow[]> {
  const byLanding = new Map<string, SourceAuditRow[]>()
  for (const row of projection.rows) {
    for (const landing of row.landing) {
      const values = byLanding.get(landing) ?? []
      values.push(row)
      byLanding.set(landing, values)
    }
  }
  return new Map(
    [...byLanding.entries()]
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([landing, rows]) => [
        landing,
        Object.freeze(
          [...rows].sort((left, right) =>
            left.source.localeCompare(right.source)
            || left.capability.localeCompare(right.capability)
          ),
        ),
      ]),
  )
}
