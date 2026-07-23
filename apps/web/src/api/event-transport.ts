import {
  BoundedIdentityWindow,
  CursorPollingLoop,
  normalizeIdentity,
  type EventProjection,
  type PollingSnapshot,
} from "../../../../packages/core/typed-api-client/src/index.ts"
import type { TaskApi } from "./task-api.ts"

export interface EventBatch {
  taskId: string
  events: EventProjection[]
  cursor?: string
  receivedAt: number
}

export class TaskEventTransport {
  readonly #api: TaskApi
  readonly #loops = new Map<string, CursorPollingLoop<EventProjection>>()
  readonly #windows = new Map<string, BoundedIdentityWindow>()
  readonly #listeners = new Map<string, Set<(batch: EventBatch) => void>>()

  constructor(api: TaskApi) {
    this.#api = api
  }

  subscribe(
    taskId: string,
    listener: (batch: EventBatch) => void,
    options: {
      after?: string
      intervalMs?: number
      idleIntervalMs?: number
      signal?: AbortSignal
    } = {},
  ): () => void {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const listeners = this.#listeners.get(normalizedTaskId) ?? new Set()
    listeners.add(listener)
    this.#listeners.set(normalizedTaskId, listeners)
    let loop = this.#loops.get(normalizedTaskId)
    if (!loop) {
      const window = new BoundedIdentityWindow(20_000)
      this.#windows.set(normalizedTaskId, window)
      loop = new CursorPollingLoop<EventProjection>({
        signal: options.signal,
        intervalMs: options.intervalMs ?? 250,
        idleIntervalMs: options.idleIntervalMs ?? 1_000,
        poll: async (cursor, signal) => {
          const events = await this.#api.events(normalizedTaskId, {
            after: cursor ?? options.after,
            limit: 1_000,
            signal,
          })
          const fresh = window.filter(events, (event) => event.eventId)
          return {
            items: fresh,
            cursor: events.at(-1)?.eventId ?? cursor ?? options.after,
            caughtUp: events.length < 1_000,
          }
        },
        deliver: (events, cursor) => {
          const batch: EventBatch = {
            taskId: normalizedTaskId,
            events: [...events],
            cursor,
            receivedAt: Date.now(),
          }
          for (const observer of this.#listeners.get(normalizedTaskId) ?? []) {
            try {
              observer(batch)
            } catch {
              // A console observer cannot interrupt transport progress.
            }
          }
        },
      })
      this.#loops.set(normalizedTaskId, loop)
      void loop.start(options.after).catch(() => {})
    }
    return () => {
      const current = this.#listeners.get(normalizedTaskId)
      current?.delete(listener)
      if (current?.size) return
      this.#listeners.delete(normalizedTaskId)
      this.stop(normalizedTaskId, "Last event subscriber removed.")
    }
  }

  stop(taskId: string, reason?: unknown): boolean {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const loop = this.#loops.get(normalizedTaskId)
    if (!loop) return false
    loop.stop(reason)
    this.#loops.delete(normalizedTaskId)
    this.#windows.delete(normalizedTaskId)
    return true
  }

  stopAll(reason?: unknown): number {
    let count = 0
    for (const taskId of [...this.#loops.keys()]) if (this.stop(taskId, reason)) count += 1
    this.#listeners.clear()
    return count
  }

  snapshot(taskId: string): PollingSnapshot | undefined {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    return this.#loops.get(normalizedTaskId)?.snapshot()
  }

  snapshots(): Record<string, PollingSnapshot> {
    return Object.fromEntries(
      [...this.#loops.entries()]
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([taskId, loop]) => [taskId, loop.snapshot()]),
    )
  }
}
