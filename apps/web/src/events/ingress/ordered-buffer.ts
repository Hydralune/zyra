import {
  BufferPriority,
  DeliveryDisposition,
  PressureLevel,
  eventPartKey,
  eventPriority,
  type DeliveryReceipt,
  type IngressCapacity,
  type IngressEventFrame,
  type JsonValue,
  type PressureSnapshot,
} from "./contracts.ts"
import {
  EventIngressError,
  IngressErrorCode,
  bufferOverflowError,
} from "./errors.ts"

interface BufferedFrame {
  frame: IngressEventFrame
  priority: number
  insertedAtMs: number
  updatedAtMs: number
  sources: Set<string>
  coalesced: number
}

export interface BufferInsertResult {
  disposition:
    | typeof DeliveryDisposition.ACCEPTED
    | typeof DeliveryDisposition.COALESCED
    | typeof DeliveryDisposition.DUPLICATE
    | typeof DeliveryDisposition.BUFFERED_GAP
    | typeof DeliveryDisposition.EVICTED
  receipt: DeliveryReceipt
  pressure: PressureSnapshot
  evicted: readonly DeliveryReceipt[]
}

export interface BufferDrainResult {
  frames: readonly IngressEventFrame[]
  fromSequence: number
  sequence: number
  bytes: number
  remaining: number
  blockedByGap: boolean
  nextPreviousSequence?: number
}

export class OrderedIngressBuffer {
  readonly #capacity: IngressCapacity
  readonly #now: () => number
  readonly #bySequence = new Map<number, BufferedFrame>()
  readonly #byEventId = new Map<string, BufferedFrame>()
  readonly #byPredecessor = new Map<number, Set<BufferedFrame>>()
  readonly #coalescible = new Map<string, BufferedFrame>()
  #items = 0
  #bytes = 0
  #coalesced = 0
  #evicted = 0
  #rejected = 0
  #disabled = false

  constructor(capacity: IngressCapacity, options: { now?: () => number } = {}) {
    this.#capacity = capacity
    this.#now = options.now ?? Date.now
  }

  get size(): number {
    return this.#items
  }

  get bytes(): number {
    return this.#bytes
  }

  disable(): void {
    this.#disabled = true
  }

  enable(): void {
    this.#disabled = false
  }

  insert(frame: IngressEventFrame, committedSequence: number): BufferInsertResult {
    this.#assertEnabled()
    if (frame.encodedBytes > this.#capacity.maxBytes) {
      this.#rejected += 1
      throw new EventIngressError(
        IngressErrorCode.EVENT_TOO_LARGE,
        "One event frame exceeds the complete ingress byte capacity.",
        {
          resyncRequired: true,
          context: {
            taskId: frame.taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
            details: {
              encodedBytes: frame.encodedBytes,
              maxBytes: this.#capacity.maxBytes,
            },
          },
        },
      )
    }
    const existingById = this.#byEventId.get(frame.eventId)
    const existingBySequence = this.#bySequence.get(frame.sequence)
    if (existingById || existingBySequence) {
      const existing = existingById ?? existingBySequence!
      this.#assertSameIdentity(existing.frame, frame)
      existing.sources.add(frame.source)
      existing.updatedAtMs = this.#now()
      return {
        disposition: DeliveryDisposition.DUPLICATE,
        receipt: this.#receipt(
          frame,
          DeliveryDisposition.DUPLICATE,
          "frame_already_buffered",
        ),
        pressure: this.snapshot(),
        evicted: Object.freeze([]),
      }
    }
    const coalesced = this.#tryCoalesce(frame)
    if (coalesced) return coalesced
    const evicted = this.#makeRoom(frame)
    const now = this.#now()
    const item: BufferedFrame = {
      frame,
      priority: eventPriority(frame.event),
      insertedAtMs: now,
      updatedAtMs: now,
      sources: new Set([frame.source]),
      coalesced: 0,
    }
    this.#put(item)
    const gap = frame.previousSequence !== committedSequence
    return {
      disposition: gap
        ? DeliveryDisposition.BUFFERED_GAP
        : DeliveryDisposition.ACCEPTED,
      receipt: this.#receipt(
        frame,
        gap ? DeliveryDisposition.BUFFERED_GAP : DeliveryDisposition.ACCEPTED,
        gap ? "waiting_for_predecessor" : "ready_for_delivery",
      ),
      pressure: this.snapshot(),
      evicted: Object.freeze(evicted),
    }
  }

  drain(
    committedSequence: number,
    options: { maxItems?: number; maxBytes?: number } = {},
  ): BufferDrainResult {
    this.#assertEnabled()
    const maxItems = Math.max(
      1,
      Math.min(
        this.#capacity.maxBatchItems,
        Math.floor(options.maxItems ?? this.#capacity.maxBatchItems),
      ),
    )
    const maxBytes = Math.max(
      1,
      Math.min(
        this.#capacity.maxBatchBytes,
        Math.floor(options.maxBytes ?? this.#capacity.maxBatchBytes),
      ),
    )
    const frames: IngressEventFrame[] = []
    let sequence = committedSequence
    let bytes = 0
    const visited = new Set<number>()
    while (frames.length < maxItems) {
      const candidates = this.#byPredecessor.get(sequence)
      if (!candidates?.size) break
      const ordered = [...candidates].sort((left, right) =>
        left.frame.sequence - right.frame.sequence ||
        right.priority - left.priority ||
        left.insertedAtMs - right.insertedAtMs ||
        left.frame.eventId.localeCompare(right.frame.eventId),
      )
      if (ordered.length > 1) {
        const sequences = new Set(ordered.map((item) => item.frame.sequence))
        if (sequences.size > 1) {
          throw new EventIngressError(
            IngressErrorCode.DUPLICATE_CONFLICT,
            "Multiple event frames claim the same predecessor.",
            {
              resyncRequired: true,
              context: {
                taskId: ordered[0]!.frame.taskId,
                generation: ordered[0]!.frame.generation,
                previousSequence: sequence,
                details: {
                  candidates: ordered.map((item) => ({
                    eventId: item.frame.eventId,
                    sequence: item.frame.sequence,
                  })),
                },
              },
            },
          )
        }
      }
      const selected = ordered[0]!
      if (visited.has(selected.frame.sequence)) {
        throw new EventIngressError(
          IngressErrorCode.INTERNAL_INVARIANT,
          "Ordered ingress buffer found a predecessor cycle.",
          {
            resyncRequired: true,
            context: {
              taskId: selected.frame.taskId,
              generation: selected.frame.generation,
              eventId: selected.frame.eventId,
              sequence: selected.frame.sequence,
            },
          },
        )
      }
      if (
        frames.length > 0 &&
        bytes + selected.frame.encodedBytes > maxBytes
      ) break
      if (selected.frame.encodedBytes > maxBytes && frames.length === 0) {
        throw new EventIngressError(
          IngressErrorCode.EVENT_TOO_LARGE,
          "Ready event exceeds the configured delivery batch limit.",
          {
            context: {
              taskId: selected.frame.taskId,
              generation: selected.frame.generation,
              eventId: selected.frame.eventId,
              sequence: selected.frame.sequence,
              details: {
                encodedBytes: selected.frame.encodedBytes,
                maxBatchBytes: maxBytes,
              },
            },
          },
        )
      }
      visited.add(selected.frame.sequence)
      frames.push(selected.frame)
      bytes += selected.frame.encodedBytes
      sequence = selected.frame.sequence
      this.#remove(selected)
    }
    const nextPrevious = this.#smallestPredecessor()
    return {
      frames: Object.freeze(frames),
      fromSequence: committedSequence,
      sequence,
      bytes,
      remaining: this.#items,
      blockedByGap:
        this.#items > 0 &&
        !this.#byPredecessor.has(sequence),
      nextPreviousSequence: nextPrevious,
    }
  }

  peekReady(committedSequence: number): IngressEventFrame | undefined {
    const candidates = this.#byPredecessor.get(committedSequence)
    if (!candidates?.size) return undefined
    return [...candidates]
      .sort((left, right) =>
        left.frame.sequence - right.frame.sequence ||
        right.priority - left.priority ||
        left.insertedAtMs - right.insertedAtMs,
      )[0]?.frame
  }

  gapAfter(committedSequence: number): {
    expectedPrevious: number
    observedPrevious: number
    observedSequence: number
    eventIds: readonly string[]
  } | undefined {
    if (!this.#items || this.#byPredecessor.has(committedSequence)) return undefined
    const candidates = [...this.#bySequence.values()]
      .filter((item) => item.frame.sequence > committedSequence)
      .sort((left, right) =>
        left.frame.sequence - right.frame.sequence ||
        left.frame.previousSequence - right.frame.previousSequence,
      )
    const first = candidates[0]
    if (!first) return undefined
    const eventIds = candidates
      .filter((item) => item.frame.previousSequence === first.frame.previousSequence)
      .map((item) => item.frame.eventId)
      .sort()
    return {
      expectedPrevious: committedSequence,
      observedPrevious: first.frame.previousSequence,
      observedSequence: first.frame.sequence,
      eventIds: Object.freeze(eventIds),
    }
  }

  discardThrough(sequence: number, reason = "cursor_advanced"): readonly DeliveryReceipt[] {
    this.#assertEnabled()
    const discarded: DeliveryReceipt[] = []
    for (const item of [...this.#bySequence.values()]) {
      if (item.frame.sequence > sequence) continue
      discarded.push(
        this.#receipt(
          item.frame,
          DeliveryDisposition.STALE,
          reason,
        ),
      )
      this.#remove(item)
    }
    return Object.freeze(discarded)
  }

  discardGeneration(generation: number, reason = "generation_replaced"): readonly DeliveryReceipt[] {
    this.#assertEnabled()
    const discarded: DeliveryReceipt[] = []
    for (const item of [...this.#bySequence.values()]) {
      if (item.frame.generation !== generation) continue
      discarded.push(
        this.#receipt(
          item.frame,
          DeliveryDisposition.STALE,
          reason,
        ),
      )
      this.#remove(item)
    }
    return Object.freeze(discarded)
  }

  reset(reason = "buffer_reset"): readonly DeliveryReceipt[] {
    const discarded = [...this.#bySequence.values()].map((item) =>
      this.#receipt(
        item.frame,
        DeliveryDisposition.EVICTED,
        reason,
      ),
    )
    this.#bySequence.clear()
    this.#byEventId.clear()
    this.#byPredecessor.clear()
    this.#coalescible.clear()
    this.#items = 0
    this.#bytes = 0
    return Object.freeze(discarded)
  }

  snapshot(): PressureSnapshot {
    const itemRatio = this.#items / this.#capacity.maxItems
    const byteRatio = this.#bytes / this.#capacity.maxBytes
    const ratio = Math.max(itemRatio, byteRatio)
    const level =
      ratio >= 1
        ? PressureLevel.OVERFLOW
        : ratio >= this.#capacity.criticalWatermarkRatio
          ? PressureLevel.CRITICAL
          : ratio >= this.#capacity.highWatermarkRatio
            ? PressureLevel.HIGH
            : ratio >= this.#capacity.highWatermarkRatio * 0.75
              ? PressureLevel.ELEVATED
              : PressureLevel.NORMAL
    return {
      level,
      items: this.#items,
      bytes: this.#bytes,
      maxItems: this.#capacity.maxItems,
      maxBytes: this.#capacity.maxBytes,
      itemRatio,
      byteRatio,
      reservedItemsAvailable: Math.max(
        0,
        this.#capacity.terminalReserveItems -
          Math.max(0, this.#items - (this.#capacity.maxItems - this.#capacity.terminalReserveItems)),
      ),
      reservedBytesAvailable: Math.max(
        0,
        this.#capacity.terminalReserveBytes -
          Math.max(0, this.#bytes - (this.#capacity.maxBytes - this.#capacity.terminalReserveBytes)),
      ),
      coalesced: this.#coalesced,
      evicted: this.#evicted,
      rejected: this.#rejected,
    }
  }

  entries(options: { limit?: number; afterSequence?: number } = {}): readonly {
    eventId: string
    sequence: number
    previousSequence: number
    priority: number
    encodedBytes: number
    insertedAtMs: number
    sources: readonly string[]
    coalesced: number
  }[] {
    const limit = Math.max(
      1,
      Math.min(this.#capacity.maxItems, Math.floor(options.limit ?? this.#capacity.maxItems)),
    )
    return [...this.#bySequence.values()]
      .filter((item) =>
        options.afterSequence === undefined ||
        item.frame.sequence > options.afterSequence,
      )
      .sort((left, right) => left.frame.sequence - right.frame.sequence)
      .slice(0, limit)
      .map((item) => ({
        eventId: item.frame.eventId,
        sequence: item.frame.sequence,
        previousSequence: item.frame.previousSequence,
        priority: item.priority,
        encodedBytes: item.frame.encodedBytes,
        insertedAtMs: item.insertedAtMs,
        sources: Object.freeze([...item.sources].sort()),
        coalesced: item.coalesced,
      }))
  }

  audit(): { ok: boolean; findings: readonly string[] } {
    const findings: string[] = []
    if (this.#items !== this.#bySequence.size) {
      findings.push("item_count_does_not_match_sequence_index")
    }
    if (this.#items !== this.#byEventId.size) {
      findings.push("item_count_does_not_match_event_index")
    }
    const computedBytes = [...this.#bySequence.values()]
      .reduce((total, item) => total + item.frame.encodedBytes, 0)
    if (computedBytes !== this.#bytes) {
      findings.push(`byte_count_mismatch:${this.#bytes}:${computedBytes}`)
    }
    if (this.#items > this.#capacity.maxItems) findings.push("item_capacity_exceeded")
    if (this.#bytes > this.#capacity.maxBytes) findings.push("byte_capacity_exceeded")
    for (const item of this.#bySequence.values()) {
      if (this.#byEventId.get(item.frame.eventId) !== item) {
        findings.push(`event_index_mismatch:${item.frame.eventId}`)
      }
      if (!this.#byPredecessor.get(item.frame.previousSequence)?.has(item)) {
        findings.push(`predecessor_index_mismatch:${item.frame.eventId}`)
      }
      const key = this.#coalescingKey(item.frame)
      if (key && this.#coalescible.get(key) !== item) {
        findings.push(`coalescing_index_mismatch:${item.frame.eventId}`)
      }
      if (findings.length >= 100) break
    }
    return { ok: findings.length === 0, findings: Object.freeze(findings) }
  }

  exportState(limit = 1024): JsonValue {
    return {
      pressure: this.snapshot() as unknown as JsonValue,
      entries: this.entries({ limit }) as unknown as JsonValue,
      audit: this.audit() as unknown as JsonValue,
      disabled: this.#disabled,
    }
  }

  #tryCoalesce(frame: IngressEventFrame): BufferInsertResult | undefined {
    const key = this.#coalescingKey(frame)
    if (!key) return undefined
    const existing = this.#coalescible.get(key)
    if (!existing) return undefined
    if (
      frame.sequence <= existing.frame.sequence ||
      frame.generation !== existing.frame.generation
    ) return undefined
    if (frame.previousSequence !== existing.frame.previousSequence) return undefined
    const delta = frame.encodedBytes - existing.frame.encodedBytes
    if (delta > 0 && !this.#canFit(frame, delta, 0)) return undefined
    const replaced = existing.frame
    this.#remove(existing)
    const now = this.#now()
    const item: BufferedFrame = {
      frame,
      priority: eventPriority(frame.event),
      insertedAtMs: existing.insertedAtMs,
      updatedAtMs: now,
      sources: new Set([...existing.sources, frame.source]),
      coalesced: existing.coalesced + 1,
    }
    this.#put(item)
    this.#coalesced += 1
    return {
      disposition: DeliveryDisposition.COALESCED,
      receipt: this.#receipt(
        frame,
        DeliveryDisposition.COALESCED,
        `replaced_non_effective_update:${replaced.eventId}`,
      ),
      pressure: this.snapshot(),
      evicted: Object.freeze([
        this.#receipt(
          replaced,
          DeliveryDisposition.EVICTED,
          `coalesced_into:${frame.eventId}`,
        ),
      ]),
    }
  }

  #makeRoom(frame: IngressEventFrame): DeliveryReceipt[] {
    const evicted: DeliveryReceipt[] = []
    while (!this.#canFit(frame, frame.encodedBytes, 1)) {
      const candidate = this.#evictionCandidate(frame)
      if (!candidate) {
        this.#rejected += 1
        throw bufferOverflowError(
          "Bounded event ingress buffer cannot admit the frame without dropping protected state.",
          {
            taskId: frame.taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
            details: {
              pressure: this.snapshot() as unknown as JsonValue,
              incomingBytes: frame.encodedBytes,
              incomingPriority: eventPriority(frame.event),
            },
          },
        )
      }
      evicted.push(
        this.#receipt(
          candidate.frame,
          DeliveryDisposition.EVICTED,
          `admit_higher_priority:${frame.eventId}`,
        ),
      )
      this.#remove(candidate)
      this.#evicted += 1
    }
    return evicted
  }

  #canFit(frame: IngressEventFrame, addedBytes: number, addedItems: number): boolean {
    const priority = eventPriority(frame.event)
    const protectedEvent = priority >= BufferPriority.EFFECTIVE
    const itemLimit = protectedEvent
      ? this.#capacity.maxItems
      : this.#capacity.maxItems - this.#capacity.terminalReserveItems
    const byteLimit = protectedEvent
      ? this.#capacity.maxBytes
      : this.#capacity.maxBytes - this.#capacity.terminalReserveBytes
    return (
      this.#items + addedItems <= itemLimit &&
      this.#bytes + addedBytes <= byteLimit
    )
  }

  #evictionCandidate(incoming: IngressEventFrame): BufferedFrame | undefined {
    const incomingPriority = eventPriority(incoming.event)
    return [...this.#bySequence.values()]
      .filter((item) => item.priority < incomingPriority)
      .filter((item) => item.priority < BufferPriority.TERMINAL)
      .sort((left, right) =>
        left.priority - right.priority ||
        left.updatedAtMs - right.updatedAtMs ||
        left.frame.sequence - right.frame.sequence,
      )[0]
  }

  #put(item: BufferedFrame): void {
    this.#bySequence.set(item.frame.sequence, item)
    this.#byEventId.set(item.frame.eventId, item)
    const predecessors = this.#byPredecessor.get(item.frame.previousSequence) ?? new Set()
    predecessors.add(item)
    this.#byPredecessor.set(item.frame.previousSequence, predecessors)
    const coalescing = this.#coalescingKey(item.frame)
    if (coalescing) this.#coalescible.set(coalescing, item)
    this.#items += 1
    this.#bytes += item.frame.encodedBytes
  }

  #remove(item: BufferedFrame): void {
    if (this.#bySequence.get(item.frame.sequence) !== item) return
    this.#bySequence.delete(item.frame.sequence)
    if (this.#byEventId.get(item.frame.eventId) === item) {
      this.#byEventId.delete(item.frame.eventId)
    }
    const predecessors = this.#byPredecessor.get(item.frame.previousSequence)
    predecessors?.delete(item)
    if (!predecessors?.size) this.#byPredecessor.delete(item.frame.previousSequence)
    const coalescing = this.#coalescingKey(item.frame)
    if (coalescing && this.#coalescible.get(coalescing) === item) {
      this.#coalescible.delete(coalescing)
    }
    this.#items -= 1
    this.#bytes -= item.frame.encodedBytes
    if (this.#items < 0 || this.#bytes < 0) {
      throw new EventIngressError(
        IngressErrorCode.INTERNAL_INVARIANT,
        "Ordered ingress buffer accounting underflowed.",
        { resyncRequired: true },
      )
    }
  }

  #coalescingKey(frame: IngressEventFrame): string | undefined {
    if (
      frame.event.effective ||
      frame.event.terminal ||
      frame.event.settlement === "final" ||
      frame.event.settlement === "tombstone"
    ) return undefined
    return eventPartKey(frame.event)
  }

  #smallestPredecessor(): number | undefined {
    let minimum: number | undefined
    for (const predecessor of this.#byPredecessor.keys()) {
      minimum = minimum === undefined ? predecessor : Math.min(minimum, predecessor)
    }
    return minimum
  }

  #assertSameIdentity(left: IngressEventFrame, right: IngressEventFrame): void {
    if (
      left.eventId === right.eventId &&
      left.sequence === right.sequence &&
      left.event.contentDigest === right.event.contentDigest
    ) return
    throw new EventIngressError(
      IngressErrorCode.DUPLICATE_CONFLICT,
      "Buffered event identity conflicts with an existing frame.",
      {
        resyncRequired: true,
        context: {
          taskId: right.taskId,
          generation: right.generation,
          eventId: right.eventId,
          sequence: right.sequence,
          details: {
            knownEventId: left.eventId,
            knownSequence: left.sequence,
            knownDigest: left.event.contentDigest,
            observedDigest: right.event.contentDigest,
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
      "Ordered event ingress buffer is disabled.",
    )
  }
}
