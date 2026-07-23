import {
  type GapRange,
  type IngressCapacity,
  type IngressEventFrame,
  type IngressPage,
  type JsonValue,
} from "./contracts.ts"
import {
  EventIngressError,
  IngressErrorCode,
} from "./errors.ts"

interface GapRecord {
  expectedPrevious: number
  observedPrevious: number
  observedSequence: number
  firstObservedAtMs: number
  lastObservedAtMs: number
  attempts: number
  eventIds: Set<string>
  frames: Map<number, IngressEventFrame>
  bytes: number
  status: "open" | "healing" | "resolved" | "failed"
}

export interface GapObservation {
  gap: GapRange
  created: boolean
  expanded: boolean
  overflow: boolean
}

export interface GapHealingResult {
  resolved: boolean
  frames: readonly IngressEventFrame[]
  sequence: number
  remaining?: GapRange
}

export class EventGapRecovery {
  readonly #taskId: string
  readonly #capacity: IngressCapacity
  readonly #now: () => number
  #generation = 0
  #gap: GapRecord | undefined
  #resolved = 0
  #failed = 0
  #disabled = false

  constructor(
    taskId: string,
    capacity: IngressCapacity,
    options: { now?: () => number } = {},
  ) {
    this.#taskId = String(taskId || "").trim()
    if (!this.#taskId) throw new TypeError("Gap recovery taskId must not be empty")
    this.#capacity = capacity
    this.#now = options.now ?? Date.now
  }

  beginGeneration(generation: number): void {
    this.#assertEnabled()
    if (!Number.isSafeInteger(generation) || generation < 1) {
      throw new TypeError("Gap recovery generation must be a positive integer")
    }
    this.#generation = generation
    this.#gap = undefined
  }

  disable(): void {
    this.#disabled = true
  }

  enable(): void {
    this.#disabled = false
  }

  observe(frame: IngressEventFrame, committedSequence: number): GapObservation | undefined {
    this.#assertEnabled()
    this.#assertFrame(frame)
    if (frame.sequence <= committedSequence) return undefined
    if (frame.previousSequence === committedSequence) return undefined
    const now = this.#now()
    const created = !this.#gap
    if (!this.#gap) {
      this.#gap = {
        expectedPrevious: committedSequence,
        observedPrevious: frame.previousSequence,
        observedSequence: frame.sequence,
        firstObservedAtMs: now,
        lastObservedAtMs: now,
        attempts: 0,
        eventIds: new Set(),
        frames: new Map(),
        bytes: 0,
        status: "open",
      }
    }
    const gap = this.#gap
    if (gap.expectedPrevious !== committedSequence) {
      if (committedSequence > gap.expectedPrevious) {
        gap.expectedPrevious = committedSequence
      } else {
        throw new EventIngressError(
          IngressErrorCode.CURSOR_REGRESSION,
          "Gap recovery observed a regressed committed cursor.",
          {
            resyncRequired: true,
            context: {
              taskId: this.#taskId,
              generation: this.#generation,
              sequence: committedSequence,
              previousSequence: gap.expectedPrevious,
            },
          },
        )
      }
    }
    const existing = gap.frames.get(frame.sequence)
    if (existing) {
      if (
        existing.eventId !== frame.eventId ||
        existing.event.contentDigest !== frame.event.contentDigest
      ) {
        throw new EventIngressError(
          IngressErrorCode.DUPLICATE_CONFLICT,
          "Gap buffer contains conflicting events for one sequence.",
          {
            resyncRequired: true,
            context: {
              taskId: this.#taskId,
              generation: this.#generation,
              eventId: frame.eventId,
              sequence: frame.sequence,
              details: {
                knownEventId: existing.eventId,
                knownDigest: existing.event.contentDigest,
                observedDigest: frame.event.contentDigest,
              },
            },
          },
        )
      }
      gap.lastObservedAtMs = now
      return {
        gap: this.snapshot()!,
        created,
        expanded: false,
        overflow: false,
      }
    }
    gap.frames.set(frame.sequence, frame)
    gap.eventIds.add(frame.eventId)
    gap.bytes += frame.encodedBytes
    gap.observedPrevious = Math.min(gap.observedPrevious, frame.previousSequence)
    gap.observedSequence = Math.min(gap.observedSequence, frame.sequence)
    gap.lastObservedAtMs = now
    const overflow =
      gap.frames.size > this.#capacity.maxGapItems ||
      gap.bytes > this.#capacity.maxGapBytes
    if (overflow) {
      gap.status = "failed"
      this.#failed += 1
      throw new EventIngressError(
        IngressErrorCode.GAP_OVERFLOW,
        "Gap recovery buffer exceeded its bounded capacity.",
        {
          retryable: true,
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: this.#generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
            previousSequence: frame.previousSequence,
            details: {
              gapItems: gap.frames.size,
              gapBytes: gap.bytes,
              maxGapItems: this.#capacity.maxGapItems,
              maxGapBytes: this.#capacity.maxGapBytes,
            },
          },
        },
      )
    }
    return {
      gap: this.snapshot()!,
      created,
      expanded: true,
      overflow: false,
    }
  }

  beginHealing(): GapRange | undefined {
    this.#assertEnabled()
    if (!this.#gap) return undefined
    this.#gap.status = "healing"
    this.#gap.attempts += 1
    this.#gap.lastObservedAtMs = this.#now()
    return this.snapshot()
  }

  applyPage(page: IngressPage, committedSequence: number): GapHealingResult {
    this.#assertEnabled()
    if (page.taskId !== this.#taskId || page.generation !== this.#generation) {
      throw new EventIngressError(
        IngressErrorCode.STALE_GENERATION,
        "Gap recovery page belongs to another task or generation.",
        {
          context: {
            taskId: this.#taskId,
            generation: page.generation,
            details: {
              actualTaskId: page.taskId,
              activeGeneration: this.#generation,
            },
          },
        },
      )
    }
    const gap = this.#gap
    if (!gap) {
      return {
        resolved: true,
        frames: Object.freeze([...page.frames]),
        sequence: page.nextSequence,
      }
    }
    if (page.fromSequence !== committedSequence) {
      throw new EventIngressError(
        IngressErrorCode.GAP_UNRECOVERABLE,
        "Gap catch-up page does not start at the committed cursor.",
        {
          retryable: true,
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: this.#generation,
            sequence: page.fromSequence,
            previousSequence: committedSequence,
          },
        },
      )
    }
    for (const frame of page.frames) {
      const existing = gap.frames.get(frame.sequence)
      if (
        existing &&
        (
          existing.eventId !== frame.eventId ||
          existing.event.contentDigest !== frame.event.contentDigest
        )
      ) {
        throw new EventIngressError(
          IngressErrorCode.DUPLICATE_CONFLICT,
          "Catch-up page conflicts with a buffered gap event.",
          {
            resyncRequired: true,
            context: {
              taskId: this.#taskId,
              generation: this.#generation,
              eventId: frame.eventId,
              sequence: frame.sequence,
              details: {
                knownEventId: existing.eventId,
                knownDigest: existing.event.contentDigest,
                observedDigest: frame.event.contentDigest,
              },
            },
          },
        )
      }
      if (!existing) {
        gap.frames.set(frame.sequence, frame)
        gap.eventIds.add(frame.eventId)
        gap.bytes += frame.encodedBytes
      }
    }
    const ordered = [...gap.frames.values()].sort((left, right) =>
      left.sequence - right.sequence ||
      left.eventId.localeCompare(right.eventId),
    )
    const deliver: IngressEventFrame[] = []
    let sequence = committedSequence
    const consumed = new Set<number>()
    while (true) {
      const candidates = ordered.filter((frame) =>
        !consumed.has(frame.sequence) &&
        frame.previousSequence === sequence,
      )
      if (!candidates.length) break
      if (candidates.length > 1) {
        throw new EventIngressError(
          IngressErrorCode.DUPLICATE_CONFLICT,
          "Gap recovery found multiple successors for one cursor.",
          {
            resyncRequired: true,
            context: {
              taskId: this.#taskId,
              generation: this.#generation,
              previousSequence: sequence,
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
      deliver.push(next)
      consumed.add(next.sequence)
      sequence = next.sequence
    }
    const gapResolved =
      consumed.size === gap.frames.size &&
      (
        page.caughtUp === true ||
        page.hasMore === false ||
        sequence >= gap.observedSequence
      )
    if (gapResolved) {
      gap.status = "resolved"
      this.#resolved += 1
      this.#gap = undefined
      return {
        resolved: true,
        frames: Object.freeze(deliver),
        sequence: Math.max(sequence, page.nextSequence),
      }
    }
    for (const consumedSequence of consumed) {
      const frame = gap.frames.get(consumedSequence)
      if (frame) gap.bytes -= frame.encodedBytes
      gap.frames.delete(consumedSequence)
    }
    gap.expectedPrevious = sequence
    gap.status = "open"
    gap.lastObservedAtMs = this.#now()
    return {
      resolved: false,
      frames: Object.freeze(deliver),
      sequence,
      remaining: this.snapshot(),
    }
  }

  resolveThrough(sequence: number): GapRange | undefined {
    this.#assertEnabled()
    const gap = this.#gap
    if (!gap) return undefined
    for (const [itemSequence, frame] of [...gap.frames]) {
      if (itemSequence > sequence) continue
      gap.frames.delete(itemSequence)
      gap.eventIds.delete(frame.eventId)
      gap.bytes -= frame.encodedBytes
    }
    gap.expectedPrevious = Math.max(gap.expectedPrevious, sequence)
    if (!gap.frames.size || sequence >= gap.observedSequence) {
      gap.status = "resolved"
      this.#resolved += 1
      this.#gap = undefined
      return undefined
    }
    return this.snapshot()
  }

  fail(reason: string): EventIngressError | undefined {
    const gap = this.#gap
    if (!gap) return undefined
    gap.status = "failed"
    this.#failed += 1
    return new EventIngressError(
      IngressErrorCode.GAP_UNRECOVERABLE,
      reason,
      {
        retryable: true,
        resyncRequired: true,
        context: {
          taskId: this.#taskId,
          generation: this.#generation,
          sequence: gap.observedSequence,
          previousSequence: gap.observedPrevious,
          details: {
            expectedPrevious: gap.expectedPrevious,
            attempts: gap.attempts,
            eventIds: [...gap.eventIds].sort(),
          },
        },
      },
    )
  }

  clear(): void {
    this.#gap = undefined
  }

  snapshot(): GapRange | undefined {
    const gap = this.#gap
    if (!gap) return undefined
    return {
      expectedPrevious: gap.expectedPrevious,
      observedPrevious: gap.observedPrevious,
      observedSequence: gap.observedSequence,
      firstObservedAtMs: gap.firstObservedAtMs,
      lastObservedAtMs: gap.lastObservedAtMs,
      attempts: gap.attempts,
      eventIds: Object.freeze([...gap.eventIds].sort()),
    }
  }

  diagnostics(): {
    generation: number
    open: boolean
    items: number
    bytes: number
    status?: string
    resolved: number
    failed: number
    gap?: GapRange
  } {
    return {
      generation: this.#generation,
      open: Boolean(this.#gap),
      items: this.#gap?.frames.size ?? 0,
      bytes: this.#gap?.bytes ?? 0,
      status: this.#gap?.status,
      resolved: this.#resolved,
      failed: this.#failed,
      gap: this.snapshot(),
    }
  }

  audit(): { ok: boolean; findings: readonly string[] } {
    const findings: string[] = []
    const gap = this.#gap
    if (gap) {
      const bytes = [...gap.frames.values()]
        .reduce((total, frame) => total + frame.encodedBytes, 0)
      if (bytes !== gap.bytes) findings.push(`gap_byte_count_mismatch:${gap.bytes}:${bytes}`)
      if (gap.frames.size > this.#capacity.maxGapItems) findings.push("gap_item_capacity_exceeded")
      if (gap.bytes > this.#capacity.maxGapBytes) findings.push("gap_byte_capacity_exceeded")
      for (const frame of gap.frames.values()) {
        if (!gap.eventIds.has(frame.eventId)) {
          findings.push(`gap_identity_index_mismatch:${frame.eventId}`)
        }
        if (frame.generation !== this.#generation) {
          findings.push(`gap_generation_mismatch:${frame.eventId}`)
        }
        if (findings.length >= 100) break
      }
    }
    return { ok: findings.length === 0, findings: Object.freeze(findings) }
  }

  exportState(): JsonValue {
    return {
      diagnostics: this.diagnostics() as unknown as JsonValue,
      audit: this.audit() as unknown as JsonValue,
      disabled: this.#disabled,
    }
  }

  #assertFrame(frame: IngressEventFrame): void {
    if (frame.taskId !== this.#taskId || frame.generation !== this.#generation) {
      throw new EventIngressError(
        IngressErrorCode.STALE_GENERATION,
        "Gap recovery rejected a frame for another task or generation.",
        {
          context: {
            taskId: this.#taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
            details: {
              actualTaskId: frame.taskId,
              activeGeneration: this.#generation,
            },
          },
        },
      )
    }
  }

  #assertEnabled(): void {
    if (!this.#disabled) return
    throw new EventIngressError(
      IngressErrorCode.DISABLED,
      "Event gap recovery is disabled.",
      { context: { taskId: this.#taskId, generation: this.#generation } },
    )
  }
}
