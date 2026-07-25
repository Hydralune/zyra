import type {
  CommandQueueSnapshot,
  CommandReceipt,
} from "./contracts.ts"
import type { ProjectedCommandLike } from "./queue.ts"

export type QueueRecoveryPhase =
  | "idle"
  | "restoring"
  | "live"
  | "degraded"
  | "offline"
  | "disabled"
  | "closed"

export type QueueRecoveryReason =
  | "route"
  | "projection"
  | "receipt"
  | "focus"
  | "visibility"
  | "online"
  | "manual"
  | "retry"

export interface QueueRecoverySnapshot {
  phase: QueueRecoveryPhase
  taskId?: string
  sessionId?: string
  generation: number
  revision: number
  attempts: number
  pendingReasons: readonly QueueRecoveryReason[]
  lastReason?: QueueRecoveryReason
  lastStartedAt?: number
  lastFinishedAt?: number
  lastSuccessfulAt?: number
  nextRetryAt?: number
  restored?: CommandQueueSnapshot
  lastError?: string
  online: boolean
  visible: boolean
  enabled: boolean
}

export interface QueueRecoveryAdapter {
  restore(input: {
    taskId: string
    sessionId: string
    includeTerminal: boolean
    signal: AbortSignal
  }): Promise<CommandQueueSnapshot>
  reconcile(input: {
    projections?: readonly ProjectedCommandLike[]
    receipts?: readonly CommandReceipt[]
    restored?: boolean
  }): CommandQueueSnapshot | undefined
  projections(taskId: string): readonly ProjectedCommandLike[]
}

export interface QueueRecoveryOptions {
  adapter: QueueRecoveryAdapter
  now?: () => number
  setTimer?: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>
  clearTimer?: (timer: ReturnType<typeof setTimeout>) => void
  minimumRetryMs?: number
  maximumRetryMs?: number
  staleAfterMs?: number
}

function errorMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : String(error || "Command queue restore failed.")
}

function boundedDelay(
  attempts: number,
  minimum: number,
  maximum: number,
): number {
  const exponent = Math.max(0, Math.min(16, attempts - 1))
  return Math.min(maximum, minimum * 2 ** exponent)
}

function uniqueReasons(
  values: Iterable<QueueRecoveryReason>,
): QueueRecoveryReason[] {
  return [...new Set(values)]
}

export class CommandQueueRecovery {
  readonly #adapter: QueueRecoveryAdapter
  readonly #now: () => number
  readonly #setTimer: (
    callback: () => void,
    delay: number,
  ) => ReturnType<typeof setTimeout>
  readonly #clearTimer: (timer: ReturnType<typeof setTimeout>) => void
  readonly #minimumRetryMs: number
  readonly #maximumRetryMs: number
  readonly #staleAfterMs: number
  readonly #listeners = new Set<() => void>()
  readonly #pendingReasons = new Set<QueueRecoveryReason>()
  #snapshot: QueueRecoverySnapshot
  #controller?: AbortController
  #inflight?: Promise<CommandQueueSnapshot | undefined>
  #retryTimer?: ReturnType<typeof setTimeout>
  #queuedRefresh = false
  #closed = false

  constructor(options: QueueRecoveryOptions) {
    this.#adapter = options.adapter
    this.#now = options.now ?? Date.now
    this.#setTimer = options.setTimer ?? setTimeout
    this.#clearTimer = options.clearTimer ?? clearTimeout
    this.#minimumRetryMs = Math.max(
      100,
      Math.min(60_000, Math.floor(options.minimumRetryMs ?? 500)),
    )
    this.#maximumRetryMs = Math.max(
      this.#minimumRetryMs,
      Math.min(300_000, Math.floor(options.maximumRetryMs ?? 30_000)),
    )
    this.#staleAfterMs = Math.max(
      1000,
      Math.min(3_600_000, Math.floor(options.staleAfterMs ?? 30_000)),
    )
    this.#snapshot = Object.freeze({
      phase: "idle",
      generation: 0,
      revision: 0,
      attempts: 0,
      pendingReasons: Object.freeze([]),
      online: true,
      visible: true,
      enabled: true,
    })
  }

  getSnapshot = (): QueueRecoverySnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  bind(taskId?: string, sessionId?: string): Promise<CommandQueueSnapshot | undefined> {
    this.#assertOpen()
    const normalizedTaskId = taskId?.trim() || undefined
    const normalizedSessionId =
      normalizedTaskId
        ? sessionId?.trim() || `task:${normalizedTaskId}`
        : undefined
    const unchanged =
      normalizedTaskId === this.#snapshot.taskId &&
      normalizedSessionId === this.#snapshot.sessionId
    if (unchanged && normalizedTaskId) {
      return this.refresh("route", { force: this.stale() })
    }
    this.#cancelInflight("Command queue route changed.")
    this.#clearRetry()
    this.#pendingReasons.clear()
    const generation = this.#snapshot.generation + 1
    if (!normalizedTaskId || !normalizedSessionId) {
      this.#replace({
        phase: "idle",
        taskId: undefined,
        sessionId: undefined,
        generation,
        attempts: 0,
        pendingReasons: Object.freeze([]),
        lastError: undefined,
        nextRetryAt: undefined,
        restored: undefined,
      })
      return Promise.resolve(undefined)
    }
    this.#replace({
      phase:
        this.#snapshot.enabled
          ? this.#snapshot.online
            ? "idle"
            : "offline"
          : "disabled",
      taskId: normalizedTaskId,
      sessionId: normalizedSessionId,
      generation,
      attempts: 0,
      pendingReasons: Object.freeze([]),
      lastError: undefined,
      nextRetryAt: undefined,
      restored: undefined,
    })
    return this.refresh("route", { force: true })
  }

  refresh(
    reason: QueueRecoveryReason = "manual",
    options: { force?: boolean; includeTerminal?: boolean } = {},
  ): Promise<CommandQueueSnapshot | undefined> {
    this.#assertAvailable()
    const taskId = this.#snapshot.taskId
    const sessionId = this.#snapshot.sessionId
    if (!taskId || !sessionId) return Promise.resolve(undefined)
    this.#pendingReasons.add(reason)
    this.#publishReasons(reason)
    if (!this.#snapshot.online) {
      this.#replace({ phase: "offline" })
      return Promise.resolve(this.#snapshot.restored)
    }
    if (
      !options.force &&
      !this.stale() &&
      this.#snapshot.restored &&
      reason !== "receipt"
    ) {
      this.#reconcileProjection(taskId)
      this.#pendingReasons.delete(reason)
      this.#publishReasons(reason)
      return Promise.resolve(this.#snapshot.restored)
    }
    if (this.#inflight) {
      this.#queuedRefresh = true
      return this.#inflight
    }
    const generation = this.#snapshot.generation
    const controller = new AbortController()
    this.#controller = controller
    const startedAt = this.#now()
    this.#replace({
      phase: "restoring",
      lastReason: reason,
      lastStartedAt: startedAt,
      lastError: undefined,
      nextRetryAt: undefined,
    })
    const promise = this.#adapter.restore({
      taskId,
      sessionId,
      includeTerminal: options.includeTerminal ?? true,
      signal: controller.signal,
    }).then((snapshot) => {
      if (
        this.#closed ||
        controller.signal.aborted ||
        generation !== this.#snapshot.generation ||
        taskId !== this.#snapshot.taskId
      ) {
        return undefined
      }
      const projection = this.#adapter.projections(taskId)
      const reconciled = this.#adapter.reconcile({
        projections: projection,
        restored: true,
      }) ?? snapshot
      const now = this.#now()
      this.#clearRetry()
      this.#replace({
        phase: "live",
        attempts: 0,
        restored: reconciled,
        lastFinishedAt: now,
        lastSuccessfulAt: now,
        lastError: undefined,
        nextRetryAt: undefined,
      })
      return reconciled
    }).catch((error) => {
      if (
        this.#closed ||
        controller.signal.aborted ||
        generation !== this.#snapshot.generation
      ) {
        return undefined
      }
      const attempts = this.#snapshot.attempts + 1
      const now = this.#now()
      this.#replace({
        phase: "degraded",
        attempts,
        lastFinishedAt: now,
        lastError: errorMessage(error),
      })
      this.#scheduleRetry(attempts)
      throw error
    }).finally(() => {
      if (this.#controller === controller) this.#controller = undefined
      if (this.#inflight === promise) this.#inflight = undefined
      this.#pendingReasons.clear()
      this.#publishReasons(reason)
      if (this.#queuedRefresh && !this.#closed) {
        this.#queuedRefresh = false
        void this.refresh("retry", { force: true }).catch(() => undefined)
      }
    })
    this.#inflight = promise
    return promise
  }

  projectionChanged(): CommandQueueSnapshot | undefined {
    this.#assertAvailable()
    const taskId = this.#snapshot.taskId
    if (!taskId) return undefined
    this.#pendingReasons.add("projection")
    const snapshot = this.#reconcileProjection(taskId)
    this.#pendingReasons.delete("projection")
    this.#replace({
      phase:
        this.#snapshot.phase === "restoring"
          ? "restoring"
          : this.#snapshot.online
            ? "live"
            : "offline",
      restored: snapshot ?? this.#snapshot.restored,
      lastReason: "projection",
      lastFinishedAt: this.#now(),
      pendingReasons: Object.freeze(uniqueReasons(this.#pendingReasons)),
    })
    return snapshot
  }

  receipt(receipt: CommandReceipt): CommandQueueSnapshot | undefined {
    this.#assertAvailable()
    if (receipt.taskId !== this.#snapshot.taskId) return undefined
    const snapshot = this.#adapter.reconcile({
      receipts: [receipt],
      projections: this.#adapter.projections(receipt.taskId),
    })
    this.#replace({
      restored: snapshot ?? this.#snapshot.restored,
      lastReason: "receipt",
      lastFinishedAt: this.#now(),
    })
    if (receipt.phase === "queued") {
      void this.refresh("receipt", { force: true }).catch(() => undefined)
    }
    return snapshot
  }

  setOnline(online: boolean): void {
    this.#assertOpen()
    if (online === this.#snapshot.online) return
    if (!online) {
      this.#cancelInflight("Browser transport is offline.")
      this.#clearRetry()
      this.#replace({
        online: false,
        phase: this.#snapshot.enabled ? "offline" : "disabled",
        nextRetryAt: undefined,
      })
      return
    }
    this.#replace({
      online: true,
      phase: this.#snapshot.enabled ? "idle" : "disabled",
    })
    if (this.#snapshot.enabled && this.#snapshot.taskId) {
      void this.refresh("online", { force: true }).catch(() => undefined)
    }
  }

  setVisible(visible: boolean): void {
    this.#assertOpen()
    if (visible === this.#snapshot.visible) return
    this.#replace({ visible })
    if (
      visible &&
      this.#snapshot.enabled &&
      this.#snapshot.online &&
      this.#snapshot.taskId &&
      this.stale()
    ) {
      void this.refresh("visibility", { force: true }).catch(() => undefined)
    }
  }

  focus(): void {
    this.#assertAvailable()
    if (
      this.#snapshot.taskId &&
      this.#snapshot.online &&
      this.stale()
    ) {
      void this.refresh("focus", { force: true }).catch(() => undefined)
    }
  }

  stale(at = this.#now()): boolean {
    const successful = this.#snapshot.lastSuccessfulAt
    return successful === undefined || at - successful >= this.#staleAfterMs
  }

  disable(reason = "Command queue recovery disabled."): void {
    if (this.#closed || !this.#snapshot.enabled) return
    this.#cancelInflight(reason)
    this.#clearRetry()
    this.#replace({
      enabled: false,
      phase: "disabled",
      lastError: reason,
      nextRetryAt: undefined,
    })
  }

  enable(): void {
    this.#assertOpen()
    if (this.#snapshot.enabled) return
    this.#replace({
      enabled: true,
      phase: this.#snapshot.online ? "idle" : "offline",
      lastError: undefined,
    })
    if (this.#snapshot.online && this.#snapshot.taskId) {
      void this.refresh("retry", { force: true }).catch(() => undefined)
    }
  }

  close(reason = "Command queue recovery closed."): void {
    if (this.#closed) return
    this.#closed = true
    this.#cancelInflight(reason)
    this.#clearRetry()
    this.#pendingReasons.clear()
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      phase: "closed",
      enabled: false,
      pendingReasons: Object.freeze([]),
      nextRetryAt: undefined,
      revision: this.#snapshot.revision + 1,
    })
    this.#listeners.clear()
  }

  #reconcileProjection(taskId: string): CommandQueueSnapshot | undefined {
    return this.#adapter.reconcile({
      projections: this.#adapter.projections(taskId),
      restored: Boolean(this.#snapshot.restored?.restored),
    })
  }

  #scheduleRetry(attempts: number): void {
    this.#clearRetry()
    if (
      !this.#snapshot.enabled ||
      !this.#snapshot.online ||
      !this.#snapshot.taskId
    ) {
      return
    }
    const delay = boundedDelay(
      attempts,
      this.#minimumRetryMs,
      this.#maximumRetryMs,
    )
    const generation = this.#snapshot.generation
    this.#replace({ nextRetryAt: this.#now() + delay })
    this.#retryTimer = this.#setTimer(() => {
      this.#retryTimer = undefined
      if (
        this.#closed ||
        generation !== this.#snapshot.generation ||
        !this.#snapshot.enabled ||
        !this.#snapshot.online
      ) {
        return
      }
      void this.refresh("retry", { force: true }).catch(() => undefined)
    }, delay)
  }

  #clearRetry(): void {
    if (!this.#retryTimer) return
    this.#clearTimer(this.#retryTimer)
    this.#retryTimer = undefined
  }

  #cancelInflight(reason: string): void {
    if (this.#controller && !this.#controller.signal.aborted) {
      this.#controller.abort(reason)
    }
    this.#controller = undefined
    this.#inflight = undefined
    this.#queuedRefresh = false
  }

  #publishReasons(reason: QueueRecoveryReason): void {
    this.#replace({
      lastReason: reason,
      pendingReasons: Object.freeze(uniqueReasons(this.#pendingReasons)),
    })
  }

  #replace(patch: Partial<QueueRecoverySnapshot>): void {
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of [...this.#listeners]) {
      try {
        listener()
      } catch {
        // Recovery observers cannot alter queue truth or retry decisions.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Command queue recovery is closed.")
  }

  #assertAvailable(): void {
    this.#assertOpen()
    if (!this.#snapshot.enabled) {
      throw new TypeError(
        this.#snapshot.lastError || "Command queue recovery is disabled.",
      )
    }
  }
}
