import {
  DeliveryDisposition,
  eventIdentityKey,
  type DeliveryReceipt,
  type IngressEventFrame,
  type JsonValue,
} from "./contracts.ts"
import {
  EventIngressError,
  IngressErrorCode,
} from "./errors.ts"

interface IdentityRecord {
  eventId: string
  sequence: number
  contentDigest: string
  identityKey: string
  generation: number
  firstObservedAtMs: number
  lastObservedAtMs: number
  observations: number
  committed: boolean
  source: string
}

export interface IdentityDecision {
  disposition:
    | typeof DeliveryDisposition.ACCEPTED
    | typeof DeliveryDisposition.DUPLICATE
    | typeof DeliveryDisposition.STALE
  record: Readonly<IdentityRecord>
  receipt: DeliveryReceipt
}

export class BoundedEventIdentityWindow {
  readonly #capacity: number
  readonly #now: () => number
  readonly #byId = new Map<string, IdentityRecord>()
  readonly #bySequence = new Map<number, IdentityRecord>()
  readonly #byKey = new Map<string, IdentityRecord>()
  #committedSequence = 0
  #duplicates = 0
  #stale = 0
  #conflicts = 0
  #evictions = 0
  #disabled = false

  constructor(capacity: number, options: { now?: () => number } = {}) {
    if (!Number.isSafeInteger(capacity) || capacity < 64) {
      throw new TypeError("Identity window capacity must be an integer of at least 64")
    }
    this.#capacity = capacity
    this.#now = options.now ?? Date.now
  }

  get size(): number {
    return this.#byId.size
  }

  get committedSequence(): number {
    return this.#committedSequence
  }

  disable(): void {
    this.#disabled = true
  }

  enable(): void {
    this.#disabled = false
  }

  observe(frame: IngressEventFrame, committedSequence: number): IdentityDecision {
    this.#assertEnabled()
    this.#committedSequence = Math.max(this.#committedSequence, committedSequence)
    const key = eventIdentityKey(frame.event)
    const byId = this.#byId.get(frame.eventId)
    const bySequence = this.#bySequence.get(frame.sequence)
    const byKey = this.#byKey.get(key)
    this.#assertCompatible(frame, byId, "event identity")
    this.#assertCompatible(frame, bySequence, "canonical sequence")
    this.#assertCompatible(frame, byKey, "content identity")
    const existing = byId ?? bySequence ?? byKey
    const now = this.#now()
    if (existing) {
      existing.lastObservedAtMs = now
      existing.observations += 1
      this.#touch(existing)
      if (frame.sequence < committedSequence) {
        this.#stale += 1
        return {
          disposition: DeliveryDisposition.STALE,
          record: Object.freeze({ ...existing }),
          receipt: this.#receipt(
            frame,
            DeliveryDisposition.STALE,
            "sequence_precedes_committed_cursor",
            now,
          ),
        }
      }
      this.#duplicates += 1
      return {
        disposition: DeliveryDisposition.DUPLICATE,
        record: Object.freeze({ ...existing }),
        receipt: this.#receipt(
          frame,
          DeliveryDisposition.DUPLICATE,
          existing.committed
            ? "identity_already_committed"
            : "identity_already_buffered",
          now,
        ),
      }
    }
    if (frame.sequence <= committedSequence) {
      this.#stale += 1
      const record = this.#insert(frame, now, frame.sequence === committedSequence)
      return {
        disposition: DeliveryDisposition.STALE,
        record: Object.freeze({ ...record }),
        receipt: this.#receipt(
          frame,
          DeliveryDisposition.STALE,
          "sequence_not_ahead_of_committed_cursor",
          now,
        ),
      }
    }
    const record = this.#insert(frame, now, false)
    return {
      disposition: DeliveryDisposition.ACCEPTED,
      record: Object.freeze({ ...record }),
      receipt: this.#receipt(
        frame,
        DeliveryDisposition.ACCEPTED,
        "new_identity",
        now,
      ),
    }
  }

  commit(frame: IngressEventFrame, committedAtMs = this.#now()): DeliveryReceipt {
    this.#assertEnabled()
    const record = this.#byId.get(frame.eventId)
    if (!record) {
      throw new EventIngressError(
        IngressErrorCode.INTERNAL_INVARIANT,
        "Cannot commit an event that was not observed by the identity window.",
        {
          context: {
            taskId: frame.taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
          },
        },
      )
    }
    this.#assertCompatible(frame, record, "commit identity")
    record.committed = true
    record.lastObservedAtMs = committedAtMs
    this.#committedSequence = Math.max(this.#committedSequence, frame.sequence)
    this.#touch(record)
    this.#prune()
    return {
      eventId: frame.eventId,
      sequence: frame.sequence,
      generation: frame.generation,
      disposition: DeliveryDisposition.ACCEPTED,
      reason: "committed",
      observedAtMs: frame.observedAtMs,
      committedAtMs,
      encodedBytes: frame.encodedBytes,
    }
  }

  tombstone(targetId: string, sequence: number): IdentityRecord | undefined {
    this.#assertEnabled()
    const record = this.#byId.get(targetId)
    if (!record) return undefined
    if (record.sequence >= sequence) {
      throw new EventIngressError(
        IngressErrorCode.TOMBSTONE_CONFLICT,
        "Tombstone sequence must follow its target event.",
        {
          resyncRequired: true,
          context: {
            eventId: targetId,
            sequence,
            details: { targetSequence: record.sequence },
          },
        },
      )
    }
    this.#delete(record)
    return { ...record }
  }

  hasEvent(eventId: string): boolean {
    return this.#byId.has(eventId)
  }

  hasSequence(sequence: number): boolean {
    return this.#bySequence.has(sequence)
  }

  getEvent(eventId: string): Readonly<IdentityRecord> | undefined {
    const record = this.#byId.get(eventId)
    return record ? Object.freeze({ ...record }) : undefined
  }

  getSequence(sequence: number): Readonly<IdentityRecord> | undefined {
    const record = this.#bySequence.get(sequence)
    return record ? Object.freeze({ ...record }) : undefined
  }

  markCommittedThrough(sequence: number): number {
    this.#assertEnabled()
    let count = 0
    for (const record of this.#bySequence.values()) {
      if (record.sequence > sequence || record.committed) continue
      record.committed = true
      count += 1
    }
    this.#committedSequence = Math.max(this.#committedSequence, sequence)
    this.#prune()
    return count
  }

  reset(options: { preserveCommitted?: boolean; committedSequence?: number } = {}): void {
    const preserved = options.preserveCommitted
      ? [...this.#byId.values()].filter((record) => record.committed)
      : []
    this.#byId.clear()
    this.#bySequence.clear()
    this.#byKey.clear()
    this.#committedSequence = Math.max(
      0,
      Math.floor(options.committedSequence ?? this.#committedSequence),
    )
    for (const record of preserved.slice(-this.#capacity)) {
      this.#byId.set(record.eventId, record)
      this.#bySequence.set(record.sequence, record)
      this.#byKey.set(record.identityKey, record)
    }
  }

  snapshot(): {
    capacity: number
    size: number
    committed: number
    uncommitted: number
    committedSequence: number
    duplicates: number
    stale: number
    conflicts: number
    evictions: number
    oldestSequence?: number
    newestSequence?: number
  } {
    let committed = 0
    let oldestSequence: number | undefined
    let newestSequence: number | undefined
    for (const record of this.#byId.values()) {
      if (record.committed) committed += 1
      oldestSequence =
        oldestSequence === undefined
          ? record.sequence
          : Math.min(oldestSequence, record.sequence)
      newestSequence =
        newestSequence === undefined
          ? record.sequence
          : Math.max(newestSequence, record.sequence)
    }
    return {
      capacity: this.#capacity,
      size: this.#byId.size,
      committed,
      uncommitted: this.#byId.size - committed,
      committedSequence: this.#committedSequence,
      duplicates: this.#duplicates,
      stale: this.#stale,
      conflicts: this.#conflicts,
      evictions: this.#evictions,
      oldestSequence,
      newestSequence,
    }
  }

  audit(): { ok: boolean; findings: readonly string[] } {
    const findings: string[] = []
    if (this.#byId.size !== this.#bySequence.size) {
      findings.push("id_and_sequence_window_size_mismatch")
    }
    if (this.#byId.size !== this.#byKey.size) {
      findings.push("id_and_content_window_size_mismatch")
    }
    if (this.#byId.size > this.#capacity) {
      findings.push("identity_window_exceeds_capacity")
    }
    for (const record of this.#byId.values()) {
      if (this.#bySequence.get(record.sequence) !== record) {
        findings.push(`sequence_index_mismatch:${record.sequence}`)
      }
      if (this.#byKey.get(record.identityKey) !== record) {
        findings.push(`content_index_mismatch:${record.eventId}`)
      }
      if (record.committed && record.sequence > this.#committedSequence) {
        findings.push(`committed_record_ahead_of_cursor:${record.eventId}`)
      }
      if (findings.length >= 100) break
    }
    return { ok: findings.length === 0, findings: Object.freeze(findings) }
  }

  exportState(limit = 1024): JsonValue {
    const records = [...this.#byId.values()]
      .slice(-Math.max(1, Math.min(this.#capacity, Math.floor(limit))))
      .map((record) => ({ ...record }))
    return {
      snapshot: this.snapshot() as unknown as JsonValue,
      records: records as unknown as JsonValue,
      audit: this.audit() as unknown as JsonValue,
      disabled: this.#disabled,
    }
  }

  #insert(frame: IngressEventFrame, now: number, committed: boolean): IdentityRecord {
    const record: IdentityRecord = {
      eventId: frame.eventId,
      sequence: frame.sequence,
      contentDigest: frame.event.contentDigest,
      identityKey: eventIdentityKey(frame.event),
      generation: frame.generation,
      firstObservedAtMs: now,
      lastObservedAtMs: now,
      observations: 1,
      committed,
      source: frame.source,
    }
    this.#byId.set(record.eventId, record)
    this.#bySequence.set(record.sequence, record)
    this.#byKey.set(record.identityKey, record)
    this.#prune()
    return record
  }

  #touch(record: IdentityRecord): void {
    this.#byId.delete(record.eventId)
    this.#byId.set(record.eventId, record)
    this.#bySequence.delete(record.sequence)
    this.#bySequence.set(record.sequence, record)
    this.#byKey.delete(record.identityKey)
    this.#byKey.set(record.identityKey, record)
  }

  #prune(): void {
    while (this.#byId.size > this.#capacity) {
      let candidate: IdentityRecord | undefined
      for (const record of this.#byId.values()) {
        if (record.committed && record.sequence <= this.#committedSequence) {
          candidate = record
          break
        }
        candidate ??= record
      }
      if (!candidate) break
      this.#delete(candidate)
      this.#evictions += 1
    }
  }

  #delete(record: IdentityRecord): void {
    if (this.#byId.get(record.eventId) === record) this.#byId.delete(record.eventId)
    if (this.#bySequence.get(record.sequence) === record) {
      this.#bySequence.delete(record.sequence)
    }
    if (this.#byKey.get(record.identityKey) === record) {
      this.#byKey.delete(record.identityKey)
    }
  }

  #assertCompatible(
    frame: IngressEventFrame,
    record: IdentityRecord | undefined,
    dimension: string,
  ): void {
    if (!record) return
    if (
      record.eventId === frame.eventId &&
      record.sequence === frame.sequence &&
      record.contentDigest === frame.event.contentDigest
    ) return
    this.#conflicts += 1
    throw new EventIngressError(
      IngressErrorCode.DUPLICATE_CONFLICT,
      `Event ${dimension} conflicts with a previously observed canonical identity.`,
      {
        resyncRequired: true,
        context: {
          taskId: frame.taskId,
          generation: frame.generation,
          eventId: frame.eventId,
          sequence: frame.sequence,
          details: {
            dimension,
            knownEventId: record.eventId,
            knownSequence: record.sequence,
            knownDigest: record.contentDigest,
            observedDigest: frame.event.contentDigest,
          },
        },
      },
    )
  }

  #receipt(
    frame: IngressEventFrame,
    disposition: IdentityDecision["disposition"],
    reason: string,
    observedAtMs: number,
  ): DeliveryReceipt {
    return {
      eventId: frame.eventId,
      sequence: frame.sequence,
      generation: frame.generation,
      disposition,
      reason,
      observedAtMs,
      encodedBytes: frame.encodedBytes,
    }
  }

  #assertEnabled(): void {
    if (!this.#disabled) return
    throw new EventIngressError(
      IngressErrorCode.DISABLED,
      "Event identity window is disabled.",
    )
  }
}
