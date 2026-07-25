import type {
  CommandDeliveryMode,
  CommandQueueItem,
  CommandReceipt,
} from "./contracts.ts"

export type QueueActionKind = "cancel" | "retry"
export type QueueActionPhase =
  | "pending"
  | "applied"
  | "rejected"
  | "cancelled"
  | "expired"

export interface QueueActionRecord {
  id: string
  kind: QueueActionKind
  queueId: string
  requestId?: string
  commandId?: string
  taskId?: string
  phase: QueueActionPhase
  createdAt: number
  updatedAt: number
  attempt: number
  reason?: string
  mode?: CommandDeliveryMode
  receipt?: CommandReceipt
  error?: string
}

export interface QueueActionSnapshot {
  enabled: boolean
  busy: boolean
  revision: number
  active: readonly QueueActionRecord[]
  recent: readonly QueueActionRecord[]
  lastError?: string
}

export interface QueueActionAdapter {
  cancel(item: CommandQueueItem, reason?: string): Promise<CommandReceipt>
  retry(item: CommandQueueItem, mode?: CommandDeliveryMode): Promise<CommandReceipt>
  refresh(taskId?: string): Promise<unknown>
}

function actionIdentity(kind: QueueActionKind, item: CommandQueueItem): string {
  return [
    "queue-action",
    kind,
    item.taskId ?? "unknown-task",
    item.requestId ?? item.commandId ?? item.queueId,
  ].join(":")
}

function copyRecord(record: QueueActionRecord): QueueActionRecord {
  return {
    ...record,
    receipt: record.receipt
      ? {
          ...record.receipt,
          data: { ...record.receipt.data },
          eventIds: [...record.receipt.eventIds],
          raw: { ...record.receipt.raw },
        }
      : undefined,
  }
}

function message(error: unknown): string {
  return error instanceof Error
    ? error.message
    : String(error || "Command queue action failed.")
}

function phase(receipt: CommandReceipt): QueueActionPhase {
  if (receipt.phase === "expired") return "expired"
  if (receipt.phase === "cancelled") return "cancelled"
  if (receipt.phase === "rejected") return "rejected"
  return "applied"
}

export class CommandQueueActions {
  readonly #adapter: QueueActionAdapter
  readonly #now: () => number
  readonly #listeners = new Set<() => void>()
  readonly #records = new Map<string, QueueActionRecord>()
  readonly #inflight = new Map<string, Promise<CommandReceipt>>()
  #snapshot: QueueActionSnapshot = Object.freeze({
    enabled: true,
    busy: false,
    revision: 0,
    active: Object.freeze([]),
    recent: Object.freeze([]),
  })
  #closed = false
  #disabledReason = "Command queue actions are disabled."

  constructor(
    adapter: QueueActionAdapter,
    options: { now?: () => number } = {},
  ) {
    this.#adapter = adapter
    this.#now = options.now ?? Date.now
  }

  getSnapshot = (): QueueActionSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  cancel(
    item: CommandQueueItem,
    reason = "Cancelled from the Zyra command queue preview.",
  ): Promise<CommandReceipt> {
    this.#assertAvailable()
    if (item.phase !== "queued" || !item.cancellable) {
      throw new TypeError("Queue item is not cancellable.")
    }
    if (!item.requestId || !item.taskId) {
      throw new TypeError(
        "Queue cancellation requires backend request and task identities.",
      )
    }
    const normalizedReason = reason.trim()
    if (!normalizedReason) {
      throw new TypeError("Queue cancellation requires a reason.")
    }
    return this.#run("cancel", item, {
      reason: normalizedReason,
      execute: () => this.#adapter.cancel(item, normalizedReason),
    })
  }

  retry(
    item: CommandQueueItem,
    mode: CommandDeliveryMode = "enqueue",
  ): Promise<CommandReceipt> {
    this.#assertAvailable()
    if (!item.terminal || !item.retryable) {
      throw new TypeError("Queue item is not retryable.")
    }
    if (!item.requestId || !item.taskId) {
      throw new TypeError(
        "Queue retry requires backend request and task identities.",
      )
    }
    if (!item.text && !item.name) {
      throw new TypeError("Queue retry requires canonical command text.")
    }
    return this.#run("retry", item, {
      mode,
      execute: () => this.#adapter.retry(item, mode),
    })
  }

  find(kind: QueueActionKind, item: CommandQueueItem): QueueActionRecord | undefined {
    const selected = this.#records.get(actionIdentity(kind, item))
    return selected ? copyRecord(selected) : undefined
  }

  records(options: {
    phases?: readonly QueueActionPhase[]
    taskId?: string
    limit?: number
  } = {}): QueueActionRecord[] {
    const phases = new Set(options.phases ?? [])
    const limit = Math.max(1, Math.min(1000, Math.floor(options.limit ?? 100)))
    return [...this.#records.values()]
      .filter((record) => !phases.size || phases.has(record.phase))
      .filter((record) => !options.taskId || record.taskId === options.taskId)
      .sort((left, right) => {
        const updated = right.updatedAt - left.updatedAt
        return updated || right.id.localeCompare(left.id)
      })
      .slice(0, limit)
      .map(copyRecord)
  }

  prune(options: { olderThanMs?: number; maximum?: number } = {}): number {
    this.#assertOpen()
    const cutoff = this.#now() - Math.max(0, options.olderThanMs ?? 3_600_000)
    const maximum = Math.max(1, Math.min(10_000, options.maximum ?? 500))
    const settled = this.records({
      phases: ["applied", "rejected", "cancelled", "expired"],
      limit: 10_000,
    })
    const remove = new Set(
      settled
        .filter((record, index) => index >= maximum || record.updatedAt < cutoff)
        .map((record) => record.id),
    )
    for (const id of remove) this.#records.delete(id)
    if (remove.size) this.#publish()
    return remove.size
  }

  disable(reason = "Command queue actions are disabled."): void {
    if (this.#closed || !this.#snapshot.enabled) return
    this.#disabledReason = reason.trim() || "Command queue actions are disabled."
    this.#replace({
      enabled: false,
      lastError: this.#disabledReason,
    })
  }

  enable(): void {
    this.#assertOpen()
    if (this.#snapshot.enabled) return
    this.#replace({
      enabled: true,
      lastError: undefined,
    })
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    this.#listeners.clear()
    this.#inflight.clear()
    this.#records.clear()
  }

  #run(
    kind: QueueActionKind,
    item: CommandQueueItem,
    input: {
      reason?: string
      mode?: CommandDeliveryMode
      execute(): Promise<CommandReceipt>
    },
  ): Promise<CommandReceipt> {
    const id = actionIdentity(kind, item)
    const existing = this.#inflight.get(id)
    if (existing) return existing
    const prior = this.#records.get(id)
    if (
      prior?.phase === "applied" &&
      prior.receipt &&
      (
        kind === "cancel" ||
        prior.receipt.phase === "queued" ||
        prior.receipt.phase === "running" ||
        prior.receipt.phase === "applied"
      )
    ) {
      return Promise.resolve(prior.receipt)
    }
    const now = this.#now()
    const record: QueueActionRecord = {
      id,
      kind,
      queueId: item.queueId,
      requestId: item.requestId,
      commandId: item.commandId,
      taskId: item.taskId,
      phase: "pending",
      createdAt: prior?.createdAt ?? now,
      updatedAt: now,
      attempt: (prior?.attempt ?? 0) + 1,
      reason: input.reason,
      mode: input.mode,
    }
    this.#records.set(id, record)
    this.#publish(record)
    const promise = input.execute()
      .then(async (receipt) => {
        record.phase = phase(receipt)
        record.receipt = receipt
        record.updatedAt = this.#now()
        record.error = receipt.error?.message
        this.#publish(record)
        try {
          await this.#adapter.refresh(item.taskId)
        } catch (error) {
          record.error =
            `${record.error ? `${record.error} ` : ""}` +
            `Queue refresh failed: ${message(error)}`
          record.updatedAt = this.#now()
          this.#publish(record)
        }
        return receipt
      })
      .catch((error) => {
        record.phase = "rejected"
        record.error = message(error)
        record.updatedAt = this.#now()
        this.#publish(record)
        throw error
      })
      .finally(() => {
        if (this.#inflight.get(id) === promise) this.#inflight.delete(id)
        this.#publish()
      })
    this.#inflight.set(id, promise)
    return promise
  }

  #publish(active?: QueueActionRecord): void {
    this.#replace({
      busy: this.#inflight.size > 0 ||
        [...this.#records.values()].some((record) => record.phase === "pending"),
      lastError: active?.error ?? this.#snapshot.lastError,
    })
  }

  #replace(patch: Partial<QueueActionSnapshot>): void {
    const records = [...this.#records.values()]
      .sort((left, right) => right.updatedAt - left.updatedAt)
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      active: Object.freeze(
        records.filter((record) => record.phase === "pending").map(copyRecord),
      ),
      recent: Object.freeze(records.slice(0, 100).map(copyRecord)),
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of [...this.#listeners]) {
      try {
        listener()
      } catch {
        // Queue action observers cannot change backend action settlement.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Command queue actions are closed.")
  }

  #assertAvailable(): void {
    this.#assertOpen()
    if (!this.#snapshot.enabled) throw new TypeError(this.#disabledReason)
  }
}
