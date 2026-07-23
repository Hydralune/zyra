export type AnnouncementPriority = "polite" | "assertive"

export interface Announcement {
  id: string
  message: string
  priority: AnnouncementPriority
  createdAt: number
}

export interface AnnouncementSnapshot {
  polite: string
  assertive: string
  revision: number
}

export class AccessibilityAnnouncer {
  readonly #listeners = new Set<() => void>()
  readonly #recent = new Map<string, number>()
  #snapshot: AnnouncementSnapshot = Object.freeze({
    polite: "",
    assertive: "",
    revision: 0,
  })
  #closed = false

  getSnapshot = (): AnnouncementSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  announce(messageValue: string, priority: AnnouncementPriority = "polite"): Announcement | undefined {
    if (this.#closed) return undefined
    const message = messageValue.replace(/\s+/g, " ").trim()
    if (!message) return undefined
    const now = Date.now()
    const key = `${priority}:${message}`
    const previous = this.#recent.get(key)
    if (previous && now - previous < 1_000) return undefined
    this.#recent.set(key, now)
    for (const [entry, timestamp] of this.#recent) {
      if (now - timestamp > 60_000) this.#recent.delete(entry)
    }
    const announcement: Announcement = {
      id: `announcement_${now.toString(36)}_${this.#snapshot.revision.toString(36)}`,
      message,
      priority,
      createdAt: now,
    }
    this.#snapshot = Object.freeze({
      polite: priority === "polite" ? message : this.#snapshot.polite,
      assertive: priority === "assertive" ? message : this.#snapshot.assertive,
      revision: this.#snapshot.revision + 1,
    })
    this.#publish()
    return announcement
  }

  clear(priority?: AnnouncementPriority): void {
    if (this.#closed) return
    this.#snapshot = Object.freeze({
      polite: !priority || priority === "polite" ? "" : this.#snapshot.polite,
      assertive: !priority || priority === "assertive" ? "" : this.#snapshot.assertive,
      revision: this.#snapshot.revision + 1,
    })
    this.#publish()
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    this.#recent.clear()
    this.#listeners.clear()
  }

  #publish(): void {
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // Live-region observers cannot affect workbench state.
      }
    }
  }
}
