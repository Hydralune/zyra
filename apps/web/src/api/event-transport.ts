import { normalizeIdentity } from "../../../../packages/core/typed-api-client/src/index.ts"
import {
  EventIngressCoordinator,
  EventIngressError,
  IngressErrorCode,
  TaskApiEventIngressSource,
  type ConnectionSnapshot,
  type EventIngressCoordinatorOptions,
  type IngressBatch,
  type IngressDiagnostic,
  type IngressLiveFrame,
  type IngressObserver,
  type IngressSubscriptionFilter,
  type JsonValue,
  type TransportKindValue,
} from "../events/ingress/index.ts"
import type { TaskApi } from "./task-api.ts"

export type EventBatch = IngressBatch

export interface EventSubscriptionOptions {
  cursor?: string
  /**
   * Compatibility alias from the M2-S01A polling facade.  New callers should
   * pass an opaque event-ingress cursor.  Event ids are rejected explicitly
   * because they cannot be converted into a signed canonical sequence.
   */
  after?: string
  filter?: IngressSubscriptionFilter
  signal?: AbortSignal
  transportPreference?: readonly TransportKindValue[]
  snapshotPageSize?: number
  deltaPageSize?: number
  longPollMs?: number
  heartbeatTimeoutMs?: number
  capacity?: EventIngressCoordinatorOptions["capacity"]
  reconnect?: EventIngressCoordinatorOptions["reconnect"]
  status?: (snapshot: ConnectionSnapshot) => void
  diagnostic?: (diagnostic: IngressDiagnostic) => void
  /** Observer hooks for owners that share this connection with the store. */
  live?: (frame: IngressLiveFrame) => void
  batch?: (batch: IngressBatch) => void
}

interface CoordinatorRecord {
  coordinator: EventIngressCoordinator
  optionsKey: string
  createdAtMs: number
}

function normalizedCursor(options: EventSubscriptionOptions): string | undefined {
  const cursor = String(options.cursor || options.after || "").trim()
  if (!cursor) return undefined
  if (options.after && !options.cursor && !cursor.includes(".")) {
    throw new EventIngressError(
      IngressErrorCode.CURSOR_SCOPE,
      "Legacy event-id cursors cannot resume the canonical event ingress; request a fresh snapshot.",
      { resyncRequired: true },
    )
  }
  return cursor
}

function stableOptions(options: EventSubscriptionOptions): string {
  return JSON.stringify({
    cursor: normalizedCursor(options) ?? null,
    transportPreference: options.transportPreference ?? null,
    snapshotPageSize: options.snapshotPageSize ?? null,
    deltaPageSize: options.deltaPageSize ?? null,
    longPollMs: options.longPollMs ?? null,
    heartbeatTimeoutMs: options.heartbeatTimeoutMs ?? null,
    capacity: options.capacity ?? null,
    reconnect: options.reconnect ?? null,
  })
}

export class TaskEventTransport {
  readonly #source: TaskApiEventIngressSource
  readonly #coordinators = new Map<string, CoordinatorRecord>()
  #closed = false
  #disabled = false

  constructor(api: TaskApi) {
    this.#source = new TaskApiEventIngressSource(api)
  }

  subscribe(
    taskId: string,
    listener: ((batch: EventBatch) => void) | IngressObserver,
    options: EventSubscriptionOptions = {},
  ): () => void {
    this.#assertAvailable()
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const observer: IngressObserver =
      typeof listener === "function"
        ? {
            batch: listener,
            live: options.live,
            status: options.status,
            diagnostic: options.diagnostic,
          }
        : listener
    const record = this.#coordinator(normalizedTaskId, options)
    const handle = record.coordinator.subscribe(observer, {
      filter: options.filter,
      signal: options.signal,
    })
    void record.coordinator.start()
    let unsubscribed = false
    return () => {
      if (unsubscribed) return
      unsubscribed = true
      handle.close("Browser event observer unsubscribed.")
      if (record.coordinator.subscriberCount > 0) return
      record.coordinator.stop("Last browser event subscriber removed.")
      record.coordinator.close("Task event transport released idle coordinator.")
      if (this.#coordinators.get(normalizedTaskId) === record) {
        this.#coordinators.delete(normalizedTaskId)
      }
    }
  }

  stop(taskId: string, reason?: unknown): boolean {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const record = this.#coordinators.get(normalizedTaskId)
    if (!record) return false
    record.coordinator.close(reason ?? "Task event transport stopped.")
    this.#coordinators.delete(normalizedTaskId)
    return true
  }

  stopAll(reason?: unknown): number {
    const records = [...this.#coordinators.entries()]
    for (const [, record] of records) {
      record.coordinator.close(reason ?? "All task event transports stopped.")
    }
    this.#coordinators.clear()
    return records.length
  }

  disable(reason = "Task event transport disabled."): void {
    if (this.#disabled) return
    this.#disabled = true
    for (const record of this.#coordinators.values()) {
      record.coordinator.disable(reason)
    }
  }

  enable(): void {
    if (!this.#disabled || this.#closed) return
    this.#disabled = false
    for (const record of this.#coordinators.values()) {
      record.coordinator.enable()
    }
  }

  close(reason?: unknown): void {
    if (this.#closed) return
    this.#closed = true
    this.stopAll(reason ?? "Task event transport closed.")
  }

  snapshot(taskId: string): ConnectionSnapshot | undefined {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    return this.#coordinators.get(normalizedTaskId)?.coordinator.snapshot()
  }

  snapshots(): Record<string, ConnectionSnapshot> {
    return Object.fromEntries(
      [...this.#coordinators.entries()]
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([taskId, record]) => [taskId, record.coordinator.snapshot()]),
    )
  }

  diagnostics(
    taskId: string,
    options: {
      afterId?: number
      category?: IngressDiagnostic["category"]
      code?: string
      limit?: number
    } = {},
  ): readonly IngressDiagnostic[] {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    return this.#coordinators
      .get(normalizedTaskId)
      ?.coordinator.diagnostics(options) ?? Object.freeze([])
  }

  audit(taskId: string): ReturnType<EventIngressCoordinator["audit"]> | undefined {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    return this.#coordinators.get(normalizedTaskId)?.coordinator.audit()
  }

  exportState(): JsonValue {
    return {
      closed: this.#closed,
      disabled: this.#disabled,
      tasks: Object.fromEntries(
        [...this.#coordinators.entries()]
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([taskId, record]) => [
            taskId,
            {
              createdAtMs: record.createdAtMs,
              optionsKey: record.optionsKey,
              ingress: record.coordinator.exportState(),
            },
          ]),
      ),
    }
  }

  #coordinator(
    taskId: string,
    options: EventSubscriptionOptions,
  ): CoordinatorRecord {
    const key = stableOptions(options)
    const existing = this.#coordinators.get(taskId)
    if (existing) {
      if (existing.optionsKey !== key) {
        const diagnostic = existing.coordinator.snapshot()
        if (diagnostic.subscribers > 0) {
          // Per-observer filters may differ, but connection-level capacity,
          // retry, and cursor policy are immutable while the task is active.
          const candidate = JSON.parse(key) as Record<string, unknown>
          const current = JSON.parse(existing.optionsKey) as Record<string, unknown>
          for (const field of [
            "cursor",
            "transportPreference",
            "snapshotPageSize",
            "deltaPageSize",
            "longPollMs",
            "heartbeatTimeoutMs",
            "capacity",
            "reconnect",
          ]) {
            if (JSON.stringify(candidate[field]) !== JSON.stringify(current[field])) {
              throw new EventIngressError(
                IngressErrorCode.INVALID_CONFIGURATION,
                `Cannot change active task ingress option ${field}.`,
                {
                  context: {
                    taskId,
                    details: { field },
                  },
                },
              )
            }
          }
        }
      }
      return existing
    }
    const coordinator = new EventIngressCoordinator(taskId, this.#source, {
      cursor: normalizedCursor(options),
      transportPreference: options.transportPreference,
      snapshotPageSize: options.snapshotPageSize,
      deltaPageSize: options.deltaPageSize,
      longPollMs: options.longPollMs,
      heartbeatTimeoutMs: options.heartbeatTimeoutMs,
      capacity: options.capacity,
      reconnect: options.reconnect,
    })
    const record = {
      coordinator,
      optionsKey: key,
      createdAtMs: Date.now(),
    }
    this.#coordinators.set(taskId, record)
    return record
  }

  #assertAvailable(): void {
    if (this.#closed) {
      throw new EventIngressError(
        IngressErrorCode.CLOSED,
        "Task event transport is closed.",
      )
    }
    if (this.#disabled) {
      throw new EventIngressError(
        IngressErrorCode.DISABLED,
        "Task event transport is disabled.",
      )
    }
  }
}
