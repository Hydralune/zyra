export type NotificationTone = "info" | "success" | "warning" | "error"

export interface WorkbenchNotification {
  id: string
  title: string
  message: string
  tone: NotificationTone
  createdAt: number
  expiresAt?: number
  taskId?: string
  actionLabel?: string
  acknowledged: boolean
}

export interface NotificationSnapshot {
  notifications: readonly WorkbenchNotification[]
  unreadCount: number
  revision: number
}

function notificationId(): string {
  return `notice_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 12)}`
}

function cloneNotification(value: WorkbenchNotification): WorkbenchNotification {
  return { ...value }
}

export class NotificationCenter {
  readonly #notifications: WorkbenchNotification[] = []
  readonly #listeners = new Set<() => void>()
  readonly #timers = new Map<string, ReturnType<typeof setTimeout>>()
  #snapshot: NotificationSnapshot = Object.freeze({
    notifications: Object.freeze([]),
    unreadCount: 0,
    revision: 0,
  })
  #revision = 0
  #closed = false

  getSnapshot = (): NotificationSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  push(input: {
    id?: string
    title: string
    message: string
    tone?: NotificationTone
    durationMs?: number
    taskId?: string
    actionLabel?: string
  }): WorkbenchNotification {
    this.#assertOpen()
    const title = input.title.trim()
    const message = input.message.trim()
    if (!title || !message) throw new TypeError("Notification title and message are required.")
    const duration = input.durationMs === undefined
      ? input.tone === "error" ? 0 : 6_000
      : Math.max(0, Math.min(120_000, Math.floor(input.durationMs)))
    const notification: WorkbenchNotification = {
      id: input.id ?? notificationId(),
      title,
      message,
      tone: input.tone ?? "info",
      createdAt: Date.now(),
      expiresAt: duration ? Date.now() + duration : undefined,
      taskId: input.taskId,
      actionLabel: input.actionLabel,
      acknowledged: false,
    }
    const existing = this.#notifications.findIndex((entry) => entry.id === notification.id)
    if (existing >= 0) {
      this.#notifications.splice(existing, 1, notification)
      this.#clearTimer(notification.id)
    } else {
      this.#notifications.unshift(notification)
    }
    if (this.#notifications.length > 100) {
      for (const removed of this.#notifications.splice(100)) this.#clearTimer(removed.id)
    }
    if (duration) {
      this.#timers.set(notification.id, setTimeout(() => {
        this.#timers.delete(notification.id)
        this.dismiss(notification.id)
      }, duration))
    }
    this.#publish()
    return cloneNotification(notification)
  }

  acknowledge(id: string): WorkbenchNotification | undefined {
    this.#assertOpen()
    const notification = this.#notifications.find((entry) => entry.id === id)
    if (!notification || notification.acknowledged) {
      return notification ? cloneNotification(notification) : undefined
    }
    notification.acknowledged = true
    this.#publish()
    return cloneNotification(notification)
  }

  acknowledgeAll(): void {
    this.#assertOpen()
    let changed = false
    for (const notification of this.#notifications) {
      if (!notification.acknowledged) {
        notification.acknowledged = true
        changed = true
      }
    }
    if (changed) this.#publish()
  }

  dismiss(id: string): WorkbenchNotification | undefined {
    if (this.#closed) return undefined
    const index = this.#notifications.findIndex((entry) => entry.id === id)
    if (index < 0) return undefined
    const [removed] = this.#notifications.splice(index, 1)
    this.#clearTimer(id)
    this.#publish()
    return removed ? cloneNotification(removed) : undefined
  }

  clear(options: { includeErrors?: boolean } = {}): WorkbenchNotification[] {
    this.#assertOpen()
    const removed: WorkbenchNotification[] = []
    for (let index = this.#notifications.length - 1; index >= 0; index -= 1) {
      const notification = this.#notifications[index]!
      if (notification.tone === "error" && !options.includeErrors) continue
      removed.unshift(cloneNotification(notification))
      this.#notifications.splice(index, 1)
      this.#clearTimer(notification.id)
    }
    if (removed.length) this.#publish()
    return removed
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    for (const timer of this.#timers.values()) clearTimeout(timer)
    this.#timers.clear()
    this.#listeners.clear()
    this.#notifications.length = 0
  }

  #clearTimer(id: string): void {
    const timer = this.#timers.get(id)
    if (timer) clearTimeout(timer)
    this.#timers.delete(id)
  }

  #publish(): void {
    this.#revision += 1
    const notifications = Object.freeze(this.#notifications.map(cloneNotification))
    this.#snapshot = Object.freeze({
      notifications,
      unreadCount: notifications.filter((entry) => !entry.acknowledged).length,
      revision: this.#revision,
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // Notification views cannot change lifecycle semantics.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Notification center is closed.")
  }
}
