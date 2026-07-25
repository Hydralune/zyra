import type {
  ExperimentRegistryProjection,
  ExperimentRunProjection,
  ExperimentSamplePageProjection,
  ExperimentStatusProjection,
} from "../../api/experiment-api.ts"
import { assessBundle, reviewerGraph, type BundleAssessment } from "./evidence.ts"
import {
  assessRegistry,
  assessReport,
  assessRun,
  assessStatus,
  type ExperimentAdmission,
} from "./admission.ts"
import {
  comparisonRows,
  metricRows,
  reportHeadline,
  requirementRows,
} from "./report.ts"

export type ExperimentConnection =
  | "idle"
  | "loading"
  | "online"
  | "reconnecting"
  | "offline"
  | "closed"

export interface ExperimentRow {
  run: ExperimentRunProjection
  admission: ExperimentAdmission
  selected: boolean
  active: boolean
  progressRatio: number
}

export interface ExperimentProjection {
  revision: number
  connection: ExperimentConnection
  connectionReason?: string
  registry?: ExperimentRegistryProjection
  registryAdmission?: ExperimentAdmission
  rows: readonly ExperimentRow[]
  selectedRunId?: string
  selected?: ExperimentRow
  selectedStatus?: ExperimentStatusProjection
  report?: Readonly<Record<string, any>>
  reportAdmission?: ExperimentAdmission
  bundle?: Readonly<Record<string, any>>
  bundleAssessment?: BundleAssessment
  requirements?: Readonly<Record<string, any>>
  samples?: ExperimentSamplePageProjection
  metrics: ReturnType<typeof metricRows>
  comparisons: ReturnType<typeof comparisonRows>
  requirementRows: ReturnType<typeof requirementRows>
  headline: ReturnType<typeof reportHeadline>
  reviewerGraph: ReturnType<typeof reviewerGraph>
  activeCount: number
  succeededCount: number
  failedCount: number
  lastRefreshAt?: number
  nextPollAt?: number
}

function timestamp(value: string): number {
  const selected = Date.parse(String(value || ""))
  return Number.isFinite(selected) ? selected : 0
}

export class ExperimentProjectionStore {
  #registry?: ExperimentRegistryProjection
  #runs = new Map<string, ExperimentRunProjection>()
  #statuses = new Map<string, ExperimentStatusProjection>()
  #selectedRunId?: string
  #report?: Readonly<Record<string, any>>
  #bundle?: Readonly<Record<string, any>>
  #requirements?: Readonly<Record<string, any>>
  #samples?: ExperimentSamplePageProjection
  #connection: ExperimentConnection = "idle"
  #connectionReason?: string
  #revision = 0
  #lastRefreshAt?: number
  #nextPollAt?: number
  #snapshot?: ExperimentProjection

  getSnapshot = (): ExperimentProjection => {
    if (this.#snapshot) return this.#snapshot
    const rows = [...this.#runs.values()]
      .sort(
        (left, right) =>
          timestamp(right.created_at) - timestamp(left.created_at)
          || right.experiment_id.localeCompare(left.experiment_id),
      )
      .map((run) => {
        const status = this.#statuses.get(run.experiment_id)
        const progress = run.progress
        const admission = status ? assessStatus(status) : assessRun(run)
        return Object.freeze({
          run,
          admission,
          selected: run.experiment_id === this.#selectedRunId,
          active: !run.terminal,
          progressRatio: progress.planned
            ? progress.terminal / progress.planned
            : 0,
        })
      })
    const selected = rows.find((row) => row.selected)
    const reportAdmission = this.#report ? assessReport(this.#report) : undefined
    this.#snapshot = Object.freeze({
      revision: this.#revision,
      connection: this.#connection,
      connectionReason: this.#connectionReason,
      registry: this.#registry,
      registryAdmission: this.#registry
        ? assessRegistry(this.#registry)
        : undefined,
      rows: Object.freeze(rows),
      selectedRunId: this.#selectedRunId,
      selected,
      selectedStatus: this.#selectedRunId
        ? this.#statuses.get(this.#selectedRunId)
        : undefined,
      report: this.#report,
      reportAdmission,
      bundle: this.#bundle,
      bundleAssessment: this.#bundle
        ? assessBundle(this.#bundle)
        : undefined,
      requirements: this.#requirements,
      samples: this.#samples,
      metrics: metricRows(this.#report),
      comparisons: comparisonRows(this.#report),
      requirementRows: requirementRows(this.#report, this.#registry),
      headline: reportHeadline(this.#report),
      reviewerGraph: reviewerGraph(this.#report),
      activeCount: rows.filter((row) => row.active).length,
      succeededCount: rows.filter((row) => row.run.phase === "succeeded").length,
      failedCount: rows.filter((row) => row.run.phase === "failed").length,
      lastRefreshAt: this.#lastRefreshAt,
      nextPollAt: this.#nextPollAt,
    })
    return this.#snapshot
  }

  registry(value: ExperimentRegistryProjection): void {
    this.#registry = structuredClone(value)
    this.#changed()
  }

  replaceRuns(values: readonly ExperimentRunProjection[]): void {
    this.#runs.clear()
    for (const value of values) this.#runs.set(value.experiment_id, structuredClone(value))
    if (this.#selectedRunId && !this.#runs.has(this.#selectedRunId)) {
      this.#selectedRunId = undefined
      this.#clearResources()
    }
    this.#changed()
  }

  observeStatus(value: ExperimentStatusProjection): void {
    const current = this.#runs.get(value.run.experiment_id)
    if (!current || value.run.revision >= current.revision) {
      this.#runs.set(value.run.experiment_id, structuredClone(value.run))
      this.#statuses.set(value.run.experiment_id, structuredClone(value))
      this.#changed()
    }
  }

  observeRun(value: ExperimentRunProjection): void {
    const current = this.#runs.get(value.experiment_id)
    if (!current || value.revision >= current.revision) {
      this.#runs.set(value.experiment_id, structuredClone(value))
      this.#changed()
    }
  }

  select(experimentId?: string): void {
    if (experimentId && !this.#runs.has(experimentId)) {
      throw new TypeError("Experiment must be loaded before selection.")
    }
    if (this.#selectedRunId === experimentId) return
    this.#selectedRunId = experimentId
    this.#clearResources()
    this.#changed()
  }

  report(value?: Readonly<Record<string, any>>): void {
    this.#report = value ? structuredClone(value) : undefined
    this.#changed()
  }

  bundle(value?: Readonly<Record<string, any>>): void {
    this.#bundle = value ? structuredClone(value) : undefined
    this.#changed()
  }

  requirements(value?: Readonly<Record<string, any>>): void {
    this.#requirements = value ? structuredClone(value) : undefined
    this.#changed()
  }

  samples(value?: ExperimentSamplePageProjection): void {
    this.#samples = value ? structuredClone(value) : undefined
    this.#changed()
  }

  connection(
    value: ExperimentConnection,
    reason?: string,
  ): void {
    this.#connection = value
    this.#connectionReason = reason
    if (value === "online") this.#lastRefreshAt = Date.now()
    this.#changed()
  }

  nextPoll(value?: number): void {
    this.#nextPollAt = value
    this.#changed()
  }

  #clearResources(): void {
    this.#report = undefined
    this.#bundle = undefined
    this.#requirements = undefined
    this.#samples = undefined
  }

  #changed(): void {
    this.#revision += 1
    this.#snapshot = undefined
  }
}
