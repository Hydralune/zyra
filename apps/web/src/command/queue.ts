export type QueuePriority = "now" | "next" | "later"
export type QueuePhase = "queued" | "dispatching" | "committed" | "failed" | "cancelled"
export type QueueOrigin = "keyboard" | "button" | "overlay" | "recovery"

export interface QueuedSubmission {
  id: string
  value: string
  priority: QueuePriority
  phase: QueuePhase
  origin: QueueOrigin
  createdAt: number
  updatedAt: number
  taskId?: string
  editable: boolean
  visible: boolean
  remoteSafe: boolean
  attempts: number
  receiptId?: string
  error?: string
}

export interface QueueSnapshot {
  entries: readonly QueuedSubmission[]
  visible: readonly QueuedSubmission[]
  pendingCount: number
  dispatchingCount: number
  revision: number
}

export interface EnqueueInput {
  id?: string
  value: string
  priority?: QueuePriority
  origin?: QueueOrigin
  taskId?: string
  editable?: boolean
  visible?: boolean
  remoteSafe?: boolean
}

const PRIORITY: Record<QueuePriority, number> = {
  now: 0,
  next: 1,
  later: 2,
}

function queueId(): string {
  const token =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID().replace(/-/g, "")
      : `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`
  return `queue_${token.slice(0, 24)}`
}

function cloneEntry(entry: QueuedSubmission): QueuedSubmission {
  return { ...entry }
}

function normalizedValue(value: string): string {
  const normalized = value.trim()
  if (!normalized) throw new TypeError("Queued command must not be empty.")
  if (new TextEncoder().encode(normalized).byteLength > 256 * 1024) {
    throw new TypeError("Queued command exceeds 256 KiB.")
  }
  return normalized
}

export class CommandQueue {
  readonly #entries: QueuedSubmission[] = []
  readonly #listeners = new Set<() => void>()
  #snapshot: QueueSnapshot = Object.freeze({
    entries: Object.freeze([]),
    visible: Object.freeze([]),
    pendingCount: 0,
    dispatchingCount: 0,
    revision: 0,
  })
  #revision = 0
  #closed = false

  getSnapshot = (): QueueSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  enqueue(input: EnqueueInput): QueuedSubmission {
    this.#assertOpen()
    const value = normalizedValue(input.value)
    const id = input.id ?? queueId()
    if (this.#entries.some((entry) => entry.id === id)) {
      throw new TypeError(`Queue identity is already active: ${id}`)
    }
    const now = Date.now()
    const entry: QueuedSubmission = {
      id,
      value,
      priority: input.priority ?? "next",
      phase: "queued",
      origin: input.origin ?? "keyboard",
      createdAt: now,
      updatedAt: now,
      taskId: input.taskId,
      editable: input.editable ?? true,
      visible: input.visible ?? true,
      remoteSafe: input.remoteSafe ?? false,
      attempts: 0,
    }
    this.#entries.push(entry)
    this.#publish()
    return cloneEntry(entry)
  }

  peek(filter?: (entry: QueuedSubmission) => boolean): QueuedSubmission | undefined {
    const index = this.#bestIndex(filter)
    return index < 0 ? undefined : cloneEntry(this.#entries[index]!)
  }

  reserve(filter?: (entry: QueuedSubmission) => boolean): QueuedSubmission | undefined {
    this.#assertOpen()
    const index = this.#bestIndex((entry) => {
      if (entry.phase !== "queued") return false
      return filter ? filter(entry) : true
    })
    if (index < 0) return undefined
    const entry = this.#entries[index]!
    entry.phase = "dispatching"
    entry.attempts += 1
    entry.updatedAt = Date.now()
    this.#publish()
    return cloneEntry(entry)
  }

  commit(id: string, receiptId?: string): QueuedSubmission | undefined {
    return this.#settle(id, "committed", { receiptId })
  }

  fail(id: string, error: unknown): QueuedSubmission | undefined {
    const message = error instanceof Error ? error.message : String(error)
    return this.#settle(id, "failed", { error: message || "Command failed." })
  }

  cancel(id: string, reason = "Command cancelled."): QueuedSubmission | undefined {
    return this.#settle(id, "cancelled", { error: reason })
  }

  retry(id: string, options: { priority?: QueuePriority } = {}): QueuedSubmission | undefined {
    this.#assertOpen()
    const entry = this.#entries.find((candidate) => candidate.id === id)
    if (!entry || (entry.phase !== "failed" && entry.phase !== "cancelled")) return undefined
    entry.phase = "queued"
    entry.priority = options.priority ?? "now"
    entry.error = undefined
    entry.receiptId = undefined
    entry.updatedAt = Date.now()
    this.#publish()
    return cloneEntry(entry)
  }

  remove(id: string, options: { allowPending?: boolean } = {}): QueuedSubmission | undefined {
    this.#assertOpen()
    const index = this.#entries.findIndex((entry) => entry.id === id)
    if (index < 0) return undefined
    const entry = this.#entries[index]!
    if (
      !options.allowPending &&
      (entry.phase === "queued" || entry.phase === "dispatching")
    ) {
      return undefined
    }
    this.#entries.splice(index, 1)
    this.#publish()
    return cloneEntry(entry)
  }

  removeSettled(options: { olderThanMs?: number } = {}): QueuedSubmission[] {
    this.#assertOpen()
    const threshold = Date.now() - Math.max(0, options.olderThanMs ?? 0)
    const removed: QueuedSubmission[] = []
    for (let index = this.#entries.length - 1; index >= 0; index -= 1) {
      const entry = this.#entries[index]!
      if (
        (entry.phase === "committed" || entry.phase === "failed" || entry.phase === "cancelled") &&
        entry.updatedAt <= threshold
      ) {
        removed.unshift(cloneEntry(entry))
        this.#entries.splice(index, 1)
      }
    }
    if (removed.length) this.#publish()
    return removed
  }

  edit(id: string, value: string): QueuedSubmission | undefined {
    this.#assertOpen()
    const entry = this.#entries.find((candidate) => candidate.id === id)
    if (!entry || entry.phase !== "queued" || !entry.editable) return undefined
    entry.value = normalizedValue(value)
    entry.updatedAt = Date.now()
    this.#publish()
    return cloneEntry(entry)
  }

  move(id: string, priority: QueuePriority): QueuedSubmission | undefined {
    this.#assertOpen()
    const entry = this.#entries.find((candidate) => candidate.id === id)
    if (!entry || entry.phase !== "queued") return undefined
    entry.priority = priority
    entry.updatedAt = Date.now()
    this.#publish()
    return cloneEntry(entry)
  }

  popEditable(currentValue: string, cursor: number): {
    value: string
    cursor: number
    removed: QueuedSubmission[]
  } | undefined {
    this.#assertOpen()
    const editable = this.#entries.filter(
      (entry) => entry.phase === "queued" && entry.editable,
    )
    if (!editable.length) return undefined
    const ordered = this.#ordered(editable)
    const values = ordered.map((entry) => entry.value)
    const prefix = values.join("\n")
    const value = [...values, currentValue].filter(Boolean).join("\n")
    const nextCursor = prefix.length + (prefix && currentValue ? 1 : 0) + cursor
    const ids = new Set(ordered.map((entry) => entry.id))
    for (let index = this.#entries.length - 1; index >= 0; index -= 1) {
      if (ids.has(this.#entries[index]!.id)) this.#entries.splice(index, 1)
    }
    this.#publish()
    return { value, cursor: nextCursor, removed: ordered.map(cloneEntry) }
  }

  list(options: {
    phases?: readonly QueuePhase[]
    taskId?: string
    visibleOnly?: boolean
    limit?: number
  } = {}): QueuedSubmission[] {
    const phaseSet = options.phases ? new Set(options.phases) : undefined
    const values = this.#entries.filter((entry) => {
      if (phaseSet && !phaseSet.has(entry.phase)) return false
      if (options.taskId && entry.taskId !== options.taskId) return false
      if (options.visibleOnly && !entry.visible) return false
      return true
    })
    const ordered = this.#ordered(values)
    const limit = Math.max(1, Math.min(500, Math.floor(options.limit ?? 500)))
    return ordered.slice(0, limit).map(cloneEntry)
  }

  pending(taskId?: string): QueuedSubmission[] {
    return this.list({
      phases: ["queued", "dispatching"],
      taskId,
    })
  }

  containsDuplicate(value: string, taskId?: string): QueuedSubmission | undefined {
    const normalized = value.trim()
    const entry = this.#entries.find(
      (candidate) =>
        candidate.value === normalized &&
        candidate.taskId === taskId &&
        (candidate.phase === "queued" || candidate.phase === "dispatching"),
    )
    return entry ? cloneEntry(entry) : undefined
  }

  clear(options: { includeDispatching?: boolean; reason?: string } = {}): QueuedSubmission[] {
    this.#assertOpen()
    const removed: QueuedSubmission[] = []
    for (let index = this.#entries.length - 1; index >= 0; index -= 1) {
      const entry = this.#entries[index]!
      if (entry.phase === "dispatching" && !options.includeDispatching) continue
      removed.unshift(cloneEntry(entry))
      this.#entries.splice(index, 1)
    }
    if (removed.length) this.#publish()
    return removed
  }

  close(reason = "Command queue closed."): void {
    if (this.#closed) return
    this.#closed = true
    for (const entry of this.#entries) {
      if (entry.phase === "queued" || entry.phase === "dispatching") {
        entry.phase = "cancelled"
        entry.error = reason
        entry.updatedAt = Date.now()
      }
    }
    this.#publish()
    this.#listeners.clear()
  }

  #settle(
    id: string,
    phase: Extract<QueuePhase, "committed" | "failed" | "cancelled">,
    patch: { receiptId?: string; error?: string },
  ): QueuedSubmission | undefined {
    this.#assertOpen()
    const entry = this.#entries.find((candidate) => candidate.id === id)
    if (!entry || (entry.phase !== "dispatching" && entry.phase !== "queued")) return undefined
    entry.phase = phase
    entry.receiptId = patch.receiptId
    entry.error = patch.error
    entry.updatedAt = Date.now()
    this.#publish()
    return cloneEntry(entry)
  }

  #bestIndex(filter?: (entry: QueuedSubmission) => boolean): number {
    let bestIndex = -1
    let bestPriority = Number.POSITIVE_INFINITY
    let bestCreatedAt = Number.POSITIVE_INFINITY
    for (let index = 0; index < this.#entries.length; index += 1) {
      const entry = this.#entries[index]!
      if (filter && !filter(entry)) continue
      const priority = PRIORITY[entry.priority]
      if (
        priority < bestPriority ||
        (priority === bestPriority && entry.createdAt < bestCreatedAt)
      ) {
        bestIndex = index
        bestPriority = priority
        bestCreatedAt = entry.createdAt
      }
    }
    return bestIndex
  }

  #ordered(values: readonly QueuedSubmission[]): QueuedSubmission[] {
    return [...values].sort((left, right) => {
      const priority = PRIORITY[left.priority] - PRIORITY[right.priority]
      if (priority) return priority
      return left.createdAt - right.createdAt
    })
  }

  #publish(): void {
    this.#revision += 1
    const entries = Object.freeze(this.#entries.map(cloneEntry))
    const visible = Object.freeze(
      this.#ordered(this.#entries.filter((entry) => entry.visible))
        .slice(0, 100)
        .map(cloneEntry),
    )
    this.#snapshot = Object.freeze({
      entries,
      visible,
      pendingCount: entries.filter(
        (entry) => entry.phase === "queued" || entry.phase === "dispatching",
      ).length,
      dispatchingCount: entries.filter((entry) => entry.phase === "dispatching").length,
      revision: this.#revision,
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // Queue observers cannot mutate queue semantics.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Command queue is closed.")
  }
}
