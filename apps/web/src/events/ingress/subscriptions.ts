import {
  eventMatchesFilter,
  normalizeIngressFilter,
  type ConnectionSnapshot,
  type IngressBatch,
  type IngressDiagnostic,
  type IngressObserver,
  type IngressSubscriptionFilter,
  type JsonValue,
  type NormalizedIngressFilter,
} from "./contracts.ts"
import {
  EventIngressError,
  IngressErrorCode,
  subscriberError,
} from "./errors.ts"

interface SubscriptionRecord {
  id: number
  observer: IngressObserver
  filter: NormalizedIngressFilter
  createdAtMs: number
  deliveredBatches: number
  deliveredEvents: number
  failures: number
  closed: boolean
  abortSignal?: AbortSignal
  abortListener?: () => void
}

export interface SubscriptionHandle {
  readonly id: number
  readonly taskId: string
  readonly closed: boolean
  close(reason?: unknown): boolean
}

export interface SubscriptionDispatchReport {
  observers: number
  deliveredObservers: number
  deliveredEvents: number
  filteredEvents: number
  failures: readonly EventIngressError[]
}

export class IngressSubscriptionRegistry {
  readonly #taskId: string
  readonly #now: () => number
  readonly #subscriptions = new Map<number, SubscriptionRecord>()
  #nextId = 1
  #revision = 0
  #union = normalizeIngressFilter(undefined)
  #closed = false
  #closeReason: unknown
  #onChange: ((count: number, filter: NormalizedIngressFilter, revision: number) => void) | undefined
  #onSubscriberError: ((error: EventIngressError, subscriptionId: number) => void) | undefined

  constructor(
    taskId: string,
    options: {
      now?: () => number
      onChange?: (count: number, filter: NormalizedIngressFilter, revision: number) => void
      onSubscriberError?: (error: EventIngressError, subscriptionId: number) => void
    } = {},
  ) {
    this.#taskId = String(taskId || "").trim()
    if (!this.#taskId) throw new TypeError("Subscription registry taskId must not be empty")
    this.#now = options.now ?? Date.now
    this.#onChange = options.onChange
    this.#onSubscriberError = options.onSubscriberError
  }

  get size(): number {
    return this.#subscriptions.size
  }

  get revision(): number {
    return this.#revision
  }

  get closed(): boolean {
    return this.#closed
  }

  subscribe(
    observer: IngressObserver,
    options: {
      filter?: IngressSubscriptionFilter
      signal?: AbortSignal
    } = {},
  ): SubscriptionHandle {
    this.#assertOpen()
    if (!observer || typeof observer.batch !== "function") {
      throw new TypeError("Ingress observer must provide a batch callback")
    }
    if (options.signal?.aborted) {
      throw new EventIngressError(
        IngressErrorCode.CANCELLED,
        "Cannot create an event subscription with an aborted signal.",
        { context: { taskId: this.#taskId } },
      )
    }
    const id = this.#nextId++
    const record: SubscriptionRecord = {
      id,
      observer,
      filter: normalizeIngressFilter(options.filter),
      createdAtMs: this.#now(),
      deliveredBatches: 0,
      deliveredEvents: 0,
      failures: 0,
      closed: false,
      abortSignal: options.signal,
    }
    if (options.signal) {
      record.abortListener = () => this.unsubscribe(id, options.signal?.reason)
      options.signal.addEventListener("abort", record.abortListener, { once: true })
    }
    this.#subscriptions.set(id, record)
    this.#changed()
    const registry = this
    return {
      id,
      taskId: this.#taskId,
      get closed() {
        return record.closed || registry.#closed
      },
      close(reason?: unknown) {
        return registry.unsubscribe(id, reason)
      },
    }
  }

  unsubscribe(id: number, _reason?: unknown): boolean {
    const record = this.#subscriptions.get(id)
    if (!record) return false
    record.closed = true
    if (record.abortSignal && record.abortListener) {
      record.abortSignal.removeEventListener("abort", record.abortListener)
    }
    this.#subscriptions.delete(id)
    this.#changed()
    return true
  }

  dispatchBatch(batch: IngressBatch): SubscriptionDispatchReport {
    this.#assertOpen()
    if (batch.taskId !== this.#taskId) {
      throw new EventIngressError(
        IngressErrorCode.CROSS_TASK,
        "Subscription registry rejected a batch for another task.",
        {
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: batch.generation,
            sequence: batch.sequence,
            details: { actualTaskId: batch.taskId },
          },
        },
      )
    }
    let deliveredObservers = 0
    let deliveredEvents = 0
    let filteredEvents = 0
    const failures: EventIngressError[] = []
    for (const record of [...this.#subscriptions.values()]) {
      if (record.closed) continue
      const events = batch.events.filter((event) => eventMatchesFilter(event, record.filter))
      filteredEvents += batch.events.length - events.length
      if (!events.length && batch.events.length) continue
      const eventIds = new Set(events.map((event) => event.eventId))
      const receipts = batch.receipts.filter((receipt) => eventIds.has(receipt.eventId))
      const projected: IngressBatch = Object.freeze({
        ...batch,
        events: Object.freeze(events),
        receipts: Object.freeze(receipts),
      })
      try {
        record.observer.batch(projected)
        record.deliveredBatches += 1
        record.deliveredEvents += events.length
        deliveredObservers += 1
        deliveredEvents += events.length
      } catch (error) {
        record.failures += 1
        const classified = subscriberError(error, {
          taskId: this.#taskId,
          generation: batch.generation,
          sequence: batch.sequence,
          details: { subscriptionId: record.id },
        })
        failures.push(classified)
        try {
          this.#onSubscriberError?.(classified, record.id)
        } catch {
          // Subscriber error reporting is isolated from canonical ingress progress.
        }
      }
    }
    return {
      observers: this.#subscriptions.size,
      deliveredObservers,
      deliveredEvents,
      filteredEvents,
      failures: Object.freeze(failures),
    }
  }

  dispatchStatus(snapshot: ConnectionSnapshot): readonly EventIngressError[] {
    if (this.#closed) return Object.freeze([])
    const failures: EventIngressError[] = []
    for (const record of [...this.#subscriptions.values()]) {
      if (record.closed || typeof record.observer.status !== "function") continue
      try {
        record.observer.status(snapshot)
      } catch (error) {
        record.failures += 1
        const classified = subscriberError(error, {
          taskId: this.#taskId,
          generation: snapshot.generation,
          sequence: snapshot.cursor.committedSequence,
          details: { subscriptionId: record.id, callback: "status" },
        })
        failures.push(classified)
        try {
          this.#onSubscriberError?.(classified, record.id)
        } catch {
          // Observer failure cannot interrupt other status callbacks.
        }
      }
    }
    return Object.freeze(failures)
  }

  dispatchDiagnostic(diagnostic: IngressDiagnostic): readonly EventIngressError[] {
    if (this.#closed) return Object.freeze([])
    const failures: EventIngressError[] = []
    for (const record of [...this.#subscriptions.values()]) {
      if (record.closed || typeof record.observer.diagnostic !== "function") continue
      try {
        record.observer.diagnostic(diagnostic)
      } catch (error) {
        record.failures += 1
        const classified = subscriberError(error, {
          taskId: this.#taskId,
          generation: diagnostic.generation,
          sequence: diagnostic.sequence,
          eventId: diagnostic.eventId,
          details: { subscriptionId: record.id, callback: "diagnostic" },
        })
        failures.push(classified)
        try {
          this.#onSubscriberError?.(classified, record.id)
        } catch {
          // Diagnostic observers are best effort and isolated.
        }
      }
    }
    return Object.freeze(failures)
  }

  unionFilter(): NormalizedIngressFilter {
    return this.#union
  }

  records(): readonly {
    id: number
    filter: NormalizedIngressFilter
    createdAtMs: number
    deliveredBatches: number
    deliveredEvents: number
    failures: number
    closed: boolean
  }[] {
    return [...this.#subscriptions.values()]
      .sort((left, right) => left.id - right.id)
      .map((record) => ({
        id: record.id,
        filter: record.filter,
        createdAtMs: record.createdAtMs,
        deliveredBatches: record.deliveredBatches,
        deliveredEvents: record.deliveredEvents,
        failures: record.failures,
        closed: record.closed,
      }))
  }

  close(reason?: unknown): number {
    if (this.#closed) return 0
    this.#closed = true
    this.#closeReason = reason
    const count = this.#subscriptions.size
    for (const record of this.#subscriptions.values()) {
      record.closed = true
      if (record.abortSignal && record.abortListener) {
        record.abortSignal.removeEventListener("abort", record.abortListener)
      }
    }
    this.#subscriptions.clear()
    this.#changed()
    return count
  }

  snapshot(): {
    taskId: string
    revision: number
    count: number
    union: NormalizedIngressFilter
    closed: boolean
    closeReason?: string
    totalDeliveredBatches: number
    totalDeliveredEvents: number
    totalFailures: number
  } {
    let totalDeliveredBatches = 0
    let totalDeliveredEvents = 0
    let totalFailures = 0
    for (const record of this.#subscriptions.values()) {
      totalDeliveredBatches += record.deliveredBatches
      totalDeliveredEvents += record.deliveredEvents
      totalFailures += record.failures
    }
    return {
      taskId: this.#taskId,
      revision: this.#revision,
      count: this.#subscriptions.size,
      union: this.#union,
      closed: this.#closed,
      closeReason: this.#closeReason === undefined ? undefined : String(this.#closeReason),
      totalDeliveredBatches,
      totalDeliveredEvents,
      totalFailures,
    }
  }

  exportState(): JsonValue {
    return {
      snapshot: this.snapshot() as unknown as JsonValue,
      records: this.records() as unknown as JsonValue,
    }
  }

  #changed(): void {
    this.#revision += 1
    this.#union = this.#computeUnion()
    try {
      this.#onChange?.(this.#subscriptions.size, this.#union, this.#revision)
    } catch {
      // Registry observers never own subscription state.
    }
  }

  #computeUnion(): NormalizedIngressFilter {
    if (!this.#subscriptions.size) return normalizeIngressFilter(undefined)
    const filters = [...this.#subscriptions.values()].map((record) => record.filter)
    const unionDimension = (
      select: (filter: NormalizedIngressFilter) => readonly string[],
    ): readonly string[] => {
      if (filters.some((filter) => select(filter).length === 0)) return Object.freeze([])
      return Object.freeze(
        [...new Set(filters.flatMap((filter) => [...select(filter)]))].sort(),
      )
    }
    const core = {
      eventTypes: unionDimension((filter) => filter.eventTypes),
      intents: unionDimension((filter) => filter.intents),
      correlationIds: unionDimension((filter) => filter.correlationIds),
      artifactIds: unionDimension((filter) => filter.artifactIds),
      includeNonEffective: filters.some((filter) => filter.includeNonEffective),
      includeHeartbeats: filters.some((filter) => filter.includeHeartbeats),
    }
    return Object.freeze({
      ...core,
      digestMaterial: JSON.stringify({
        eventTypes: core.eventTypes,
        intents: core.intents,
        correlationIds: core.correlationIds,
        artifactIds: core.artifactIds,
        includeNonEffective: core.includeNonEffective,
        includeHeartbeats: core.includeHeartbeats,
      }),
    })
  }

  #assertOpen(): void {
    if (!this.#closed) return
    throw new EventIngressError(
      IngressErrorCode.CLOSED,
      "Event subscription registry is closed.",
      {
        context: {
          taskId: this.#taskId,
          details: {
            reason: this.#closeReason === undefined
              ? null
              : String(this.#closeReason),
          },
        },
      },
    )
  }
}
