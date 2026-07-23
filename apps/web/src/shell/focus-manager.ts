export interface FocusTarget {
  id: string
  focus(options?: FocusOptions): void
  isConnected?: boolean
  disabled?: boolean
  getAttribute?(name: string): string | null
}

export interface FocusDocument {
  activeElement: Element | null
  getElementById(id: string): HTMLElement | null
  querySelector<E extends Element = Element>(selectors: string): E | null
}

export interface FocusCheckpoint {
  key: string
  targetId?: string
  fallbackSelector?: string
  reason: string
  createdAt: number
}

function canFocus(target: FocusTarget | null | undefined): target is FocusTarget {
  if (!target) return false
  if (target.isConnected === false) return false
  if (target.disabled === true) return false
  if (target.getAttribute?.("aria-hidden") === "true") return false
  return typeof target.focus === "function"
}

export class FocusManager {
  readonly #document: FocusDocument
  readonly #checkpoints = new Map<string, FocusCheckpoint>()
  readonly #listeners = new Set<(checkpoint: FocusCheckpoint, restored: boolean) => void>()
  #sequence = 0

  constructor(documentLike: FocusDocument) {
    this.#document = documentLike
  }

  capture(reason: string, fallbackSelector?: string): string {
    const active = this.#document.activeElement
    const targetId =
      active instanceof HTMLElement && active.id
        ? active.id
        : undefined
    const key = `focus_${Date.now().toString(36)}_${(++this.#sequence).toString(36)}`
    this.#checkpoints.set(key, {
      key,
      targetId,
      fallbackSelector,
      reason,
      createdAt: Date.now(),
    })
    this.#trim()
    return key
  }

  remember(
    key: string,
    options: { targetId?: string; fallbackSelector?: string; reason?: string },
  ): FocusCheckpoint {
    const checkpoint: FocusCheckpoint = {
      key,
      targetId: options.targetId,
      fallbackSelector: options.fallbackSelector,
      reason: options.reason ?? "explicit",
      createdAt: Date.now(),
    }
    this.#checkpoints.set(key, checkpoint)
    this.#trim()
    return { ...checkpoint }
  }

  checkpoint(key: string): FocusCheckpoint | undefined {
    const value = this.#checkpoints.get(key)
    return value ? { ...value } : undefined
  }

  restore(key: string, options: { consume?: boolean; preventScroll?: boolean } = {}): boolean {
    const checkpoint = this.#checkpoints.get(key)
    if (!checkpoint) return false
    const restored =
      this.#focusById(checkpoint.targetId, options.preventScroll) ||
      this.#focusBySelector(checkpoint.fallbackSelector, options.preventScroll)
    if (options.consume !== false) this.#checkpoints.delete(key)
    this.#emit(checkpoint, restored)
    return restored
  }

  focusId(id: string, options: FocusOptions = { preventScroll: true }): boolean {
    const target = this.#document.getElementById(id) as FocusTarget | null
    if (!canFocus(target)) return false
    target.focus(options)
    return true
  }

  focusSelector(selector: string, options: FocusOptions = { preventScroll: true }): boolean {
    const target = this.#document.querySelector(selector) as FocusTarget | null
    if (!canFocus(target)) return false
    target.focus(options)
    return true
  }

  routeFocus(target: "command" | "task-list" | "task-detail" | undefined): boolean {
    if (!target) return false
    if (target === "command") {
      return this.focusId("workbench-command-input")
    }
    if (target === "task-list") {
      return (
        this.focusSelector("[data-task-row][aria-current='true']") ||
        this.focusSelector("[data-task-row]") ||
        this.focusId("task-list-heading")
      )
    }
    return (
      this.focusId("task-detail-heading") ||
      this.focusSelector("[data-task-detail-action]")
    )
  }

  listen(listener: (checkpoint: FocusCheckpoint, restored: boolean) => void): () => void {
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  forget(key: string): boolean {
    return this.#checkpoints.delete(key)
  }

  clear(): void {
    this.#checkpoints.clear()
  }

  #focusById(id: string | undefined, preventScroll = true): boolean {
    if (!id) return false
    return this.focusId(id, { preventScroll })
  }

  #focusBySelector(selector: string | undefined, preventScroll = true): boolean {
    if (!selector) return false
    return this.focusSelector(selector, { preventScroll })
  }

  #trim(): void {
    if (this.#checkpoints.size <= 100) return
    const ordered = [...this.#checkpoints.values()].sort((a, b) => a.createdAt - b.createdAt)
    for (const checkpoint of ordered.slice(0, ordered.length - 100)) {
      this.#checkpoints.delete(checkpoint.key)
    }
  }

  #emit(checkpoint: FocusCheckpoint, restored: boolean): void {
    for (const listener of this.#listeners) {
      try {
        listener({ ...checkpoint }, restored)
      } catch {
        // Focus observers are diagnostic only.
      }
    }
  }
}

export function createBrowserFocusManager(): FocusManager {
  if (typeof document === "undefined") {
    throw new TypeError("Browser focus manager requires document.")
  }
  return new FocusManager(document)
}
