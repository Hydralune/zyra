import {
  ConnectionPhase,
  PressureLevel,
  emptyCursorSnapshot,
  type ConnectionPhaseValue,
  type ConnectionSnapshot,
  type CursorSnapshot,
  type IngressDiagnostic,
  type JsonValue,
  type PressureSnapshot,
  type TransportKindValue,
} from "./contracts.ts"
import type { EventIngressError } from "./errors.ts"

const EMPTY_PRESSURE: Readonly<PressureSnapshot> = Object.freeze({
  level: PressureLevel.NORMAL,
  items: 0,
  bytes: 0,
  maxItems: 0,
  maxBytes: 0,
  itemRatio: 0,
  byteRatio: 0,
  reservedItemsAvailable: 0,
  reservedBytesAvailable: 0,
  coalesced: 0,
  evicted: 0,
  rejected: 0,
})

export class IngressDiagnostics {
  readonly #taskId: string
  readonly #limit: number
  readonly #now: () => number
  readonly #items: IngressDiagnostic[] = []
  readonly #counters = new Map<string, number>()
  #nextId = 1
  #phase: ConnectionPhaseValue = ConnectionPhase.IDLE
  #generation = 0
  #transport: TransportKindValue | undefined
  #startedAtMs: number | undefined
  #connectedAtMs: number | undefined
  #lastFrameAtMs: number | undefined
  #lastHeartbeatAtMs: number | undefined
  #lastDeliveryAtMs: number | undefined
  #reconnectAttempt = 0
  #resyncAttempt = 0
  #subscribers = 0
  #cursor: CursorSnapshot
  #pressure: PressureSnapshot = EMPTY_PRESSURE
  #gap: ConnectionSnapshot["gap"]
  #error: ConnectionSnapshot["error"]

  constructor(
    taskId: string,
    limit: number,
    options: { now?: () => number } = {},
  ) {
    this.#taskId = String(taskId || "").trim()
    if (!this.#taskId) throw new TypeError("Ingress diagnostics taskId must not be empty")
    this.#limit = Math.max(64, Math.min(100_000, Math.floor(limit)))
    this.#now = options.now ?? Date.now
    this.#cursor = emptyCursorSnapshot(this.#taskId, this.#now())
  }

  transition(
    phase: ConnectionPhaseValue,
    details: {
      generation?: number
      transport?: TransportKindValue
      message?: string
      reconnectAttempt?: number
      resyncAttempt?: number
    } = {},
  ): ConnectionSnapshot {
    const previous = this.#phase
    const now = this.#now()
    this.#phase = phase
    if (details.generation !== undefined) this.#generation = details.generation
    if (details.transport !== undefined) this.#transport = details.transport
    if (details.reconnectAttempt !== undefined) {
      this.#reconnectAttempt = details.reconnectAttempt
    }
    if (details.resyncAttempt !== undefined) this.#resyncAttempt = details.resyncAttempt
    if (
      phase === ConnectionPhase.NEGOTIATING ||
      phase === ConnectionPhase.SUBSCRIBING ||
      phase === ConnectionPhase.SNAPSHOTTING
    ) this.#startedAtMs ??= now
    if (phase === ConnectionPhase.LIVE) {
      this.#connectedAtMs ??= now
      this.#error = undefined
    }
    if (phase === ConnectionPhase.STOPPED || phase === ConnectionPhase.IDLE) {
      this.#transport = undefined
    }
    this.record(
      "connection",
      `phase.${phase}`,
      details.message ?? `Connection transitioned from ${previous} to ${phase}.`,
      {
        previous,
        phase,
        reconnectAttempt: this.#reconnectAttempt,
        resyncAttempt: this.#resyncAttempt,
      },
    )
    return this.snapshot()
  }

  frame(
    code: "event" | "heartbeat" | "ready" | "close" | "error",
    details: {
      sequence?: number
      eventId?: string
      transport?: TransportKindValue
      generation?: number
    } = {},
  ): void {
    const now = this.#now()
    this.#lastFrameAtMs = now
    if (code === "heartbeat" || code === "ready") this.#lastHeartbeatAtMs = now
    if (details.generation !== undefined) this.#generation = details.generation
    if (details.transport !== undefined) this.#transport = details.transport
    this.increment(`frame.${code}`)
    this.record(
      "transport",
      `frame.${code}`,
      `Received ${code} ingress frame.`,
      {},
      {
        sequence: details.sequence,
        eventId: details.eventId,
        transport: details.transport,
      },
    )
  }

  delivery(count: number, sequence: number, bytes: number): void {
    this.#lastDeliveryAtMs = this.#now()
    this.increment("delivery.batches")
    this.increment("delivery.events", count)
    this.increment("delivery.bytes", bytes)
    this.record(
      "delivery",
      "delivery.batch",
      `Delivered ${count} committed ingress events.`,
      { count, bytes },
      { sequence },
    )
  }

  updateCursor(cursor: CursorSnapshot): void {
    this.#cursor = { ...cursor }
    this.#generation = cursor.generation
    this.record(
      "cursor",
      "cursor.updated",
      "Committed event cursor advanced.",
      {
        committedSequence: cursor.committedSequence,
        observedSequence: cursor.observedSequence,
        highWatermark: cursor.highWatermark,
        snapshotComplete: cursor.snapshotComplete,
      },
      { sequence: cursor.committedSequence },
    )
  }

  updatePressure(pressure: PressureSnapshot): void {
    const changed = pressure.level !== this.#pressure.level
    this.#pressure = { ...pressure }
    if (changed || pressure.level === PressureLevel.CRITICAL || pressure.level === PressureLevel.OVERFLOW) {
      this.record(
        "pressure",
        `pressure.${pressure.level}`,
        `Ingress buffer pressure is ${pressure.level}.`,
        {
          items: pressure.items,
          bytes: pressure.bytes,
          itemRatio: pressure.itemRatio,
          byteRatio: pressure.byteRatio,
          coalesced: pressure.coalesced,
          evicted: pressure.evicted,
          rejected: pressure.rejected,
        },
      )
    }
  }

  updateGap(gap: ConnectionSnapshot["gap"]): void {
    const previous = this.#gap
    this.#gap = gap ? { ...gap, eventIds: Object.freeze([...gap.eventIds]) } : undefined
    if (gap) {
      this.increment("gap.observed")
      this.record(
        "gap",
        "gap.open",
        "Ingress delivery is waiting for a missing predecessor.",
        {
          expectedPrevious: gap.expectedPrevious,
          observedPrevious: gap.observedPrevious,
          observedSequence: gap.observedSequence,
          attempts: gap.attempts,
          eventIds: [...gap.eventIds],
        },
        { sequence: gap.observedSequence },
      )
    } else if (previous) {
      this.increment("gap.resolved")
      this.record(
        "gap",
        "gap.resolved",
        "Ingress predecessor gap was repaired.",
        { expectedPrevious: previous.expectedPrevious },
      )
    }
  }

  failure(error: EventIngressError): void {
    this.#error = {
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      resyncRequired: error.resyncRequired,
      observedAtMs: error.observedAtMs,
    }
    this.increment(`error.${error.code}`)
    this.record(
      error.resyncRequired ? "recovery" : "transport",
      error.code,
      error.message,
      {
        retryable: error.retryable,
        resyncRequired: error.resyncRequired,
        context: error.context as unknown as JsonValue,
      },
      {
        sequence: error.context.sequence,
        eventId: error.context.eventId,
        transport: error.context.transport,
      },
    )
  }

  subscribers(count: number): void {
    this.#subscribers = Math.max(0, Math.floor(count))
    this.record(
      "subscription",
      "subscription.count",
      `Ingress subscriber count is ${this.#subscribers}.`,
      { subscribers: this.#subscribers },
    )
  }

  attempt(kind: "reconnect" | "resync", value: number): void {
    if (kind === "reconnect") this.#reconnectAttempt = value
    else this.#resyncAttempt = value
    this.increment(`${kind}.attempt`)
    this.record(
      "recovery",
      `${kind}.attempt`,
      `Starting ${kind} attempt ${value}.`,
      { attempt: value },
    )
  }

  record(
    category: IngressDiagnostic["category"],
    code: string,
    message: string,
    details: Record<string, JsonValue> = {},
    binding: {
      sequence?: number
      eventId?: string
      transport?: TransportKindValue
    } = {},
  ): IngressDiagnostic {
    const item: IngressDiagnostic = Object.freeze({
      id: this.#nextId++,
      taskId: this.#taskId,
      generation: this.#generation,
      atMs: this.#now(),
      category,
      code,
      message,
      sequence: binding.sequence,
      eventId: binding.eventId,
      transport: binding.transport ?? this.#transport,
      details: Object.freeze({ ...details }),
    })
    this.#items.push(item)
    if (this.#items.length > this.#limit) {
      this.#items.splice(0, this.#items.length - this.#limit)
    }
    this.increment(`diagnostic.${category}`)
    return item
  }

  increment(key: string, amount = 1): number {
    const next = (this.#counters.get(key) ?? 0) + amount
    this.#counters.set(key, next)
    return next
  }

  snapshot(): ConnectionSnapshot {
    return {
      taskId: this.#taskId,
      phase: this.#phase,
      generation: this.#generation,
      transport: this.#transport,
      startedAtMs: this.#startedAtMs,
      connectedAtMs: this.#connectedAtMs,
      lastFrameAtMs: this.#lastFrameAtMs,
      lastHeartbeatAtMs: this.#lastHeartbeatAtMs,
      lastDeliveryAtMs: this.#lastDeliveryAtMs,
      reconnectAttempt: this.#reconnectAttempt,
      resyncAttempt: this.#resyncAttempt,
      subscribers: this.#subscribers,
      cursor: { ...this.#cursor },
      pressure: { ...this.#pressure },
      gap: this.#gap
        ? { ...this.#gap, eventIds: Object.freeze([...this.#gap.eventIds]) }
        : undefined,
      error: this.#error ? { ...this.#error } : undefined,
    }
  }

  items(options: {
    afterId?: number
    category?: IngressDiagnostic["category"]
    code?: string
    limit?: number
  } = {}): readonly IngressDiagnostic[] {
    const limit = Math.max(1, Math.min(this.#limit, Math.floor(options.limit ?? this.#limit)))
    return this.#items
      .filter((item) => options.afterId === undefined || item.id > options.afterId)
      .filter((item) => options.category === undefined || item.category === options.category)
      .filter((item) => options.code === undefined || item.code === options.code)
      .slice(-limit)
      .map((item) => ({ ...item, details: { ...item.details } }))
  }

  counters(): Readonly<Record<string, number>> {
    return Object.freeze(
      Object.fromEntries(
        [...this.#counters.entries()].sort(([left], [right]) => left.localeCompare(right)),
      ),
    )
  }

  exportState(limit = 1024): JsonValue {
    return {
      connection: this.snapshot() as unknown as JsonValue,
      diagnostics: this.items({ limit }) as unknown as JsonValue,
      counters: this.counters() as unknown as JsonValue,
    }
  }
}
