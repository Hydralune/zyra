import type {
  PolicyApi,
  PolicyEvidenceQuery,
} from "../../../api/policy-api.ts"
import {
  PolicyEvidenceStore,
  type PolicyEvidenceSnapshot,
} from "./store.ts"

function errorMessage(value: unknown): string {
  return value instanceof Error
    ? value.message
    : String(value || "Policy evidence projection failed.")
}

export class PolicyEvidenceRuntime {
  readonly api: PolicyApi
  readonly store: PolicyEvidenceStore
  readonly pageLimit: number
  #query: PolicyEvidenceQuery
  #abort?: AbortController
  #generation = 0
  #closed = false

  constructor(input: {
    api: PolicyApi
    query?: PolicyEvidenceQuery
    pageLimit?: number
    maximumTransitions?: number
  }) {
    this.api = input.api
    this.pageLimit = Math.max(1, Math.min(200, input.pageLimit ?? 100))
    this.#query = Object.freeze({ ...(input.query ?? {}) })
    this.store = new PolicyEvidenceStore(this.#query, {
      maximumTransitions: input.maximumTransitions,
    })
  }

  getSnapshot = (): PolicyEvidenceSnapshot => this.store.getSnapshot()

  subscribe = (listener: () => void): (() => void) =>
    this.store.subscribe(listener)

  async open(): Promise<PolicyEvidenceSnapshot> {
    this.#assertOpen()
    this.store.reset(this.#query)
    return this.#load("")
  }

  async refresh(): Promise<PolicyEvidenceSnapshot> {
    return this.open()
  }

  async setFilters(
    filters: Pick<
      PolicyEvidenceQuery,
      "mechanismVersion" | "receiptId" | "reportId"
    >,
  ): Promise<PolicyEvidenceSnapshot> {
    this.#assertOpen()
    this.#query = Object.freeze({
      ...this.#query,
      mechanismVersion: filters.mechanismVersion,
      receiptId: filters.receiptId,
      reportId: filters.reportId ?? this.#query.reportId,
    })
    return this.open()
  }

  async loadNext(): Promise<PolicyEvidenceSnapshot> {
    this.#assertOpen()
    const snapshot = this.getSnapshot()
    if (!snapshot.hasMore || !snapshot.cursor) return snapshot
    return this.#load(snapshot.cursor)
  }

  select(transitionId: string): void {
    this.store.select(transitionId)
  }

  exportLoaded(): string {
    const snapshot = this.getSnapshot()
    return JSON.stringify(
      {
        schema_version: "zyra.policy-evidence-export/v1",
        complete: !snapshot.hasMore && snapshot.droppedTransitions === 0,
        truncated: snapshot.droppedTransitions > 0,
        filters: snapshot.query,
        filter_digest: snapshot.filterDigest,
        snapshot_digest: snapshot.snapshotDigest,
        high_watermark: snapshot.highWatermark,
        evidence_digests: snapshot.evidenceDigests,
        transitions: snapshot.transitions,
        issues: snapshot.issues,
        metric_report: snapshot.metricReport,
        dropped_transitions: snapshot.droppedTransitions,
        retained_transition_count: snapshot.transitions.length,
        canonical_write_allowed: false,
      },
      null,
      2,
    )
  }

  close(reason = "Policy evidence runtime closed."): void {
    if (this.#closed) return
    this.#closed = true
    this.#generation += 1
    this.#abort?.abort(reason)
    this.#abort = undefined
    this.store.close(reason)
  }

  async #load(cursor: string): Promise<PolicyEvidenceSnapshot> {
    const generation = ++this.#generation
    this.#abort?.abort("Policy evidence request superseded.")
    this.#abort = new AbortController()
    this.store.loading()
    try {
      const page = await this.api.evidence({
        ...this.#query,
        cursor: cursor || undefined,
        limit: this.pageLimit,
        signal: this.#abort.signal,
      })
      if (generation !== this.#generation || this.#closed) {
        return this.getSnapshot()
      }
      this.store.append(page)
    } catch (error) {
      if (generation !== this.#generation || this.#closed) {
        return this.getSnapshot()
      }
      this.store.failed(errorMessage(error), true)
    } finally {
      if (generation === this.#generation) this.#abort = undefined
    }
    return this.getSnapshot()
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Policy evidence runtime is closed.")
  }
}
