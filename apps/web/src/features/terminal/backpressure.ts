import type {
  TerminalBackpressureFrame,
  TerminalOutputFrame,
  TerminalResyncFrame,
  TerminalSpillProjection,
} from "./contracts.ts"

export interface TerminalReplayState {
  cursor: number
  acknowledgedCursor: number
  earliestCursor: number
  expectedSequence: number
  paused: boolean
  unackedBytes: number
  maximumUnackedBytes: number
  gap?: { expected: number; received: number }
  spills: readonly TerminalSpillProjection[]
}

export interface TerminalOutputAdmission {
  accepted: boolean
  duplicate: boolean
  needsResync: boolean
  reason?:
    | "sequence_gap"
    | "cursor_gap"
    | "cursor_overlap"
    | "acknowledgement_exhausted"
    | "paused"
  frame?: TerminalOutputFrame
  state: TerminalReplayState
}

export class TerminalReplayWindow {
  #cursor: number
  #acknowledgedCursor: number
  #earliestCursor: number
  #expectedSequence: number
  #paused = false
  #maximumUnackedBytes: number
  #gap?: { expected: number; received: number }
  #spills = new Map<string, TerminalSpillProjection>()

  constructor(options: {
    cursor?: number
    earliestCursor?: number
    expectedSequence?: number
    maximumUnackedBytes?: number
  } = {}) {
    this.#cursor = this.#integer(options.cursor ?? 0, "cursor")
    this.#acknowledgedCursor = this.#cursor
    this.#earliestCursor = this.#integer(options.earliestCursor ?? 0, "earliest cursor")
    this.#expectedSequence = this.#integer(options.expectedSequence ?? 1, "sequence", 1)
    this.#maximumUnackedBytes = this.#integer(
      options.maximumUnackedBytes ?? 4 * 1_024 * 1_024,
      "unacknowledged byte budget",
      1_024,
      64 * 1_024 * 1_024,
    )
    if (this.#earliestCursor > this.#cursor) {
      throw new RangeError("Replay earliest cursor exceeds the current cursor.")
    }
  }

  admit(frame: TerminalOutputFrame): TerminalOutputAdmission {
    if (frame.sequence < this.#expectedSequence) {
      return { accepted: false, duplicate: true, needsResync: false, state: this.snapshot() }
    }
    if (frame.sequence > this.#expectedSequence) {
      this.#gap = { expected: this.#expectedSequence, received: frame.sequence }
      return {
        accepted: false,
        duplicate: false,
        needsResync: true,
        reason: "sequence_gap",
        state: this.snapshot(),
      }
    }
    if (frame.nextCursor <= this.#cursor) {
      this.#expectedSequence += 1
      return { accepted: false, duplicate: true, needsResync: false, state: this.snapshot() }
    }
    if (frame.firstCursor > this.#cursor) {
      this.#gap = { expected: this.#cursor, received: frame.firstCursor }
      return {
        accepted: false,
        duplicate: false,
        needsResync: true,
        reason: "cursor_gap",
        state: this.snapshot(),
      }
    }
    if (frame.firstCursor < this.#cursor) {
      this.#gap = { expected: this.#cursor, received: frame.firstCursor }
      return {
        accepted: false,
        duplicate: false,
        needsResync: true,
        reason: "cursor_overlap",
        state: this.snapshot(),
      }
    }
    const nextUnacked = frame.nextCursor - this.#acknowledgedCursor
    if (nextUnacked > this.#maximumUnackedBytes) {
      this.#paused = true
      return {
        accepted: false,
        duplicate: false,
        needsResync: false,
        reason: "acknowledgement_exhausted",
        state: this.snapshot(),
      }
    }
    if (this.#paused) {
      return {
        accepted: false,
        duplicate: false,
        needsResync: false,
        reason: "paused",
        state: this.snapshot(),
      }
    }
    this.#cursor = frame.nextCursor
    this.#expectedSequence += 1
    this.#gap = undefined
    return {
      accepted: true,
      duplicate: false,
      needsResync: false,
      frame,
      state: this.snapshot(),
    }
  }

  acknowledge(cursor: number): TerminalReplayState {
    const selected = this.#integer(cursor, "acknowledgement cursor")
    if (selected < this.#acknowledgedCursor) return this.snapshot()
    if (selected > this.#cursor) throw new RangeError("Acknowledgement exceeds the admitted cursor.")
    this.#acknowledgedCursor = selected
    if (this.#cursor - selected < this.#maximumUnackedBytes) this.#paused = false
    return this.snapshot()
  }

  applyBackpressure(frame: TerminalBackpressureFrame): TerminalReplayState {
    if (frame.requiredCursor > this.#cursor) {
      this.#gap = { expected: this.#cursor, received: frame.requiredCursor }
    }
    this.#maximumUnackedBytes = frame.maximumUnackedBytes
    this.#paused = frame.paused
    return this.snapshot()
  }

  resync(frame: TerminalResyncFrame): TerminalReplayState {
    this.#earliestCursor = frame.earliestCursor
    this.#cursor = frame.acceptedCursor
    this.#acknowledgedCursor = frame.acceptedCursor
    this.#expectedSequence = 1
    this.#paused = false
    this.#gap = undefined
    for (const spill of frame.spills) this.addSpill(spill)
    return this.snapshot()
  }

  addSpill(spill: TerminalSpillProjection): TerminalReplayState {
    const key = `${spill.artifactId}:${spill.revision}`
    const previous = this.#spills.get(key)
    if (previous && (
      previous.sha256 !== spill.sha256
      || previous.firstCursor !== spill.firstCursor
      || previous.nextCursor !== spill.nextCursor
    )) throw new TypeError("Terminal spill identity was reused with different content.")
    this.#spills.set(key, spill)
    if (spill.nextCursor > this.#earliestCursor && spill.firstCursor <= this.#earliestCursor) {
      this.#earliestCursor = spill.nextCursor
    }
    return this.snapshot()
  }

  restore(options: {
    cursor: number
    acknowledgedCursor: number
    earliestCursor: number
    expectedSequence?: number
  }): TerminalReplayState {
    const cursor = this.#integer(options.cursor, "restored cursor")
    const acknowledged = this.#integer(options.acknowledgedCursor, "restored acknowledgement")
    const earliest = this.#integer(options.earliestCursor, "restored earliest cursor")
    if (earliest > acknowledged || acknowledged > cursor) {
      throw new RangeError("Restored terminal cursor ordering is invalid.")
    }
    this.#cursor = cursor
    this.#acknowledgedCursor = acknowledged
    this.#earliestCursor = earliest
    this.#expectedSequence = this.#integer(options.expectedSequence ?? 1, "restored sequence", 1)
    this.#paused = cursor - acknowledged >= this.#maximumUnackedBytes
    this.#gap = undefined
    return this.snapshot()
  }

  snapshot(): TerminalReplayState {
    return Object.freeze({
      cursor: this.#cursor,
      acknowledgedCursor: this.#acknowledgedCursor,
      earliestCursor: this.#earliestCursor,
      expectedSequence: this.#expectedSequence,
      paused: this.#paused,
      unackedBytes: this.#cursor - this.#acknowledgedCursor,
      maximumUnackedBytes: this.#maximumUnackedBytes,
      gap: this.#gap ? Object.freeze({ ...this.#gap }) : undefined,
      spills: Object.freeze(
        [...this.#spills.values()].sort((a, b) =>
          a.firstCursor - b.firstCursor || a.artifactId.localeCompare(b.artifactId)
        ),
      ),
    })
  }

  #integer(value: number, label: string, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): number {
    if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
      throw new RangeError(`Terminal ${label} is outside ${minimum}..${maximum}.`)
    }
    return value
  }
}
