import {
  DeliveryDisposition,
  EventSettlement,
  eventPartKey,
  type DeliveryReceipt,
  type IngressCapacity,
  type IngressEventFrame,
  type JsonValue,
} from "./contracts.ts"
import {
  EventIngressError,
  IngressErrorCode,
} from "./errors.ts"

interface PartialChain {
  key: string
  parentEventId: string
  taskId: string
  generation: number
  frames: IngressEventFrame[]
  eventIds: Set<string>
  firstSequence: number
  lastSequence: number
  bytes: number
  startedAtMs: number
  updatedAtMs: number
  finalEventId?: string
  tombstoned: boolean
}

interface OrphanRecord {
  frame: IngressEventFrame
  parentEventId: string
  insertedAtMs: number
  expiresAtMs: number
}

interface TombstoneRecord {
  targetId: string
  eventId: string
  sequence: number
  generation: number
  observedAtMs: number
}

export interface AssemblyDecision {
  disposition:
    | typeof DeliveryDisposition.ACCEPTED
    | typeof DeliveryDisposition.BUFFERED_ORPHAN
    | typeof DeliveryDisposition.COALESCED
    | typeof DeliveryDisposition.TOMBSTONED
    | typeof DeliveryDisposition.DUPLICATE
  deliver: readonly IngressEventFrame[]
  receipts: readonly DeliveryReceipt[]
  releasedOrphans: number
  settledChain?: {
    key: string
    parentEventId: string
    firstSequence: number
    lastSequence: number
    eventIds: readonly string[]
    bytes: number
  }
}

export class PartialEventAssembler {
  readonly #capacity: IngressCapacity
  readonly #now: () => number
  readonly #chains = new Map<string, PartialChain>()
  readonly #chainByEventId = new Map<string, PartialChain>()
  readonly #orphansByParent = new Map<string, OrphanRecord[]>()
  readonly #orphansByEvent = new Map<string, OrphanRecord>()
  readonly #tombstones = new Map<string, TombstoneRecord>()
  readonly #settledParents = new Map<string, number>()
  #partialBytes = 0
  #orphanBytes = 0
  #settled = 0
  #duplicates = 0
  #tombstoned = 0
  #expired = 0
  #disabled = false

  constructor(capacity: IngressCapacity, options: { now?: () => number } = {}) {
    this.#capacity = capacity
    this.#now = options.now ?? Date.now
  }

  disable(): void {
    this.#disabled = true
  }

  enable(): void {
    this.#disabled = false
  }

  process(frame: IngressEventFrame): AssemblyDecision {
    this.#assertEnabled()
    const expired = this.expire()
    if (frame.event.settlement === EventSettlement.TOMBSTONE) {
      return this.#processTombstone(frame, expired)
    }
    if (this.#tombstones.has(frame.eventId)) {
      this.#tombstoned += 1
      return {
        disposition: DeliveryDisposition.TOMBSTONED,
        deliver: Object.freeze([]),
        receipts: Object.freeze([
          ...expired,
          this.#receipt(
            frame,
            DeliveryDisposition.TOMBSTONED,
            "event_identity_was_tombstoned",
          ),
        ]),
        releasedOrphans: 0,
      }
    }
    const parentEventId = frame.event.parentEventId
    if (parentEventId && !this.#parentKnown(parentEventId)) {
      return this.#bufferOrphan(frame, parentEventId, expired)
    }
    if (frame.event.settlement === EventSettlement.PARTIAL) {
      return this.#processPartial(frame, expired)
    }
    if (frame.event.settlement === EventSettlement.FINAL) {
      return this.#processFinal(frame, expired)
    }
    return this.#processAtomic(frame, expired)
  }

  expire(atMs = this.#now()): readonly DeliveryReceipt[] {
    this.#assertEnabled()
    const receipts: DeliveryReceipt[] = []
    for (const orphan of [...this.#orphansByEvent.values()]) {
      if (orphan.expiresAtMs > atMs) continue
      this.#deleteOrphan(orphan)
      this.#expired += 1
      receipts.push(
        this.#receipt(
          orphan.frame,
          DeliveryDisposition.EVICTED,
          "orphan_expired",
        ),
      )
    }
    const retention = this.#capacity.maxOrphanAgeMs * 4
    for (const [parent, settledAt] of this.#settledParents) {
      if (atMs - settledAt > retention) this.#settledParents.delete(parent)
    }
    for (const [target, record] of this.#tombstones) {
      if (atMs - record.observedAtMs > retention) this.#tombstones.delete(target)
    }
    return Object.freeze(receipts)
  }

  chainForEvent(eventId: string): {
    key: string
    parentEventId: string
    eventIds: readonly string[]
    firstSequence: number
    lastSequence: number
    bytes: number
    finalEventId?: string
    tombstoned: boolean
  } | undefined {
    const chain = this.#chainByEventId.get(eventId)
    if (!chain) return undefined
    return {
      key: chain.key,
      parentEventId: chain.parentEventId,
      eventIds: Object.freeze(chain.frames.map((item) => item.eventId)),
      firstSequence: chain.firstSequence,
      lastSequence: chain.lastSequence,
      bytes: chain.bytes,
      finalEventId: chain.finalEventId,
      tombstoned: chain.tombstoned,
    }
  }

  orphanCount(parentEventId?: string): number {
    if (parentEventId !== undefined) {
      return this.#orphansByParent.get(parentEventId)?.length ?? 0
    }
    return this.#orphansByEvent.size
  }

  tombstoned(targetId: string): boolean {
    return this.#tombstones.has(targetId)
  }

  reset(reason = "assembly_reset"): readonly DeliveryReceipt[] {
    const receipts: DeliveryReceipt[] = []
    for (const chain of this.#chains.values()) {
      for (const frame of chain.frames) {
        receipts.push(
          this.#receipt(
            frame,
            DeliveryDisposition.EVICTED,
            reason,
          ),
        )
      }
    }
    for (const orphan of this.#orphansByEvent.values()) {
      receipts.push(
        this.#receipt(
          orphan.frame,
          DeliveryDisposition.EVICTED,
          reason,
        ),
      )
    }
    this.#chains.clear()
    this.#chainByEventId.clear()
    this.#orphansByParent.clear()
    this.#orphansByEvent.clear()
    this.#tombstones.clear()
    this.#settledParents.clear()
    this.#partialBytes = 0
    this.#orphanBytes = 0
    return Object.freeze(receipts)
  }

  snapshot(): {
    chains: number
    partialFrames: number
    partialBytes: number
    orphanParents: number
    orphans: number
    orphanBytes: number
    tombstones: number
    settledParents: number
    settled: number
    duplicates: number
    tombstoned: number
    expired: number
  } {
    let partialFrames = 0
    for (const chain of this.#chains.values()) partialFrames += chain.frames.length
    return {
      chains: this.#chains.size,
      partialFrames,
      partialBytes: this.#partialBytes,
      orphanParents: this.#orphansByParent.size,
      orphans: this.#orphansByEvent.size,
      orphanBytes: this.#orphanBytes,
      tombstones: this.#tombstones.size,
      settledParents: this.#settledParents.size,
      settled: this.#settled,
      duplicates: this.#duplicates,
      tombstoned: this.#tombstoned,
      expired: this.#expired,
    }
  }

  audit(): { ok: boolean; findings: readonly string[] } {
    const findings: string[] = []
    let partialBytes = 0
    for (const [key, chain] of this.#chains) {
      if (key !== chain.key) findings.push(`chain_key_mismatch:${key}`)
      if (!chain.frames.length) findings.push(`empty_chain:${key}`)
      let previous = -1
      let computedBytes = 0
      for (const frame of chain.frames) {
        if (frame.sequence <= previous) findings.push(`chain_sequence_regression:${key}`)
        previous = frame.sequence
        computedBytes += frame.encodedBytes
        if (this.#chainByEventId.get(frame.eventId) !== chain) {
          findings.push(`chain_event_index_mismatch:${frame.eventId}`)
        }
      }
      if (computedBytes !== chain.bytes) findings.push(`chain_byte_mismatch:${key}`)
      partialBytes += computedBytes
      if (findings.length >= 100) break
    }
    if (partialBytes !== this.#partialBytes) {
      findings.push(`partial_byte_total_mismatch:${this.#partialBytes}:${partialBytes}`)
    }
    let orphanBytes = 0
    for (const [eventId, orphan] of this.#orphansByEvent) {
      orphanBytes += orphan.frame.encodedBytes
      if (eventId !== orphan.frame.eventId) findings.push(`orphan_event_key_mismatch:${eventId}`)
      if (!this.#orphansByParent.get(orphan.parentEventId)?.includes(orphan)) {
        findings.push(`orphan_parent_index_mismatch:${eventId}`)
      }
      if (findings.length >= 100) break
    }
    if (orphanBytes !== this.#orphanBytes) {
      findings.push(`orphan_byte_total_mismatch:${this.#orphanBytes}:${orphanBytes}`)
    }
    if (this.#orphansByEvent.size > this.#capacity.maxOrphans) {
      findings.push("orphan_capacity_exceeded")
    }
    return { ok: findings.length === 0, findings: Object.freeze(findings) }
  }

  exportState(limit = 1024): JsonValue {
    const chains = [...this.#chains.values()].slice(0, limit).map((chain) => ({
      key: chain.key,
      parentEventId: chain.parentEventId,
      eventIds: chain.frames.map((frame) => frame.eventId),
      firstSequence: chain.firstSequence,
      lastSequence: chain.lastSequence,
      bytes: chain.bytes,
      finalEventId: chain.finalEventId ?? null,
      tombstoned: chain.tombstoned,
    }))
    const orphans = [...this.#orphansByEvent.values()].slice(0, limit).map((orphan) => ({
      eventId: orphan.frame.eventId,
      parentEventId: orphan.parentEventId,
      sequence: orphan.frame.sequence,
      insertedAtMs: orphan.insertedAtMs,
      expiresAtMs: orphan.expiresAtMs,
    }))
    return {
      snapshot: this.snapshot() as unknown as JsonValue,
      chains: chains as unknown as JsonValue,
      orphans: orphans as unknown as JsonValue,
      audit: this.audit() as unknown as JsonValue,
      disabled: this.#disabled,
    }
  }

  #processAtomic(
    frame: IngressEventFrame,
    expired: readonly DeliveryReceipt[],
  ): AssemblyDecision {
    this.#settledParents.set(frame.eventId, this.#now())
    const released = this.#releaseOrphans(frame.eventId)
    return {
      disposition: DeliveryDisposition.ACCEPTED,
      deliver: Object.freeze([frame, ...released.frames]),
      receipts: Object.freeze([
        ...expired,
        this.#receipt(
          frame,
          DeliveryDisposition.ACCEPTED,
          "atomic_event_ready",
        ),
        ...released.receipts,
      ]),
      releasedOrphans: released.frames.length,
    }
  }

  #processPartial(
    frame: IngressEventFrame,
    expired: readonly DeliveryReceipt[],
  ): AssemblyDecision {
    const key = eventPartKey(frame.event)
    const existing = this.#chains.get(key)
    if (existing?.eventIds.has(frame.eventId)) {
      this.#duplicates += 1
      return {
        disposition: DeliveryDisposition.DUPLICATE,
        deliver: Object.freeze([]),
        receipts: Object.freeze([
          ...expired,
          this.#receipt(
            frame,
            DeliveryDisposition.DUPLICATE,
            "partial_event_already_observed",
          ),
        ]),
        releasedOrphans: 0,
      }
    }
    const chain = existing ?? this.#createChain(frame, key, false)
    if (chain.finalEventId) {
      throw new EventIngressError(
        IngressErrorCode.PARTIAL_CONFLICT,
        "Partial event arrived after its chain was finalized.",
        {
          resyncRequired: true,
          context: {
            taskId: frame.taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
            details: {
              chain: key,
              finalEventId: chain.finalEventId,
            },
          },
        },
      )
    }
    if (existing && frame.sequence <= chain.lastSequence) {
      throw new EventIngressError(
        IngressErrorCode.PARTIAL_CONFLICT,
        "Partial event chain regressed its canonical sequence.",
        {
          resyncRequired: true,
          context: {
            taskId: frame.taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
            details: { chain: key, lastSequence: chain.lastSequence },
          },
        },
      )
    }
    this.#appendChain(chain, frame)
    this.#assertPartialCapacity(frame)
    const parentId = frame.event.parentEventId ?? frame.eventId
    this.#settledParents.set(parentId, this.#now())
    const released = this.#releaseOrphans(parentId)
    return {
      disposition: DeliveryDisposition.COALESCED,
      deliver: Object.freeze([frame, ...released.frames]),
      receipts: Object.freeze([
        ...expired,
        this.#receipt(
          frame,
          DeliveryDisposition.COALESCED,
          `partial_chain:${key}`,
        ),
        ...released.receipts,
      ]),
      releasedOrphans: released.frames.length,
    }
  }

  #processFinal(
    frame: IngressEventFrame,
    expired: readonly DeliveryReceipt[],
  ): AssemblyDecision {
    const key = eventPartKey(frame.event)
    const chain = this.#chains.get(key)
    if (chain?.eventIds.has(frame.eventId)) {
      this.#duplicates += 1
      return {
        disposition: DeliveryDisposition.DUPLICATE,
        deliver: Object.freeze([]),
        receipts: Object.freeze([
          ...expired,
          this.#receipt(
            frame,
            DeliveryDisposition.DUPLICATE,
            "final_event_already_observed",
          ),
        ]),
        releasedOrphans: 0,
      }
    }
    const materialized = chain ?? this.#createChain(frame, key, false)
    if (materialized.finalEventId && materialized.finalEventId !== frame.eventId) {
      throw new EventIngressError(
        IngressErrorCode.PARTIAL_CONFLICT,
        "Partial chain received multiple final events.",
        {
          resyncRequired: true,
          context: {
            taskId: frame.taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
            details: {
              chain: key,
              knownFinalEventId: materialized.finalEventId,
            },
          },
        },
      )
    }
    this.#appendChain(materialized, frame)
    materialized.finalEventId = frame.eventId
    this.#settled += 1
    this.#settledParents.set(materialized.parentEventId, this.#now())
    this.#settledParents.set(frame.eventId, this.#now())
    const summary = {
      key: materialized.key,
      parentEventId: materialized.parentEventId,
      firstSequence: materialized.firstSequence,
      lastSequence: materialized.lastSequence,
      eventIds: Object.freeze(materialized.frames.map((item) => item.eventId)),
      bytes: materialized.bytes,
    }
    this.#removeChain(materialized)
    const released = this.#releaseOrphans(materialized.parentEventId)
    const direct = this.#releaseOrphans(frame.eventId)
    return {
      disposition: DeliveryDisposition.ACCEPTED,
      deliver: Object.freeze([
        frame,
        ...released.frames,
        ...direct.frames,
      ]),
      receipts: Object.freeze([
        ...expired,
        this.#receipt(
          frame,
          DeliveryDisposition.ACCEPTED,
          `partial_chain_final:${key}`,
        ),
        ...released.receipts,
        ...direct.receipts,
      ]),
      releasedOrphans: released.frames.length + direct.frames.length,
      settledChain: summary,
    }
  }

  #processTombstone(
    frame: IngressEventFrame,
    expired: readonly DeliveryReceipt[],
  ): AssemblyDecision {
    const targetId = frame.event.tombstoneTargetId!
    const known = this.#tombstones.get(targetId)
    if (known) {
      if (known.eventId !== frame.eventId || known.sequence !== frame.sequence) {
        throw new EventIngressError(
          IngressErrorCode.TOMBSTONE_CONFLICT,
          "A tombstone target was assigned conflicting canonical tombstones.",
          {
            resyncRequired: true,
            context: {
              taskId: frame.taskId,
              generation: frame.generation,
              eventId: frame.eventId,
              sequence: frame.sequence,
              details: {
                targetId,
                knownEventId: known.eventId,
                knownSequence: known.sequence,
              },
            },
          },
        )
      }
      this.#duplicates += 1
      return {
        disposition: DeliveryDisposition.DUPLICATE,
        deliver: Object.freeze([]),
        receipts: Object.freeze([
          ...expired,
          this.#receipt(
            frame,
            DeliveryDisposition.DUPLICATE,
            "tombstone_already_observed",
          ),
        ]),
        releasedOrphans: 0,
      }
    }
    const now = this.#now()
    this.#tombstones.set(targetId, {
      targetId,
      eventId: frame.eventId,
      sequence: frame.sequence,
      generation: frame.generation,
      observedAtMs: now,
    })
    const receipts: DeliveryReceipt[] = [...expired]
    const orphan = this.#orphansByEvent.get(targetId)
    if (orphan) {
      this.#deleteOrphan(orphan)
      receipts.push(
        this.#receipt(
          orphan.frame,
          DeliveryDisposition.TOMBSTONED,
          `removed_by:${frame.eventId}`,
        ),
      )
    }
    const chain = this.#chainByEventId.get(targetId)
    if (chain) {
      chain.tombstoned = true
      for (const partial of chain.frames) {
        receipts.push(
          this.#receipt(
            partial,
            DeliveryDisposition.TOMBSTONED,
            `chain_removed_by:${frame.eventId}`,
          ),
        )
      }
      this.#removeChain(chain)
    }
    this.#tombstoned += 1
    receipts.push(
      this.#receipt(
        frame,
        DeliveryDisposition.TOMBSTONED,
        `tombstone_applied:${targetId}`,
      ),
    )
    this.#settledParents.set(frame.eventId, now)
    const released = this.#releaseOrphans(frame.eventId)
    return {
      disposition: DeliveryDisposition.TOMBSTONED,
      deliver: Object.freeze([frame, ...released.frames]),
      receipts: Object.freeze([...receipts, ...released.receipts]),
      releasedOrphans: released.frames.length,
    }
  }

  #bufferOrphan(
    frame: IngressEventFrame,
    parentEventId: string,
    expired: readonly DeliveryReceipt[],
  ): AssemblyDecision {
    const existing = this.#orphansByEvent.get(frame.eventId)
    if (existing) {
      if (
        existing.frame.sequence !== frame.sequence ||
        existing.frame.event.contentDigest !== frame.event.contentDigest
      ) {
        throw new EventIngressError(
          IngressErrorCode.DUPLICATE_CONFLICT,
          "Orphan identity conflicts with an existing buffered child.",
          {
            resyncRequired: true,
            context: {
              taskId: frame.taskId,
              generation: frame.generation,
              eventId: frame.eventId,
              sequence: frame.sequence,
            },
          },
        )
      }
      this.#duplicates += 1
      return {
        disposition: DeliveryDisposition.DUPLICATE,
        deliver: Object.freeze([]),
        receipts: Object.freeze([
          ...expired,
          this.#receipt(
            frame,
            DeliveryDisposition.DUPLICATE,
            "orphan_already_buffered",
          ),
        ]),
        releasedOrphans: 0,
      }
    }
    if (
      this.#orphansByEvent.size >= this.#capacity.maxOrphans ||
      this.#orphanBytes + frame.encodedBytes > this.#capacity.maxGapBytes
    ) {
      throw new EventIngressError(
        IngressErrorCode.ORPHAN_OVERFLOW,
        "Bounded orphan buffer cannot retain another child event.",
        {
          retryable: true,
          resyncRequired: true,
          context: {
            taskId: frame.taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
            details: {
              parentEventId,
              orphanCount: this.#orphansByEvent.size,
              orphanBytes: this.#orphanBytes,
              maxOrphans: this.#capacity.maxOrphans,
              maxBytes: this.#capacity.maxGapBytes,
            },
          },
        },
      )
    }
    const now = this.#now()
    const orphan: OrphanRecord = {
      frame,
      parentEventId,
      insertedAtMs: now,
      expiresAtMs: now + this.#capacity.maxOrphanAgeMs,
    }
    this.#orphansByEvent.set(frame.eventId, orphan)
    const siblings = this.#orphansByParent.get(parentEventId) ?? []
    siblings.push(orphan)
    siblings.sort((left, right) =>
      left.frame.sequence - right.frame.sequence ||
      left.frame.eventId.localeCompare(right.frame.eventId),
    )
    this.#orphansByParent.set(parentEventId, siblings)
    this.#orphanBytes += frame.encodedBytes
    return {
      disposition: DeliveryDisposition.BUFFERED_ORPHAN,
      deliver: Object.freeze([]),
      receipts: Object.freeze([
        ...expired,
        this.#receipt(
          frame,
          DeliveryDisposition.BUFFERED_ORPHAN,
          `waiting_for_parent:${parentEventId}`,
        ),
      ]),
      releasedOrphans: 0,
    }
  }

  #releaseOrphans(parentEventId: string): {
    frames: IngressEventFrame[]
    receipts: DeliveryReceipt[]
  } {
    const orphans = [...(this.#orphansByParent.get(parentEventId) ?? [])]
      .sort((left, right) =>
        left.frame.sequence - right.frame.sequence ||
        left.frame.eventId.localeCompare(right.frame.eventId),
      )
    const frames: IngressEventFrame[] = []
    const receipts: DeliveryReceipt[] = []
    for (const orphan of orphans) {
      this.#deleteOrphan(orphan)
      if (this.#tombstones.has(orphan.frame.eventId)) {
        receipts.push(
          this.#receipt(
            orphan.frame,
            DeliveryDisposition.TOMBSTONED,
            "orphan_was_tombstoned_before_parent",
          ),
        )
        continue
      }
      frames.push(orphan.frame)
      receipts.push(
        this.#receipt(
          orphan.frame,
          DeliveryDisposition.ACCEPTED,
          `parent_arrived:${parentEventId}`,
        ),
      )
      this.#settledParents.set(orphan.frame.eventId, this.#now())
    }
    return { frames, receipts }
  }

  #createChain(
    frame: IngressEventFrame,
    key: string,
    include = true,
  ): PartialChain {
    const parentEventId = frame.event.parentEventId ?? frame.eventId
    const now = this.#now()
    const chain: PartialChain = {
      key,
      parentEventId,
      taskId: frame.taskId,
      generation: frame.generation,
      frames: [],
      eventIds: new Set(),
      firstSequence: frame.sequence,
      lastSequence: frame.sequence - 1,
      bytes: 0,
      startedAtMs: now,
      updatedAtMs: now,
      tombstoned: false,
    }
    this.#chains.set(key, chain)
    if (include) this.#appendChain(chain, frame)
    return chain
  }

  #appendChain(chain: PartialChain, frame: IngressEventFrame): void {
    if (chain.generation !== frame.generation || chain.taskId !== frame.taskId) {
      throw new EventIngressError(
        IngressErrorCode.PARTIAL_CONFLICT,
        "Partial chain crossed task or generation boundaries.",
        {
          resyncRequired: true,
          context: {
            taskId: frame.taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
          },
        },
      )
    }
    chain.frames.push(frame)
    chain.eventIds.add(frame.eventId)
    chain.firstSequence = Math.min(chain.firstSequence, frame.sequence)
    chain.lastSequence = Math.max(chain.lastSequence, frame.sequence)
    chain.bytes += frame.encodedBytes
    chain.updatedAtMs = this.#now()
    this.#partialBytes += frame.encodedBytes
    this.#chainByEventId.set(frame.eventId, chain)
  }

  #removeChain(chain: PartialChain): void {
    if (this.#chains.get(chain.key) === chain) this.#chains.delete(chain.key)
    for (const frame of chain.frames) {
      if (this.#chainByEventId.get(frame.eventId) === chain) {
        this.#chainByEventId.delete(frame.eventId)
      }
    }
    this.#partialBytes -= chain.bytes
    if (this.#partialBytes < 0) {
      throw new EventIngressError(
        IngressErrorCode.INTERNAL_INVARIANT,
        "Partial assembly byte accounting underflowed.",
        { resyncRequired: true },
      )
    }
  }

  #deleteOrphan(orphan: OrphanRecord): void {
    if (this.#orphansByEvent.get(orphan.frame.eventId) !== orphan) return
    this.#orphansByEvent.delete(orphan.frame.eventId)
    const siblings = this.#orphansByParent.get(orphan.parentEventId)
    if (siblings) {
      const index = siblings.indexOf(orphan)
      if (index >= 0) siblings.splice(index, 1)
      if (!siblings.length) this.#orphansByParent.delete(orphan.parentEventId)
    }
    this.#orphanBytes -= orphan.frame.encodedBytes
    if (this.#orphanBytes < 0) {
      throw new EventIngressError(
        IngressErrorCode.INTERNAL_INVARIANT,
        "Orphan byte accounting underflowed.",
        { resyncRequired: true },
      )
    }
  }

  #parentKnown(parentEventId: string): boolean {
    return (
      this.#settledParents.has(parentEventId) ||
      this.#chainByEventId.has(parentEventId) ||
      this.#tombstones.has(parentEventId)
    )
  }

  #assertPartialCapacity(frame: IngressEventFrame): void {
    if (
      this.#partialBytes <= this.#capacity.maxGapBytes &&
      this.#chainByEventId.size <= this.#capacity.maxGapItems
    ) return
    throw new EventIngressError(
      IngressErrorCode.BUFFER_OVERFLOW,
      "Partial assembly exceeded its bounded capacity.",
      {
        retryable: true,
        resyncRequired: true,
        context: {
          taskId: frame.taskId,
          generation: frame.generation,
          eventId: frame.eventId,
          sequence: frame.sequence,
          details: {
            partialBytes: this.#partialBytes,
            partialItems: this.#chainByEventId.size,
            maxBytes: this.#capacity.maxGapBytes,
            maxItems: this.#capacity.maxGapItems,
          },
        },
      },
    )
  }

  #receipt(
    frame: IngressEventFrame,
    disposition: DeliveryReceipt["disposition"],
    reason: string,
  ): DeliveryReceipt {
    return {
      eventId: frame.eventId,
      sequence: frame.sequence,
      generation: frame.generation,
      disposition,
      reason,
      observedAtMs: this.#now(),
      encodedBytes: frame.encodedBytes,
    }
  }

  #assertEnabled(): void {
    if (!this.#disabled) return
    throw new EventIngressError(
      IngressErrorCode.DISABLED,
      "Partial event assembler is disabled.",
    )
  }
}
