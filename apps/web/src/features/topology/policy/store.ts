import type {
  PolicyEvidenceIssue,
  PolicyEvidencePage,
  PolicyEvidenceTransition,
  PolicyEvidenceQuery,
} from "../../../api/policy-api.ts"

export type PolicyEvidenceConnection =
  | "idle"
  | "loading"
  | "ready"
  | "degraded"
  | "error"
  | "closed"

export interface PolicyEvidenceSnapshot {
  readonly revision: number
  readonly connection: PolicyEvidenceConnection
  readonly query: Readonly<PolicyEvidenceQuery>
  readonly filterDigest: string
  readonly snapshotDigest: string
  readonly highWatermark: number
  readonly cursor: string
  readonly hasMore: boolean
  readonly transitions: readonly PolicyEvidenceTransition[]
  readonly issues: readonly PolicyEvidenceIssue[]
  readonly evidenceDigests: readonly string[]
  readonly metricReport: Readonly<Record<string, unknown>>
  readonly selectedTransitionId: string
  readonly error: string
  readonly droppedTransitions: number
}

function frozenQuery(value: PolicyEvidenceQuery): Readonly<PolicyEvidenceQuery> {
  return Object.freeze({
    runId: value.runId,
    taskId: value.taskId,
    mechanismVersion: value.mechanismVersion,
    receiptId: value.receiptId,
    reportId: value.reportId,
    limit: value.limit,
  })
}

function initial(query: PolicyEvidenceQuery): PolicyEvidenceSnapshot {
  return Object.freeze({
    revision: 0,
    connection: "idle",
    query: frozenQuery(query),
    filterDigest: "",
    snapshotDigest: "",
    highWatermark: 0,
    cursor: "",
    hasMore: false,
    transitions: Object.freeze([]),
    issues: Object.freeze([]),
    evidenceDigests: Object.freeze([]),
    metricReport: Object.freeze({}),
    selectedTransitionId: "",
    error: "",
    droppedTransitions: 0,
  })
}

export class PolicyEvidenceStore {
  readonly maximumTransitions: number
  #snapshot: PolicyEvidenceSnapshot
  #listeners = new Set<() => void>()

  constructor(
    query: PolicyEvidenceQuery = {},
    options: { maximumTransitions?: number } = {},
  ) {
    this.maximumTransitions = Math.max(
      200,
      options.maximumTransitions ?? 5_000,
    )
    this.#snapshot = initial(query)
  }

  getSnapshot = (): PolicyEvidenceSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  reset(query: PolicyEvidenceQuery): void {
    const next = initial(query)
    this.#set({ ...next, revision: this.#snapshot.revision + 1 })
  }

  loading(): void {
    this.#set({
      ...this.#snapshot,
      revision: this.#snapshot.revision + 1,
      connection: "loading",
      error: "",
    })
  }

  append(page: PolicyEvidencePage): void {
    if (
      this.#snapshot.filterDigest
      && this.#snapshot.filterDigest !== page.filter_digest
    ) {
      throw new TypeError("Policy evidence filter digest changed mid-stream.")
    }
    if (
      this.#snapshot.snapshotDigest
      && this.#snapshot.snapshotDigest !== page.snapshot_digest
    ) {
      throw new TypeError("Policy evidence snapshot changed mid-stream.")
    }
    const byId = new Map(
      this.#snapshot.transitions.map((item) => [item.transition_id, item]),
    )
    for (const item of page.transitions) {
      const prior = byId.get(item.transition_id)
      if (prior && prior.contract_digest !== item.contract_digest) {
        throw new TypeError(
          `Policy transition ${item.transition_id} changed digest.`,
        )
      }
      byId.set(item.transition_id, item)
    }
    const ordered = [...byId.values()].sort(
      (left, right) => left.sequence - right.sequence,
    )
    const overflow = Math.max(0, ordered.length - this.maximumTransitions)
    const retained = overflow > 0 ? ordered.slice(overflow) : ordered
    const digests = new Set(this.#snapshot.evidenceDigests)
    digests.add(page.evidence_digest)
    this.#set({
      ...this.#snapshot,
      revision: this.#snapshot.revision + 1,
      connection: page.status === "degraded" ? "degraded" : "ready",
      filterDigest: page.filter_digest,
      snapshotDigest: page.snapshot_digest,
      highWatermark: page.high_watermark,
      cursor: page.next_cursor,
      hasMore: page.has_more,
      transitions: Object.freeze(retained),
      issues: Object.freeze([
        ...this.#snapshot.issues,
        ...page.issues,
      ]),
      evidenceDigests: Object.freeze([...digests]),
      metricReport: page.metric_report,
      error: "",
      droppedTransitions: this.#snapshot.droppedTransitions + overflow,
    })
  }

  failed(message: string, degraded = true): void {
    this.#set({
      ...this.#snapshot,
      revision: this.#snapshot.revision + 1,
      connection: degraded ? "degraded" : "error",
      error: message,
    })
  }

  select(transitionId: string): void {
    if (
      transitionId
      && !this.#snapshot.transitions.some(
        (item) => item.transition_id === transitionId,
      )
    ) {
      return
    }
    this.#set({
      ...this.#snapshot,
      revision: this.#snapshot.revision + 1,
      selectedTransitionId: transitionId,
    })
  }

  close(reason = "Policy evidence projection closed."): void {
    this.#set({
      ...this.#snapshot,
      revision: this.#snapshot.revision + 1,
      connection: "closed",
      error: reason,
    })
    this.#listeners.clear()
  }

  #set(value: PolicyEvidenceSnapshot): void {
    this.#snapshot = Object.freeze(value)
    for (const listener of this.#listeners) listener()
  }
}
