import {
  DeliveryDisposition,
  EVENT_INGRESS_SNAPSHOT_SCHEMA,
  type DeliveryReceipt,
  type IngressCapacity,
  type IngressEventFrame,
  type IngressPage,
  type JsonValue,
} from "./contracts.ts"
import {
  EventIngressError,
  IngressErrorCode,
} from "./errors.ts"

interface RetainedFrame {
  frame: IngressEventFrame
  sources: Set<string>
  insertedAtMs: number
  updatedAtMs: number
}

export interface SnapshotBarrierMerge {
  frames: readonly IngressEventFrame[]
  receipts: readonly DeliveryReceipt[]
  cursor: string
  sequence: number
  boundary: number
  complete: boolean
  hasMore: boolean
  interleaved: number
  duplicates: number
  staleSnapshotFrames: number
}

export class SubscribeBeforeSnapshotBarrier {
  readonly #taskId: string
  readonly #capacity: IngressCapacity
  readonly #now: () => number
  readonly #liveBySequence = new Map<number, RetainedFrame>()
  readonly #liveById = new Map<string, RetainedFrame>()
  #generation = 0
  #subscribed = false
  #snapshotStarted = false
  #snapshotComplete = false
  #snapshotBoundary = 0
  #snapshotCursor: string | undefined
  #snapshotId: string | undefined
  #liveBytes = 0
  #interleaved = 0
  #duplicates = 0
  #staleSnapshotFrames = 0
  #disabled = false

  constructor(
    taskId: string,
    capacity: IngressCapacity,
    options: { now?: () => number } = {},
  ) {
    this.#taskId = String(taskId || "").trim()
    if (!this.#taskId) throw new TypeError("Snapshot barrier taskId must not be empty")
    this.#capacity = capacity
    this.#now = options.now ?? Date.now
  }

  get generation(): number {
    return this.#generation
  }

  get subscribed(): boolean {
    return this.#subscribed
  }

  get snapshotComplete(): boolean {
    return this.#snapshotComplete
  }

  get boundary(): number {
    return this.#snapshotBoundary
  }

  disable(): void {
    this.#disabled = true
  }

  enable(): void {
    this.#disabled = false
  }

  beginGeneration(generation: number): void {
    this.#assertEnabled()
    if (!Number.isSafeInteger(generation) || generation < 1) {
      throw new TypeError("Snapshot barrier generation must be a positive integer")
    }
    this.reset("generation_replaced")
    this.#generation = generation
  }

  markSubscribed(generation: number): void {
    this.#assertEnabled()
    this.#assertGeneration(generation)
    if (this.#snapshotStarted) {
      throw new EventIngressError(
        IngressErrorCode.INTERNAL_INVARIANT,
        "Live event subscription must start before the snapshot request.",
        {
          resyncRequired: true,
          context: { taskId: this.#taskId, generation },
        },
      )
    }
    this.#subscribed = true
  }

  beginSnapshot(generation: number): void {
    this.#assertEnabled()
    this.#assertGeneration(generation)
    if (!this.#subscribed) {
      throw new EventIngressError(
        IngressErrorCode.INTERNAL_INVARIANT,
        "Snapshot started before the live subscription was active.",
        {
          resyncRequired: true,
          context: { taskId: this.#taskId, generation },
        },
      )
    }
    if (this.#snapshotStarted && !this.#snapshotComplete) {
      throw new EventIngressError(
        IngressErrorCode.SNAPSHOT_FAILED,
        "A snapshot request is already active for this generation.",
        {
          retryable: true,
          context: { taskId: this.#taskId, generation },
        },
      )
    }
    this.#snapshotStarted = true
    this.#snapshotComplete = false
  }

  retainLive(frame: IngressEventFrame): DeliveryReceipt {
    this.#assertEnabled()
    this.#assertGeneration(frame.generation)
    if (!this.#subscribed) {
      throw new EventIngressError(
        IngressErrorCode.INTERNAL_INVARIANT,
        "Cannot retain a live frame before subscription.",
        {
          context: {
            taskId: this.#taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
          },
        },
      )
    }
    if (frame.taskId !== this.#taskId) {
      throw new EventIngressError(
        IngressErrorCode.CROSS_TASK,
        "Snapshot barrier rejected a live frame for another task.",
        {
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
            details: { actualTaskId: frame.taskId },
          },
        },
      )
    }
    const byId = this.#liveById.get(frame.eventId)
    const bySequence = this.#liveBySequence.get(frame.sequence)
    if (byId || bySequence) {
      const existing = byId ?? bySequence!
      this.#assertCompatible(existing.frame, frame)
      existing.sources.add(frame.source)
      existing.updatedAtMs = this.#now()
      this.#duplicates += 1
      return this.#receipt(
        frame,
        DeliveryDisposition.DUPLICATE,
        "live_frame_already_retained",
      )
    }
    if (
      this.#liveBySequence.size >= this.#capacity.maxGapItems ||
      this.#liveBytes + frame.encodedBytes > this.#capacity.maxGapBytes
    ) {
      throw new EventIngressError(
        IngressErrorCode.BUFFER_OVERFLOW,
        "Subscribe-before-snapshot live retention exceeded its bounded capacity.",
        {
          retryable: true,
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
            details: {
              retainedItems: this.#liveBySequence.size,
              retainedBytes: this.#liveBytes,
              maxItems: this.#capacity.maxGapItems,
              maxBytes: this.#capacity.maxGapBytes,
            },
          },
        },
      )
    }
    const now = this.#now()
    const retained: RetainedFrame = {
      frame,
      sources: new Set([frame.source]),
      insertedAtMs: now,
      updatedAtMs: now,
    }
    this.#liveBySequence.set(frame.sequence, retained)
    this.#liveById.set(frame.eventId, retained)
    this.#liveBytes += frame.encodedBytes
    if (this.#snapshotStarted && !this.#snapshotComplete) this.#interleaved += 1
    return this.#receipt(
      frame,
      DeliveryDisposition.ACCEPTED,
      this.#snapshotStarted
        ? "retained_while_snapshot_inflight"
        : "retained_before_snapshot_request",
    )
  }

  mergeSnapshot(page: IngressPage, committedSequence: number): SnapshotBarrierMerge {
    this.#assertEnabled()
    this.#assertGeneration(page.generation)
    if (!this.#subscribed || !this.#snapshotStarted) {
      throw new EventIngressError(
        IngressErrorCode.INTERNAL_INVARIANT,
        "Snapshot page arrived without an active subscribe-before-snapshot barrier.",
        {
          resyncRequired: true,
          context: { taskId: this.#taskId, generation: page.generation },
        },
      )
    }
    if (page.schema !== EVENT_INGRESS_SNAPSHOT_SCHEMA) {
      throw new EventIngressError(
        IngressErrorCode.INVALID_PAGE,
        "Snapshot barrier received a non-snapshot page.",
        {
          resyncRequired: true,
          context: { taskId: this.#taskId, generation: page.generation },
        },
      )
    }
    const boundary = page.boundary
    if (boundary === undefined) {
      throw new EventIngressError(
        IngressErrorCode.INVALID_PAGE,
        "Snapshot page omitted its captured boundary.",
        {
          resyncRequired: true,
          context: { taskId: this.#taskId, generation: page.generation },
        },
      )
    }
    if (this.#snapshotBoundary && this.#snapshotBoundary !== boundary) {
      throw new EventIngressError(
        IngressErrorCode.SNAPSHOT_STALE,
        "Snapshot boundary changed between pages.",
        {
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: page.generation,
            sequence: boundary,
            details: { expectedBoundary: this.#snapshotBoundary },
          },
        },
      )
    }
    if (boundary < committedSequence) {
      throw new EventIngressError(
        IngressErrorCode.SNAPSHOT_STALE,
        "Snapshot boundary is older than the committed live cursor.",
        {
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: page.generation,
            sequence: boundary,
            previousSequence: committedSequence,
          },
        },
      )
    }
    if (this.#snapshotId && page.snapshotId !== this.#snapshotId) {
      throw new EventIngressError(
        IngressErrorCode.SNAPSHOT_STALE,
        "Snapshot identity changed between pages.",
        {
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: page.generation,
            details: {
              expectedSnapshotId: this.#snapshotId,
              actualSnapshotId: page.snapshotId ?? null,
            },
          },
        },
      )
    }
    this.#snapshotBoundary = boundary
    this.#snapshotCursor = page.cursor
    this.#snapshotId = page.snapshotId
    const merged = new Map<number, IngressEventFrame>()
    const identities = new Map<string, IngressEventFrame>()
    const receipts: DeliveryReceipt[] = []
    let duplicates = 0
    let staleSnapshotFrames = 0
    const accept = (frame: IngressEventFrame, origin: "snapshot" | "live") => {
      if (frame.sequence <= committedSequence) {
        if (origin === "snapshot") {
          staleSnapshotFrames += 1
          this.#staleSnapshotFrames += 1
        }
        receipts.push(
          this.#receipt(
            frame,
            DeliveryDisposition.STALE,
            `${origin}_frame_precedes_committed_cursor`,
          ),
        )
        return
      }
      const bySequence = merged.get(frame.sequence)
      const byId = identities.get(frame.eventId)
      const existing = bySequence ?? byId
      if (existing) {
        this.#assertCompatible(existing, frame)
        duplicates += 1
        receipts.push(
          this.#receipt(
            frame,
            DeliveryDisposition.DUPLICATE,
            `${origin}_frame_matches_interleaved_identity`,
          ),
        )
        if (origin === "live") {
          merged.set(frame.sequence, frame)
          identities.set(frame.eventId, frame)
        }
        return
      }
      merged.set(frame.sequence, frame)
      identities.set(frame.eventId, frame)
      receipts.push(
        this.#receipt(
          frame,
          DeliveryDisposition.ACCEPTED,
          `${origin}_frame_merged`,
        ),
      )
    }
    for (const frame of page.frames) accept(frame, "snapshot")
    for (const retained of this.#liveBySequence.values()) {
      accept(retained.frame, "live")
    }
    const ordered = [...merged.values()].sort((left, right) =>
      left.sequence - right.sequence ||
      left.eventId.localeCompare(right.eventId),
    )
    const ready: IngressEventFrame[] = []
    const consumed = new Set<number>()
    let predecessor = committedSequence
    while (true) {
      const candidates = ordered.filter(
        (frame) =>
          !consumed.has(frame.sequence) &&
          frame.sequence <= boundary &&
          frame.previousSequence === predecessor,
      )
      if (!candidates.length) break
      if (candidates.length > 1) {
        throw new EventIngressError(
          IngressErrorCode.DUPLICATE_CONFLICT,
          "Merged snapshot/live streams contain multiple successors for one cursor.",
          {
            resyncRequired: true,
            context: {
              taskId: this.#taskId,
              generation: this.#generation,
              previousSequence: predecessor,
              details: {
                candidates: candidates.map((frame) => ({
                  eventId: frame.eventId,
                  sequence: frame.sequence,
                })),
              },
            },
          },
        )
      }
      const next = candidates[0]!
      ready.push(next)
      consumed.add(next.sequence)
      predecessor = next.sequence
    }
    for (const pending of ordered) {
      if (consumed.has(pending.sequence) || this.#liveById.has(pending.eventId)) continue
      this.retainLive(pending)
    }
    this.#assertPredecessorChain(ready, committedSequence)
    this.#snapshotComplete = page.complete === true
    if (this.#snapshotComplete) {
      for (const item of [...this.#liveBySequence.values()]) {
        if (item.frame.sequence <= boundary) this.#removeLive(item)
      }
    }
    return {
      frames: Object.freeze(ready),
      receipts: Object.freeze(receipts),
      cursor: page.cursor,
      sequence: page.nextSequence,
      boundary,
      complete: this.#snapshotComplete,
      hasMore: page.hasMore,
      interleaved: this.#interleaved,
      duplicates,
      staleSnapshotFrames,
    }
  }

  releaseAfterBoundary(): readonly IngressEventFrame[] {
    this.#assertEnabled()
    if (!this.#snapshotComplete) {
      throw new EventIngressError(
        IngressErrorCode.SNAPSHOT_INCOMPLETE,
        "Cannot release post-snapshot live frames before snapshot completion.",
        {
          retryable: true,
          context: { taskId: this.#taskId, generation: this.#generation },
        },
      )
    }
    const frames = [...this.#liveBySequence.values()]
      .filter((item) => item.frame.sequence > this.#snapshotBoundary)
      .sort((left, right) =>
        left.frame.sequence - right.frame.sequence ||
        left.frame.eventId.localeCompare(right.frame.eventId),
      )
      .map((item) => item.frame)
    for (const frame of frames) {
      const retained = this.#liveBySequence.get(frame.sequence)
      if (retained) this.#removeLive(retained)
    }
    return Object.freeze(frames)
  }

  discardThrough(sequence: number): number {
    let discarded = 0
    for (const item of [...this.#liveBySequence.values()]) {
      if (item.frame.sequence > sequence) continue
      this.#removeLive(item)
      discarded += 1
    }
    return discarded
  }

  reset(reason = "snapshot_barrier_reset"): readonly DeliveryReceipt[] {
    const receipts = [...this.#liveBySequence.values()].map((item) =>
      this.#receipt(
        item.frame,
        DeliveryDisposition.EVICTED,
        reason,
      ),
    )
    this.#liveBySequence.clear()
    this.#liveById.clear()
    this.#subscribed = false
    this.#snapshotStarted = false
    this.#snapshotComplete = false
    this.#snapshotBoundary = 0
    this.#snapshotCursor = undefined
    this.#snapshotId = undefined
    this.#liveBytes = 0
    this.#interleaved = 0
    return Object.freeze(receipts)
  }

  snapshot(): {
    taskId: string
    generation: number
    subscribed: boolean
    snapshotStarted: boolean
    snapshotComplete: boolean
    snapshotBoundary: number
    snapshotCursor?: string
    snapshotId?: string
    retainedLiveItems: number
    retainedLiveBytes: number
    interleaved: number
    duplicates: number
    staleSnapshotFrames: number
  } {
    return {
      taskId: this.#taskId,
      generation: this.#generation,
      subscribed: this.#subscribed,
      snapshotStarted: this.#snapshotStarted,
      snapshotComplete: this.#snapshotComplete,
      snapshotBoundary: this.#snapshotBoundary,
      snapshotCursor: this.#snapshotCursor,
      snapshotId: this.#snapshotId,
      retainedLiveItems: this.#liveBySequence.size,
      retainedLiveBytes: this.#liveBytes,
      interleaved: this.#interleaved,
      duplicates: this.#duplicates,
      staleSnapshotFrames: this.#staleSnapshotFrames,
    }
  }

  audit(): { ok: boolean; findings: readonly string[] } {
    const findings: string[] = []
    if (this.#liveBySequence.size !== this.#liveById.size) {
      findings.push("live_sequence_and_identity_index_size_mismatch")
    }
    const bytes = [...this.#liveBySequence.values()]
      .reduce((total, item) => total + item.frame.encodedBytes, 0)
    if (bytes !== this.#liveBytes) {
      findings.push(`live_byte_count_mismatch:${this.#liveBytes}:${bytes}`)
    }
    if (this.#snapshotStarted && !this.#subscribed) {
      findings.push("snapshot_started_without_subscription")
    }
    if (this.#snapshotComplete && !this.#snapshotStarted) {
      findings.push("snapshot_complete_without_start")
    }
    for (const item of this.#liveBySequence.values()) {
      if (this.#liveById.get(item.frame.eventId) !== item) {
        findings.push(`live_identity_index_mismatch:${item.frame.eventId}`)
      }
      if (item.frame.generation !== this.#generation) {
        findings.push(`live_generation_mismatch:${item.frame.eventId}`)
      }
      if (findings.length >= 100) break
    }
    return { ok: findings.length === 0, findings: Object.freeze(findings) }
  }

  exportState(limit = 1024): JsonValue {
    const retained = [...this.#liveBySequence.values()]
      .sort((left, right) => left.frame.sequence - right.frame.sequence)
      .slice(0, limit)
      .map((item) => ({
        eventId: item.frame.eventId,
        sequence: item.frame.sequence,
        previousSequence: item.frame.previousSequence,
        sources: [...item.sources].sort(),
        insertedAtMs: item.insertedAtMs,
        updatedAtMs: item.updatedAtMs,
      }))
    return {
      snapshot: this.snapshot() as unknown as JsonValue,
      retained: retained as unknown as JsonValue,
      audit: this.audit() as unknown as JsonValue,
      disabled: this.#disabled,
    }
  }

  #removeLive(item: RetainedFrame): void {
    if (this.#liveBySequence.get(item.frame.sequence) !== item) return
    this.#liveBySequence.delete(item.frame.sequence)
    if (this.#liveById.get(item.frame.eventId) === item) {
      this.#liveById.delete(item.frame.eventId)
    }
    this.#liveBytes -= item.frame.encodedBytes
    if (this.#liveBytes < 0) {
      throw new EventIngressError(
        IngressErrorCode.INTERNAL_INVARIANT,
        "Snapshot barrier byte accounting underflowed.",
        { resyncRequired: true },
      )
    }
  }

  #assertPredecessorChain(
    frames: readonly IngressEventFrame[],
    committedSequence: number,
  ): void {
    let previous = committedSequence
    for (const frame of frames) {
      if (frame.previousSequence !== previous) {
        throw new EventIngressError(
          IngressErrorCode.GAP_DETECTED,
          "Merged snapshot/live frames do not form a canonical predecessor chain.",
          {
            retryable: true,
            context: {
              taskId: this.#taskId,
              generation: this.#generation,
              eventId: frame.eventId,
              sequence: frame.sequence,
              previousSequence: frame.previousSequence,
              details: { expectedPrevious: previous },
            },
          },
        )
      }
      previous = frame.sequence
    }
  }

  #assertCompatible(left: IngressEventFrame, right: IngressEventFrame): void {
    if (
      left.eventId === right.eventId &&
      left.sequence === right.sequence &&
      left.event.contentDigest === right.event.contentDigest
    ) return
    throw new EventIngressError(
      IngressErrorCode.DUPLICATE_CONFLICT,
      "Snapshot and live streams disagree about a canonical event identity.",
      {
        resyncRequired: true,
        context: {
          taskId: this.#taskId,
          generation: this.#generation,
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

  #assertGeneration(generation: number): void {
    if (generation === this.#generation) return
    throw new EventIngressError(
      IngressErrorCode.STALE_GENERATION,
      "Snapshot barrier rejected a stale generation.",
      {
        context: {
          taskId: this.#taskId,
          generation,
          details: { activeGeneration: this.#generation },
        },
      },
    )
  }

  #assertEnabled(): void {
    if (!this.#disabled) return
    throw new EventIngressError(
      IngressErrorCode.DISABLED,
      "Subscribe-before-snapshot barrier is disabled.",
      { context: { taskId: this.#taskId, generation: this.#generation } },
    )
  }
}
