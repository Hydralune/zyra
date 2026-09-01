import type { WorkbenchController } from "./workbench-controller.ts"
import type {
  ConnectionPhaseValue,
  ConnectionSnapshot,
  IngressBatch,
  IngressLiveFrame,
  TransportKindValue,
} from "../events/ingress/index.ts"

export type TaskLiveSyncReason =
  | "bind"
  | "interval"
  | "resume"
  | "manual"
  | "ingress"

export interface ProductAssistantStreamSnapshot {
  messageId: string
  streamId: string
  text: string
  generation: number
  firstLiveSequence?: number
  lastLiveSequence?: number
  partial: boolean
  truncated: boolean
  settling: boolean
  startedAt: number
  updatedAt: number
}

export interface TaskLiveSyncSnapshot {
  /** Task the sync loop is currently bound to. */
  taskId?: string
  /** The bound task is non-terminal, so the surface is expected to move. */
  live: boolean
  /** Live, but the tab is hidden or the browser reports no connection. */
  paused: boolean
  /** A background refresh is in flight right now. */
  syncing: boolean
  lastSyncedAt?: number
  nextSyncAt?: number
  consecutiveFailures: number
  /** Bounded transient text; canonical task metadata remains final owner. */
  assistant?: ProductAssistantStreamSnapshot
  ingressPhase?: ConnectionPhaseValue
  ingressGeneration?: number
  ingressTransport?: TransportKindValue
  revision: number
}

export interface TaskLiveSyncEnvironment {
  now(): number
  setTimeout(callback: () => void, delayMs: number): unknown
  clearTimeout(handle: unknown): void
  /** The document is not visible, so polling wastes the user's connection. */
  hidden(): boolean
  online(): boolean
  /** Notifies when `hidden()` or `online()` may have changed. */
  listen(listener: () => void): () => void
}

export interface TaskLiveSyncOptions {
  environment?: TaskLiveSyncEnvironment
  /** Refresh cadence while the backend reports the run as active. */
  activeIntervalMs?: number
  /** Refresh cadence while the run exists but has not started moving. */
  pendingIntervalMs?: number
  maximumBackoffMs?: number
  /** Upper bound for transient text retained in browser memory. */
  assistantTextBytes?: number
}

const DEFAULT_ACTIVE_INTERVAL_MS = 2_500
const DEFAULT_PENDING_INTERVAL_MS = 6_000
const DEFAULT_MAXIMUM_BACKOFF_MS = 30_000
const DEFAULT_ASSISTANT_TEXT_BYTES = 512 * 1024
const INGRESS_REFRESH_DELAY_MS = 75

const textEncoder = new TextEncoder()

function boundedTextBytes(value: number | undefined): number {
  if (value === undefined || !Number.isFinite(value)) {
    return DEFAULT_ASSISTANT_TEXT_BYTES
  }
  return Math.max(16 * 1024, Math.min(4 * 1024 * 1024, Math.floor(value)))
}

function boundedUtf8Tail(value: string, maximumBytes: number): {
  text: string
  truncated: boolean
} {
  if (textEncoder.encode(value).byteLength <= maximumBytes) {
    return { text: value, truncated: false }
  }
  let low = 0
  let high = value.length
  while (low < high) {
    const middle = Math.floor((low + high) / 2)
    if (textEncoder.encode(value.slice(middle)).byteLength <= maximumBytes) {
      high = middle
    } else {
      low = middle + 1
    }
  }
  let start = low
  if (
    start > 0
    && start < value.length
    && /[\uDC00-\uDFFF]/.test(value[start] ?? "")
  ) start += 1
  return { text: value.slice(start), truncated: true }
}

function presentationText(
  presentation: Readonly<Record<string, unknown>>,
  key: "identity" | "streamId" | "text",
): string | undefined {
  const value = presentation[key]
  return typeof value === "string" ? value : undefined
}

function boundedInterval(value: number | undefined, fallback: number): number {
  if (value === undefined || !Number.isFinite(value)) return fallback
  return Math.max(500, Math.min(120_000, Math.floor(value)))
}

export function browserTaskLiveSyncEnvironment(): TaskLiveSyncEnvironment {
  return {
    now: () => Date.now(),
    setTimeout: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
    clearTimeout: (handle) =>
      globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
    hidden: () =>
      typeof document !== "undefined" && document.visibilityState === "hidden",
    online: () => (typeof navigator === "undefined" ? true : navigator.onLine),
    listen: (listener) => {
      if (typeof window === "undefined") return () => {}
      document.addEventListener("visibilitychange", listener)
      window.addEventListener("online", listener)
      window.addEventListener("offline", listener)
      return () => {
        document.removeEventListener("visibilitychange", listener)
        window.removeEventListener("online", listener)
        window.removeEventListener("offline", listener)
      }
    },
  }
}

interface SyncPlan {
  live: boolean
  paused: boolean
  delayMs?: number
}

/**
 * Keeps the task projection that the conversation renders in step with the
 * backend while a run is still moving.
 *
 * The canonical projection owner stays `WorkbenchController`; this runtime only
 * decides *when* to ask it for a fresh read.  It stops as soon as the backend
 * reports a terminal, non-active task, so a finished conversation costs nothing.
 */
export class TaskLiveSync {
  readonly #workbench: WorkbenchController
  readonly #environment: TaskLiveSyncEnvironment
  readonly #activeIntervalMs: number
  readonly #pendingIntervalMs: number
  readonly #maximumBackoffMs: number
  readonly #assistantTextBytes: number
  readonly #listeners = new Set<() => void>()
  readonly #unlistenEnvironment: () => void
  readonly #unsubscribeWorkbench: () => void
  #snapshot: TaskLiveSyncSnapshot = Object.freeze({
    live: false,
    paused: false,
    syncing: false,
    consecutiveFailures: 0,
    revision: 0,
  })
  #timer?: unknown
  #ingressTimer?: unknown
  #taskId?: string
  #failures = 0
  #inFlight = false
  #ingressRefreshPending = false
  #ingressGeneration = 0
  #planSignature = ""
  #closed = false

  constructor(workbench: WorkbenchController, options: TaskLiveSyncOptions = {}) {
    this.#workbench = workbench
    this.#environment = options.environment ?? browserTaskLiveSyncEnvironment()
    this.#activeIntervalMs = boundedInterval(
      options.activeIntervalMs,
      DEFAULT_ACTIVE_INTERVAL_MS,
    )
    this.#pendingIntervalMs = boundedInterval(
      options.pendingIntervalMs,
      DEFAULT_PENDING_INTERVAL_MS,
    )
    this.#maximumBackoffMs = boundedInterval(
      options.maximumBackoffMs,
      DEFAULT_MAXIMUM_BACKOFF_MS,
    )
    this.#assistantTextBytes = boundedTextBytes(options.assistantTextBytes)
    this.#unlistenEnvironment = this.#environment.listen(() => {
      if (this.#closed) return
      const plan = this.#plan()
      // Coming back from a hidden tab or a dropped connection should show the
      // current truth immediately rather than after the next tick.
      if (plan.live && !plan.paused && this.#snapshot.paused) {
        void this.#sync("resume")
        return
      }
      this.#reschedule()
    })
    this.#unsubscribeWorkbench = workbench.subscribe(() => {
      if (this.#closed) return
      this.#reconcileCanonicalTask()
      if (this.#inFlight) return
      if (this.#signature() === this.#planSignature) return
      this.#reschedule()
    })
  }

  getSnapshot = (): TaskLiveSyncSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  /** Binds the loop to a task, or unbinds it when `taskId` is undefined. */
  bind(taskId: string | undefined): void {
    if (this.#closed) return
    const normalized = taskId?.trim() || undefined
    if (normalized === this.#taskId) {
      this.#reschedule()
      return
    }
    this.#taskId = normalized
    this.#failures = 0
    this.#clearTimer()
    this.#clearIngressTimer()
    this.#ingressRefreshPending = false
    this.#ingressGeneration = 0
    this.#publish({
      taskId: normalized,
      consecutiveFailures: 0,
      nextSyncAt: undefined,
      lastSyncedAt: undefined,
      assistant: undefined,
      ingressPhase: undefined,
      ingressGeneration: undefined,
      ingressTransport: undefined,
    })
    this.#reschedule()
  }

  /** Refreshes now, regardless of the pending interval. */
  async refreshNow(): Promise<void> {
    this.#clearIngressTimer()
    this.#ingressRefreshPending = false
    await this.#sync("manual")
  }

  /** Accepts validated live-only product text; it never mutates task truth. */
  observeLive(frame: IngressLiveFrame): void {
    if (this.#closed || frame.taskId !== this.#taskId) return
    if (!this.#acceptIngressGeneration(frame.generation)) return
    const presentation = frame.presentation
    if (
      presentation.schema !== "zyra.product-presentation/v1"
      || presentation.kind !== "assistant"
      || presentation.phase !== "delta"
    ) return
    const messageId = presentationText(presentation, "identity")
    const streamId = presentationText(presentation, "streamId")
    const delta = presentationText(presentation, "text")
    if (!messageId || !streamId || delta === undefined || !delta.length) return
    const current = this.#snapshot.assistant
    if (
      current
      && current.generation === frame.generation
      && current.messageId === messageId
      && current.lastLiveSequence !== undefined
      && frame.liveSequence <= current.lastLiveSequence
    ) return
    const sameStream = Boolean(
      current
      && current.generation === frame.generation
      && current.messageId === messageId
      && current.streamId === streamId,
    )
    const combined = `${sameStream ? current?.text ?? "" : ""}${delta}`
    const bounded = boundedUtf8Tail(combined, this.#assistantTextBytes)
    const now = this.#environment.now()
    this.#publish({
      assistant: Object.freeze({
        messageId,
        streamId,
        text: bounded.text,
        generation: frame.generation,
        firstLiveSequence: sameStream
          ? current?.firstLiveSequence ?? frame.liveSequence
          : frame.liveSequence,
        lastLiveSequence: frame.liveSequence,
        // A durable started frame removes the mid-stream ambiguity.
        partial: sameStream ? current?.partial ?? true : true,
        truncated: bounded.truncated || (sameStream && current?.truncated === true),
        settling: false,
        startedAt: sameStream ? current?.startedAt ?? now : now,
        updatedAt: now,
      }),
    })
  }

  /** Reconciles durable presentation lifecycle and schedules a canonical read. */
  observeBatch(batch: IngressBatch): void {
    if (this.#closed || batch.taskId !== this.#taskId) return
    if (!this.#acceptIngressGeneration(batch.generation)) return
    const canonical = this.#workbench.getSnapshot().detail.task
    if (!canonical?.terminal) {
      for (const item of batch.presentations ?? []) {
        const presentation = item.presentation
        if (
          presentation.schema !== "zyra.product-presentation/v1"
          || presentation.kind !== "assistant"
        ) continue
        const phase = presentation.phase
        const messageId = presentationText(presentation, "identity")
        const streamId = presentationText(presentation, "streamId")
        if (!messageId || !streamId) continue
        const current = this.#snapshot.assistant
        const sameStream = Boolean(
          current
          && current.generation === batch.generation
          && current.messageId === messageId
          && current.streamId === streamId,
        )
        const now = this.#environment.now()
        if (phase === "started") {
          this.#publish({
            assistant: Object.freeze({
              messageId,
              streamId,
              text: sameStream ? current?.text ?? "" : "",
              generation: batch.generation,
              firstLiveSequence: sameStream ? current?.firstLiveSequence : undefined,
              lastLiveSequence: sameStream ? current?.lastLiveSequence : undefined,
              partial: false,
              truncated: sameStream && current?.truncated === true,
              settling: false,
              startedAt: sameStream ? current?.startedAt ?? now : now,
              updatedAt: now,
            }),
          })
        } else if (phase === "completed") {
          const completed = presentationText(presentation, "text")
          const bounded = boundedUtf8Tail(
            completed ?? (sameStream ? current?.text ?? "" : ""),
            this.#assistantTextBytes,
          )
          this.#publish({
            assistant: Object.freeze({
              messageId,
              streamId,
              text: bounded.text,
              generation: batch.generation,
              firstLiveSequence: sameStream ? current?.firstLiveSequence : undefined,
              lastLiveSequence: sameStream ? current?.lastLiveSequence : undefined,
              partial: false,
              truncated: bounded.truncated || (sameStream && current?.truncated === true),
              settling: true,
              startedAt: sameStream ? current?.startedAt ?? now : now,
              updatedAt: now,
            }),
          })
        }
      }
    }
    if (batch.events.length > 0 || (batch.presentations?.length ?? 0) > 0) {
      this.#scheduleIngressRefresh()
    }
  }

  observeConnection(connection: ConnectionSnapshot): void {
    if (this.#closed || connection.taskId !== this.#taskId) return
    if (!this.#acceptIngressGeneration(connection.generation)) return
    this.#publish({
      ingressPhase: connection.phase,
      ingressGeneration: connection.generation,
      ingressTransport: connection.transport,
    })
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    this.#clearTimer()
    this.#clearIngressTimer()
    this.#unlistenEnvironment()
    this.#unsubscribeWorkbench()
    this.#listeners.clear()
  }

  #plan(): SyncPlan {
    const taskId = this.#taskId
    if (!taskId) return { live: false, paused: false }
    const snapshot = this.#workbench.getSnapshot()
    if (!snapshot.transportEnabled) return { live: false, paused: false }
    const detail = snapshot.detail
    if (detail.taskId !== taskId) return { live: false, paused: false }
    const task = detail.task
    // Only a projection the backend already returned can tell us whether the
    // run is still moving.  Never guess before the first read settles.
    if (!task || task.taskId !== taskId) return { live: false, paused: false }
    if (task.terminal && !task.active) return { live: false, paused: false }
    if (this.#environment.hidden() || !this.#environment.online()) {
      return { live: true, paused: true }
    }
    if (this.#failures > 0) {
      const backoff = this.#activeIntervalMs * 2 ** Math.min(6, this.#failures)
      return {
        live: true,
        paused: false,
        delayMs: Math.min(this.#maximumBackoffMs, backoff),
      }
    }
    return {
      live: true,
      paused: false,
      delayMs: task.active ? this.#activeIntervalMs : this.#pendingIntervalMs,
    }
  }

  #signature(): string {
    const plan = this.#plan()
    return `${this.#taskId ?? ""}:${plan.live}:${plan.paused}:${plan.delayMs ?? ""}`
  }

  #reschedule(): void {
    if (this.#closed) return
    this.#clearTimer()
    const plan = this.#plan()
    this.#planSignature = `${this.#taskId ?? ""}:${plan.live}:${plan.paused}:${plan.delayMs ?? ""}`
    this.#publish({
      live: plan.live,
      paused: plan.paused,
      nextSyncAt:
        plan.delayMs === undefined
          ? undefined
          : this.#environment.now() + plan.delayMs,
    })
    if (plan.delayMs === undefined) return
    this.#timer = this.#environment.setTimeout(() => {
      this.#timer = undefined
      void this.#sync("interval")
    }, plan.delayMs)
  }

  async #sync(reason: TaskLiveSyncReason): Promise<void> {
    const taskId = this.#taskId
    if (this.#closed || !taskId || this.#inFlight) return
    const snapshot = this.#workbench.getSnapshot()
    if (!snapshot.transportEnabled) return
    // A user-initiated load already owns the detail slot; stepping on it would
    // abort the request the user is waiting for.
    if (snapshot.detail.phase === "loading" || snapshot.detail.syncing) {
      if (reason === "ingress") this.#scheduleIngressRefresh(150)
      this.#reschedule()
      return
    }
    if (reason === "interval" && snapshot.detail.taskId !== taskId) {
      this.#reschedule()
      return
    }
    this.#clearTimer()
    if (reason === "ingress") this.#ingressRefreshPending = false
    this.#inFlight = true
    this.#publish({ syncing: true })
    try {
      const detail = await this.#workbench.loadTask(taskId, { background: true })
      if (this.#closed || this.#taskId !== taskId) return
      if (detail.phase === "ready" && !detail.staleSince) {
        this.#failures = 0
        this.#publish({
          lastSyncedAt: this.#environment.now(),
          consecutiveFailures: 0,
        })
      } else if (detail.phase !== "loading") {
        this.#failures += 1
        this.#publish({ consecutiveFailures: this.#failures })
      }
    } catch {
      if (this.#closed || this.#taskId !== taskId) return
      this.#failures += 1
      this.#publish({ consecutiveFailures: this.#failures })
    } finally {
      this.#inFlight = false
      if (!this.#closed) {
        this.#publish({ syncing: false })
        if (this.#ingressRefreshPending) this.#scheduleIngressRefresh()
        else this.#reschedule()
      }
    }
  }

  #acceptIngressGeneration(generation: number): boolean {
    if (!Number.isSafeInteger(generation) || generation < 0) return false
    if (generation < this.#ingressGeneration) return false
    if (generation === this.#ingressGeneration) return true
    this.#ingressGeneration = generation
    this.#publish({
      assistant: undefined,
      ingressGeneration: generation,
    })
    return true
  }

  #scheduleIngressRefresh(delayMs = INGRESS_REFRESH_DELAY_MS): void {
    if (this.#closed || !this.#taskId) return
    this.#ingressRefreshPending = true
    if (this.#ingressTimer !== undefined || this.#inFlight) return
    this.#ingressTimer = this.#environment.setTimeout(() => {
      this.#ingressTimer = undefined
      void this.#sync("ingress")
    }, delayMs)
  }

  #reconcileCanonicalTask(): void {
    const taskId = this.#taskId
    if (!taskId || !this.#snapshot.assistant) return
    const detail = this.#workbench.getSnapshot().detail
    if (detail.taskId !== taskId || detail.task?.taskId !== taskId) return
    if (!detail.task.terminal) return
    this.#publish({ assistant: undefined })
  }

  #clearTimer(): void {
    if (this.#timer === undefined) return
    this.#environment.clearTimeout(this.#timer)
    this.#timer = undefined
  }

  #clearIngressTimer(): void {
    if (this.#ingressTimer === undefined) return
    this.#environment.clearTimeout(this.#ingressTimer)
    this.#ingressTimer = undefined
  }

  #publish(patch: Partial<TaskLiveSyncSnapshot>): void {
    const next: TaskLiveSyncSnapshot = {
      ...this.#snapshot,
      ...patch,
      revision: this.#snapshot.revision + 1,
    }
    const changed = (Object.keys(patch) as (keyof TaskLiveSyncSnapshot)[]).some(
      (key) => this.#snapshot[key] !== next[key],
    )
    if (!changed) return
    this.#snapshot = Object.freeze(next)
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // Live-sync observers cannot change refresh ownership.
      }
    }
  }
}
