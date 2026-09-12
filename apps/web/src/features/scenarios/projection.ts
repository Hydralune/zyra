import type {
  ScenarioDefinitionProjection,
  ScenarioRegistryProjection,
  ScenarioRunProjection,
  ScenarioStatusProjection,
} from "../../api/scenario-api.ts"
import {
  assessScenarioAdmission,
  type ScenarioAdmissionAssessment,
} from "./admission.ts"
import {
  assessEvidence,
  type EvidenceAssessment,
} from "./evidence.ts"
import {
  projectSourceAudit,
  type SourceAuditProjection,
} from "./source-audit.ts"

export type ScenarioConnection =
  | "idle"
  | "loading"
  | "online"
  | "reconnecting"
  | "offline"
  | "closed"

export interface ScenarioRunRow {
  run: ScenarioRunProjection
  admission: ScenarioAdmissionAssessment
  evidence: EvidenceAssessment
  active: boolean
  selected: boolean
  elapsedMs: number
  transitionCount: number
  receiptCount: number
  sourceAudit?: SourceAuditProjection
}

export interface ScenarioProjection {
  revision: number
  connection: ScenarioConnection
  connectionReason?: string
  registryRevision: number
  registryDigest?: string
  definitions: readonly ScenarioDefinitionProjection[]
  rows: readonly ScenarioRunRow[]
  selectedRunId?: string
  selected?: ScenarioRunRow
  activeCount: number
  succeededCount: number
  failedCount: number
  archivedCount: number
  formalValidCount: number
  evidenceValidCount: number
  lastRefreshAt?: number
  nextPollAt?: number
  staleResponses: number
  conflictingResponses: number
}

const ACTIVE_PHASES = new Set([
  "created",
  "admitted",
  "queued",
  "running",
  "cancelling",
])

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function timestamp(value: string | undefined): number {
  const selected = Date.parse(String(value || ""))
  return Number.isFinite(selected) ? selected : 0
}

function elapsed(run: ScenarioRunProjection, now: number): number {
  const start = timestamp(run.started_at || run.created_at)
  if (!start) return 0
  const end = timestamp(run.completed_at) || now
  return Math.max(0, end - start)
}

export class ScenarioProjectionStore {
  #registry?: ScenarioRegistryProjection
  #statuses = new Map<string, ScenarioStatusProjection>()
  #runs = new Map<string, ScenarioRunProjection>()
  #selectedRunId?: string
  #connection: ScenarioConnection = "idle"
  #connectionReason?: string
  #revision = 0
  #lastRefreshAt?: number
  #nextPollAt?: number
  #staleResponses = 0
  #conflictingResponses = 0
  #snapshot?: ScenarioProjection
  readonly #now: () => number

  constructor(options: { now?: () => number } = {}) {
    this.#now = options.now ?? Date.now
  }

  getSnapshot = (): ScenarioProjection => {
    if (this.#snapshot) return this.#snapshot
    const now = this.#now()
    const rows = [...this.#runs.values()]
      .sort((left, right) =>
        timestamp(right.created_at) - timestamp(left.created_at)
        || right.scenario_run_id.localeCompare(left.scenario_run_id)
      )
      .map((run) => this.#row(run, now))
    const selected = rows.find(
      (row) => row.run.scenario_run_id === this.#selectedRunId,
    )
    this.#snapshot = Object.freeze({
      revision: this.#revision,
      connection: this.#connection,
      connectionReason: this.#connectionReason,
      registryRevision: this.#registry?.revision ?? 0,
      registryDigest: this.#registry?.registry_digest,
      definitions: freeze(this.#registry?.definitions ?? []),
      rows: freeze(rows),
      selectedRunId: this.#selectedRunId,
      selected,
      activeCount: rows.filter((row) => row.active).length,
      succeededCount: rows.filter((row) => row.run.phase === "succeeded").length,
      failedCount: rows.filter((row) => row.run.phase === "failed").length,
      archivedCount: rows.filter((row) => row.run.phase === "archived").length,
      formalValidCount: rows.filter((row) => row.admission.valid).length,
      evidenceValidCount: rows.filter((row) => row.evidence.valid).length,
      lastRefreshAt: this.#lastRefreshAt,
      nextPollAt: this.#nextPollAt,
      staleResponses: this.#staleResponses,
      conflictingResponses: this.#conflictingResponses,
    })
    return this.#snapshot
  }

  registry(value: ScenarioRegistryProjection): boolean {
    if (this.#registry && value.revision < this.#registry.revision) {
      this.#staleResponses += 1
      this.#changed()
      return false
    }
    if (
      this.#registry
      && value.revision === this.#registry.revision
      && value.registry_digest !== this.#registry.registry_digest
    ) {
      this.#conflictingResponses += 1
      this.#changed()
      return false
    }
    if (
      this.#registry?.revision === value.revision
      && this.#registry.registry_digest === value.registry_digest
    ) {
      return false
    }
    this.#registry = value
    this.#changed()
    return true
  }

  replaceRuns(values: readonly ScenarioRunProjection[]): boolean {
    let changed = false
    const incoming = new Set(values.map((run) => run.scenario_run_id))
    for (const run of values) {
      changed = this.observeRun(run) || changed
    }
    for (const [runId, existing] of this.#runs) {
      if (incoming.has(runId) || existing.phase === "archived") continue
      if (!ACTIVE_PHASES.has(existing.phase)) continue
      // An active run omitted by a bounded page is not deletion evidence.
      // Keep it until an explicit status read or an archived-inclusive page.
    }
    return changed
  }

  observeStatus(value: ScenarioStatusProjection): boolean {
    const runId = value.run.scenario_run_id
    const changed = this.observeRun(value.run)
    const existing = this.#statuses.get(runId)
    if (
      existing
      && existing.run.revision === value.run.revision
      && existing.status_digest === value.status_digest
    ) {
      return changed
    }
    if (
      existing
      && existing.run.revision === value.run.revision
      && existing.status_digest !== value.status_digest
    ) {
      this.#conflictingResponses += 1
      this.#changed()
      return false
    }
    this.#statuses.set(runId, value)
    this.#changed()
    return true
  }

  observeRun(value: ScenarioRunProjection): boolean {
    const runId = value.scenario_run_id
    const existing = this.#runs.get(runId)
    if (existing && value.revision < existing.revision) {
      this.#staleResponses += 1
      this.#changed()
      return false
    }
    if (existing && value.revision === existing.revision) {
      if (
        existing.phase !== value.phase
        || existing.updated_at !== value.updated_at
        || existing.task_id !== value.task_id
      ) {
        this.#conflictingResponses += 1
        this.#changed()
      }
      return false
    }
    this.#runs.set(runId, value)
    this.#changed()
    return true
  }

  select(runId?: string): void {
    const selected = runId?.trim() || undefined
    if (this.#selectedRunId === selected) return
    this.#selectedRunId = selected
    this.#changed()
  }

  connection(
    value: ScenarioConnection,
    reason = "",
  ): void {
    const normalizedReason = reason.trim() || undefined
    if (
      this.#connection === value
      && this.#connectionReason === normalizedReason
    ) {
      return
    }
    this.#connection = value
    this.#connectionReason = normalizedReason
    this.#changed()
  }

  refreshed(nextPollAt?: number): void {
    this.#lastRefreshAt = this.#now()
    this.#nextPollAt = nextPollAt
    this.connection("online")
    this.#changed()
  }

  schedule(nextPollAt?: number): void {
    if (this.#nextPollAt === nextPollAt) return
    this.#nextPollAt = nextPollAt
    this.#changed()
  }

  close(reason: string): void {
    this.#nextPollAt = undefined
    this.connection("closed", reason)
  }

  status(runId: string): ScenarioStatusProjection | undefined {
    return this.#statuses.get(runId)
  }

  run(runId: string): ScenarioRunProjection | undefined {
    return this.#runs.get(runId)
  }

  #row(run: ScenarioRunProjection, now: number): ScenarioRunRow {
    const status = this.#statuses.get(run.scenario_run_id)
    // The list endpoint returns a trimmed summary, while the status endpoint
    // for the selected run returns the full record.  Admission and evidence
    // assessment read fields (policy, preflight receipt, evidence manifest)
    // that the summary deliberately omits, so they are computed from the
    // richest object available and left unevaluated until the detail record
    // arrives -- assessing a summary would only produce false findings.
    const detail = status?.run
    const effective = detail && detail.revision >= run.revision ? detail : run
    const enriched = isFullRecord(effective)
    const sourceAuditRaw = effective.evidence_manifest?.source_audit
    return Object.freeze({
      run: effective,
      admission: enriched
        ? assessScenarioAdmission(effective)
        : PENDING_ADMISSION,
      evidence: enriched ? assessEvidence(effective) : PENDING_EVIDENCE,
      active: ACTIVE_PHASES.has(effective.phase),
      selected: effective.scenario_run_id === this.#selectedRunId,
      elapsedMs: elapsed(effective, now),
      transitionCount: status?.transitions.length ?? 0,
      receiptCount: status?.receipts.length ?? 0,
      sourceAudit: sourceAuditRaw
        ? projectSourceAudit(sourceAuditRaw)
        : undefined,
    })
  }

  #changed(): void {
    this.#revision += 1
    this.#snapshot = undefined
  }
}

/**
 * Whether a run carries the fields the admission and evidence assessments read.
 *
 * The list endpoint returns a summary projection that omits the policy block,
 * the preflight receipt and the evidence manifest; only ``/scenarios/runs/{id}``
 * returns the full record.  `to_dict(projection="summary")` always emits a null
 * manifest and no policy, so their presence is a reliable marker.
 */
function isFullRecord(run: ScenarioRunProjection): boolean {
  const configuration = run.configuration as Record<string, unknown>
  if (configuration.policy === undefined && configuration.profile === undefined) {
    return false
  }
  return run.preflight_receipt !== undefined
    && run.evidence_manifest !== null
    && run.evidence_manifest !== undefined
}

/**
 * Placeholder verdicts for a list row whose detail record has not loaded.
 *
 * Reporting these as invalid would be wrong -- the list simply does not carry
 * the inputs -- so they are marked pending and the views read them only for the
 * selected run, which always has its detail record.
 */
const PENDING_ADMISSION: ScenarioAdmissionAssessment = Object.freeze({
  valid: false,
  formal: false,
  clean: false,
  newInput: false,
  sealed: false,
  policyDigestMatches: false,
  humanInterventionCount: 0,
  operatorAttemptCount: 0,
  findings: Object.freeze([]),
})

const PENDING_EVIDENCE: EvidenceAssessment = Object.freeze({
  valid: false,
  effectiveStepCount: 0,
  excludedStepCount: 0,
  invalidStepCount: 0,
  artifactCount: 0,
  canonicalEventCount: 0,
  rawSampleCount: 0,
  effects: Object.freeze({}),
  stages: Object.freeze({}),
  providers: Object.freeze({}),
  findings: Object.freeze([]),
})
