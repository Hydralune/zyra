import {
  emptyCursorSnapshot,
  type CursorSnapshot,
  type IngressEventFrame,
  type IngressPage,
  type JsonValue,
} from "./contracts.ts"
import {
  EventIngressError,
  IngressErrorCode,
  invariant,
} from "./errors.ts"

export interface CursorObservation {
  source: string
  generation: number
  sequence: number
  previousSequence: number
  cursor?: string
  eventId?: string
  observedAtMs: number
  committed: boolean
}

export interface CursorTransition {
  before: CursorSnapshot
  after: CursorSnapshot
  observation: CursorObservation
  advanced: boolean
  duplicate: boolean
  stale: boolean
  gap: boolean
}

interface GenerationState {
  generation: number
  startedAtMs: number
  observations: CursorObservation[]
  sourceHighWatermarks: Map<string, number>
  eventSequenceById: Map<string, number>
  eventIdBySequence: Map<number, string>
}

export class EventCursorLedger {
  readonly #taskId: string
  readonly #maximumObservations: number
  readonly #maximumIdentities: number
  readonly #now: () => number
  #snapshot: CursorSnapshot
  #state: GenerationState
  #disabled = false

  constructor(
    taskId: string,
    options: {
      maximumObservations?: number
      maximumIdentities?: number
      now?: () => number
      initialCursor?: string
      initialSequence?: number
    } = {},
  ) {
    this.#taskId = String(taskId || "").trim()
    if (!this.#taskId) throw new TypeError("Cursor ledger taskId must not be empty")
    this.#maximumObservations = Math.max(
      64,
      Math.min(100_000, Math.floor(options.maximumObservations ?? 8_192)),
    )
    this.#maximumIdentities = Math.max(
      1_024,
      Math.min(5_000_000, Math.floor(options.maximumIdentities ?? 100_000)),
    )
    this.#now = options.now ?? Date.now
    const now = this.#now()
    this.#snapshot = {
      ...emptyCursorSnapshot(this.#taskId, now),
      cursor: options.initialCursor,
      committedSequence: Math.max(0, Math.floor(options.initialSequence ?? 0)),
      observedSequence: Math.max(0, Math.floor(options.initialSequence ?? 0)),
    }
    this.#state = this.#newGeneration(0, now)
  }

  get taskId(): string {
    return this.#taskId
  }

  get generation(): number {
    return this.#snapshot.generation
  }

  get committedSequence(): number {
    return this.#snapshot.committedSequence
  }

  get cursor(): string | undefined {
    return this.#snapshot.cursor
  }

  get snapshotComplete(): boolean {
    return this.#snapshot.snapshotComplete
  }

  disable(): void {
    this.#disabled = true
  }

  enable(): void {
    this.#disabled = false
  }

  beginGeneration(options: {
    cursor?: string
    committedSequence?: number
    preserveCommitted?: boolean
  } = {}): CursorSnapshot {
    this.#assertEnabled("beginGeneration")
    const before = this.#snapshot
    const generation = before.generation + 1
    const now = this.#now()
    const committed = options.preserveCommitted === false
      ? Math.max(0, Math.floor(options.committedSequence ?? 0))
      : Math.max(
          before.committedSequence,
          Math.floor(options.committedSequence ?? before.committedSequence),
        )
    this.#snapshot = {
      taskId: this.#taskId,
      generation,
      cursor: options.cursor ?? before.cursor,
      committedSequence: committed,
      observedSequence: committed,
      snapshotBoundary: 0,
      snapshotComplete: false,
      highWatermark: committed,
      lastEventId: before.lastEventId,
      updatedAtMs: now,
    }
    this.#state = this.#newGeneration(generation, now)
    return this.snapshot()
  }

  beginSnapshot(boundary: number, cursor?: string): CursorSnapshot {
    this.#assertEnabled("beginSnapshot")
    const normalized = this.#sequence(boundary, "snapshot boundary")
    if (normalized < this.#snapshot.committedSequence) {
      throw new EventIngressError(
        IngressErrorCode.SNAPSHOT_STALE,
        "Snapshot boundary precedes the committed cursor.",
        {
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: this.generation,
            sequence: normalized,
            previousSequence: this.committedSequence,
          },
        },
      )
    }
    this.#snapshot = {
      ...this.#snapshot,
      cursor: cursor ?? this.#snapshot.cursor,
      snapshotBoundary: normalized,
      snapshotComplete: false,
      highWatermark: Math.max(this.#snapshot.highWatermark, normalized),
      updatedAtMs: this.#now(),
    }
    return this.snapshot()
  }

  observeFrame(frame: IngressEventFrame, source = frame.source): CursorTransition {
    this.#assertEnabled("observeFrame")
    this.#assertGeneration(frame.generation)
    if (frame.taskId !== this.#taskId) {
      throw new EventIngressError(
        IngressErrorCode.CROSS_TASK,
        "Cursor ledger rejected a frame for another task.",
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
    const knownSequence = this.#state.eventSequenceById.get(frame.eventId)
    const knownEvent = this.#state.eventIdBySequence.get(frame.sequence)
    if (knownSequence !== undefined && knownSequence !== frame.sequence) {
      throw new EventIngressError(
        IngressErrorCode.DUPLICATE_CONFLICT,
        "One event identity was observed at multiple canonical sequences.",
        {
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
            details: { knownSequence },
          },
        },
      )
    }
    if (knownEvent !== undefined && knownEvent !== frame.eventId) {
      throw new EventIngressError(
        IngressErrorCode.DUPLICATE_CONFLICT,
        "One canonical sequence was observed with multiple event identities.",
        {
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
            details: { knownEventId: knownEvent },
          },
        },
      )
    }
    const before = this.snapshot()
    const duplicate = knownSequence === frame.sequence
    const stale = frame.sequence <= before.committedSequence
    const gap = !stale && frame.previousSequence !== before.committedSequence
    this.#rememberIdentity(frame.eventId, frame.sequence)
    this.#observe({
      source,
      generation: frame.generation,
      sequence: frame.sequence,
      previousSequence: frame.previousSequence,
      eventId: frame.eventId,
      observedAtMs: frame.observedAtMs,
      committed: false,
    })
    this.#snapshot = {
      ...this.#snapshot,
      observedSequence: Math.max(before.observedSequence, frame.sequence),
      highWatermark: Math.max(before.highWatermark, frame.sequence),
      updatedAtMs: this.#now(),
    }
    return {
      before,
      after: this.snapshot(),
      observation: this.#state.observations[this.#state.observations.length - 1]!,
      advanced: false,
      duplicate,
      stale,
      gap,
    }
  }

  commitFrame(frame: IngressEventFrame, cursor?: string): CursorTransition {
    this.#assertEnabled("commitFrame")
    this.#assertGeneration(frame.generation)
    const before = this.snapshot()
    const knownSequence = this.#state.eventSequenceById.get(frame.eventId)
    const duplicate = frame.sequence <= before.committedSequence
    if (duplicate) {
      if (knownSequence !== undefined && knownSequence !== frame.sequence) {
        throw new EventIngressError(
          IngressErrorCode.DUPLICATE_CONFLICT,
          "Duplicate event identity changed canonical sequence.",
          {
            resyncRequired: true,
            context: {
              taskId: this.#taskId,
              generation: frame.generation,
              eventId: frame.eventId,
              sequence: frame.sequence,
              details: { knownSequence },
            },
          },
        )
      }
      const observation: CursorObservation = {
        source: frame.source,
        generation: frame.generation,
        sequence: frame.sequence,
        previousSequence: frame.previousSequence,
        cursor,
        eventId: frame.eventId,
        observedAtMs: this.#now(),
        committed: false,
      }
      this.#observe(observation)
      return {
        before,
        after: this.snapshot(),
        observation,
        advanced: false,
        duplicate: true,
        stale: frame.sequence < before.committedSequence,
        gap: false,
      }
    }
    if (frame.previousSequence !== before.committedSequence) {
      throw new EventIngressError(
        IngressErrorCode.GAP_DETECTED,
        "Cannot commit an event whose predecessor is not committed.",
        {
          retryable: true,
          context: {
            taskId: this.#taskId,
            generation: frame.generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
            previousSequence: frame.previousSequence,
            details: { committedSequence: before.committedSequence },
          },
        },
      )
    }
    this.#rememberIdentity(frame.eventId, frame.sequence)
    const observation: CursorObservation = {
      source: frame.source,
      generation: frame.generation,
      sequence: frame.sequence,
      previousSequence: frame.previousSequence,
      cursor,
      eventId: frame.eventId,
      observedAtMs: this.#now(),
      committed: true,
    }
    this.#observe(observation)
    this.#snapshot = {
      ...this.#snapshot,
      cursor: cursor ?? this.#snapshot.cursor,
      committedSequence: frame.sequence,
      observedSequence: Math.max(this.#snapshot.observedSequence, frame.sequence),
      highWatermark: Math.max(this.#snapshot.highWatermark, frame.sequence),
      lastEventId: frame.eventId,
      updatedAtMs: observation.observedAtMs,
    }
    return {
      before,
      after: this.snapshot(),
      observation,
      advanced: true,
      duplicate: false,
      stale: false,
      gap: false,
    }
  }

  acceptPage(page: IngressPage): CursorSnapshot {
    this.#assertEnabled("acceptPage")
    this.#assertGeneration(page.generation)
    if (page.taskId !== this.#taskId) {
      throw new EventIngressError(
        IngressErrorCode.CROSS_TASK,
        "Cursor ledger rejected a page for another task.",
        {
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: page.generation,
            details: { actualTaskId: page.taskId },
          },
        },
      )
    }
    if (page.fromSequence > this.#snapshot.committedSequence) {
      throw new EventIngressError(
        IngressErrorCode.GAP_DETECTED,
        "Page begins after the committed cursor.",
        {
          retryable: true,
          context: {
            taskId: this.#taskId,
            generation: page.generation,
            sequence: page.fromSequence,
            previousSequence: this.#snapshot.committedSequence,
          },
        },
      )
    }
    if (
      page.nextSequence < this.#snapshot.committedSequence &&
      !page.frames.length
    ) {
      throw new EventIngressError(
        IngressErrorCode.CURSOR_REGRESSION,
        "Empty page regressed the committed cursor.",
        {
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: page.generation,
            sequence: page.nextSequence,
            previousSequence: this.#snapshot.committedSequence,
          },
        },
      )
    }
    for (const frame of page.frames) {
      this.observeFrame(frame, page.schema)
    }
    this.#snapshot = {
      ...this.#snapshot,
      cursor: page.cursor,
      observedSequence: Math.max(this.#snapshot.observedSequence, page.nextSequence),
      highWatermark: Math.max(
        this.#snapshot.highWatermark,
        page.highWatermark ?? page.boundary ?? page.nextSequence,
      ),
      snapshotBoundary: Math.max(
        this.#snapshot.snapshotBoundary,
        page.boundary ?? 0,
      ),
      updatedAtMs: this.#now(),
    }
    return this.snapshot()
  }

  advanceEmptyPage(page: IngressPage): CursorSnapshot {
    this.#assertEnabled("advanceEmptyPage")
    this.#assertGeneration(page.generation)
    if (page.frames.length) {
      throw new EventIngressError(
        IngressErrorCode.INTERNAL_INVARIANT,
        "advanceEmptyPage cannot process event frames.",
        { context: { taskId: this.#taskId, generation: page.generation } },
      )
    }
    if (page.nextSequence < this.#snapshot.committedSequence) {
      throw new EventIngressError(
        IngressErrorCode.CURSOR_REGRESSION,
        "Empty page cursor regressed the committed sequence.",
        {
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: page.generation,
            sequence: page.nextSequence,
            previousSequence: this.#snapshot.committedSequence,
          },
        },
      )
    }
    const now = this.#now()
    this.#observe({
      source: page.schema,
      generation: page.generation,
      sequence: page.nextSequence,
      previousSequence: this.#snapshot.committedSequence,
      cursor: page.cursor,
      observedAtMs: now,
      committed: true,
    })
    this.#snapshot = {
      ...this.#snapshot,
      cursor: page.cursor,
      committedSequence: page.nextSequence,
      observedSequence: Math.max(this.#snapshot.observedSequence, page.nextSequence),
      highWatermark: Math.max(
        this.#snapshot.highWatermark,
        page.highWatermark ?? page.boundary ?? page.nextSequence,
      ),
      updatedAtMs: now,
    }
    return this.snapshot()
  }

  completeSnapshot(page: IngressPage): CursorSnapshot {
    this.#assertEnabled("completeSnapshot")
    this.#assertGeneration(page.generation)
    if (page.complete !== true || page.cursorKind !== "delta") {
      throw new EventIngressError(
        IngressErrorCode.SNAPSHOT_INCOMPLETE,
        "Snapshot cannot complete without a promoted delta cursor.",
        {
          retryable: true,
          context: {
            taskId: this.#taskId,
            generation: page.generation,
            sequence: page.nextSequence,
          },
        },
      )
    }
    const boundary = page.boundary ?? this.#snapshot.snapshotBoundary
    if (page.nextSequence !== boundary) {
      throw new EventIngressError(
        IngressErrorCode.SNAPSHOT_INCOMPLETE,
        "Complete snapshot cursor does not equal its captured boundary.",
        {
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: page.generation,
            sequence: page.nextSequence,
            details: { boundary },
          },
        },
      )
    }
    if (this.#snapshot.committedSequence < boundary) {
      throw new EventIngressError(
        IngressErrorCode.SNAPSHOT_INCOMPLETE,
        "Snapshot boundary has not been committed.",
        {
          retryable: true,
          context: {
            taskId: this.#taskId,
            generation: page.generation,
            sequence: this.#snapshot.committedSequence,
            details: { boundary },
          },
        },
      )
    }
    this.#snapshot = {
      ...this.#snapshot,
      cursor: page.cursor,
      snapshotBoundary: boundary,
      snapshotComplete: true,
      highWatermark: Math.max(this.#snapshot.highWatermark, boundary),
      updatedAtMs: this.#now(),
    }
    return this.snapshot()
  }

  updateCursor(cursor: string, sequence?: number): CursorSnapshot {
    this.#assertEnabled("updateCursor")
    const normalized = String(cursor || "").trim()
    if (!normalized) {
      throw new EventIngressError(
        IngressErrorCode.CURSOR_MISSING,
        "Cannot update the ledger with an empty cursor.",
        { context: { taskId: this.#taskId, generation: this.generation } },
      )
    }
    const next = sequence === undefined
      ? this.#snapshot.committedSequence
      : this.#sequence(sequence, "cursor sequence")
    if (next < this.#snapshot.committedSequence) {
      throw new EventIngressError(
        IngressErrorCode.CURSOR_REGRESSION,
        "Cursor update regressed the committed sequence.",
        {
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: this.generation,
            sequence: next,
            previousSequence: this.#snapshot.committedSequence,
          },
        },
      )
    }
    this.#snapshot = {
      ...this.#snapshot,
      cursor: normalized,
      committedSequence: next,
      observedSequence: Math.max(this.#snapshot.observedSequence, next),
      highWatermark: Math.max(this.#snapshot.highWatermark, next),
      updatedAtMs: this.#now(),
    }
    return this.snapshot()
  }

  snapshot(): CursorSnapshot {
    return {
      ...this.#snapshot,
    }
  }

  observations(options: {
    source?: string
    committed?: boolean
    afterSequence?: number
    limit?: number
  } = {}): readonly CursorObservation[] {
    const limit = Math.max(
      1,
      Math.min(this.#maximumObservations, Math.floor(options.limit ?? this.#maximumObservations)),
    )
    return this.#state.observations
      .filter((item) => options.source === undefined || item.source === options.source)
      .filter((item) => options.committed === undefined || item.committed === options.committed)
      .filter((item) => options.afterSequence === undefined || item.sequence > options.afterSequence)
      .slice(-limit)
      .map((item) => ({ ...item }))
  }

  sourceHighWatermarks(): Readonly<Record<string, number>> {
    return Object.freeze(
      Object.fromEntries(
        [...this.#state.sourceHighWatermarks.entries()].sort(([left], [right]) =>
          left.localeCompare(right),
        ),
      ),
    )
  }

  audit(): {
    ok: boolean
    findings: readonly string[]
    snapshot: CursorSnapshot
    identityCount: number
    observationCount: number
    sourceHighWatermarks: Readonly<Record<string, number>>
  } {
    const findings: string[] = []
    if (this.#snapshot.committedSequence > this.#snapshot.observedSequence) {
      findings.push("committed_sequence_exceeds_observed_sequence")
    }
    if (this.#snapshot.observedSequence > this.#snapshot.highWatermark) {
      findings.push("observed_sequence_exceeds_high_watermark")
    }
    if (
      this.#snapshot.snapshotComplete &&
      this.#snapshot.committedSequence < this.#snapshot.snapshotBoundary
    ) {
      findings.push("snapshot_marked_complete_before_boundary")
    }
    if (this.#snapshot.generation !== this.#state.generation) {
      findings.push("generation_state_mismatch")
    }
    for (const [eventId, sequence] of this.#state.eventSequenceById) {
      if (this.#state.eventIdBySequence.get(sequence) !== eventId) {
        findings.push(`identity_bijection_failed:${eventId}:${sequence}`)
        if (findings.length >= 100) break
      }
    }
    let previousCommitted = 0
    for (const observation of this.#state.observations) {
      if (!observation.committed) continue
      if (observation.sequence < previousCommitted) {
        findings.push(`committed_observation_regressed:${observation.sequence}`)
        break
      }
      previousCommitted = observation.sequence
    }
    return {
      ok: findings.length === 0,
      findings: Object.freeze(findings),
      snapshot: this.snapshot(),
      identityCount: this.#state.eventSequenceById.size,
      observationCount: this.#state.observations.length,
      sourceHighWatermarks: this.sourceHighWatermarks(),
    }
  }

  exportState(): JsonValue {
    const audit = this.audit()
    return {
      taskId: this.#taskId,
      generation: this.generation,
      snapshot: audit.snapshot as unknown as JsonValue,
      observations: this.observations({ limit: 1024 }) as unknown as JsonValue,
      sourceHighWatermarks: audit.sourceHighWatermarks as unknown as JsonValue,
      findings: [...audit.findings],
      disabled: this.#disabled,
    }
  }

  #newGeneration(generation: number, now: number): GenerationState {
    return {
      generation,
      startedAtMs: now,
      observations: [],
      sourceHighWatermarks: new Map(),
      eventSequenceById: new Map(),
      eventIdBySequence: new Map(),
    }
  }

  #rememberIdentity(eventId: string, sequence: number): void {
    this.#state.eventSequenceById.delete(eventId)
    this.#state.eventSequenceById.set(eventId, sequence)
    this.#state.eventIdBySequence.delete(sequence)
    this.#state.eventIdBySequence.set(sequence, eventId)
    while (this.#state.eventSequenceById.size > this.#maximumIdentities) {
      const oldestId = this.#state.eventSequenceById.keys().next().value as string | undefined
      if (!oldestId) break
      const oldestSequence = this.#state.eventSequenceById.get(oldestId)
      this.#state.eventSequenceById.delete(oldestId)
      if (
        oldestSequence !== undefined &&
        this.#state.eventIdBySequence.get(oldestSequence) === oldestId
      ) this.#state.eventIdBySequence.delete(oldestSequence)
    }
    while (this.#state.eventIdBySequence.size > this.#maximumIdentities) {
      const oldestSequence = this.#state.eventIdBySequence.keys().next().value as number | undefined
      if (oldestSequence === undefined) break
      const oldestId = this.#state.eventIdBySequence.get(oldestSequence)
      this.#state.eventIdBySequence.delete(oldestSequence)
      if (
        oldestId &&
        this.#state.eventSequenceById.get(oldestId) === oldestSequence
      ) this.#state.eventSequenceById.delete(oldestId)
    }
  }

  #observe(observation: CursorObservation): void {
    this.#state.observations.push(Object.freeze({ ...observation }))
    if (this.#state.observations.length > this.#maximumObservations) {
      this.#state.observations.splice(
        0,
        this.#state.observations.length - this.#maximumObservations,
      )
    }
    this.#state.sourceHighWatermarks.set(
      observation.source,
      Math.max(
        this.#state.sourceHighWatermarks.get(observation.source) ?? 0,
        observation.sequence,
      ),
    )
  }

  #sequence(value: number, label: string): number {
    if (!Number.isSafeInteger(value) || value < 0) {
      throw new EventIngressError(
        IngressErrorCode.INVALID_CONFIGURATION,
        `${label} must be a non-negative safe integer.`,
        { context: { taskId: this.#taskId, generation: this.generation } },
      )
    }
    return value
  }

  #assertGeneration(generation: number): void {
    invariant(
      generation === this.generation,
      `Cursor ledger generation ${this.generation} rejected generation ${generation}.`,
      {
        taskId: this.#taskId,
        generation,
        details: { activeGeneration: this.generation },
      },
    )
  }

  #assertEnabled(operation: string): void {
    if (!this.#disabled) return
    throw new EventIngressError(
      IngressErrorCode.DISABLED,
      `Cursor ledger is disabled during ${operation}.`,
      {
        context: {
          taskId: this.#taskId,
          generation: this.generation,
          operation,
        },
      },
    )
  }
}
