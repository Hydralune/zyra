export type OverlayKind =
  | "command-help"
  | "keyboard-help"
  | "transport-status"
  | "task-create"
  | "task-cancel"
  | "task-resume"
  | "error-detail"

export interface OverlayDescriptor {
  id: string
  kind: OverlayKind
  title: string
  modal: boolean
  dismissible: boolean
  openedAt: number
  focusCheckpoint?: string
  payload: Record<string, unknown>
}

export interface OverlaySnapshot {
  stack: readonly OverlayDescriptor[]
  active?: OverlayDescriptor
  revision: number
}

export interface OpenOverlayInput {
  id?: string
  kind: OverlayKind
  title: string
  modal?: boolean
  dismissible?: boolean
  focusCheckpoint?: string
  payload?: Record<string, unknown>
  replaceKind?: boolean
}

function overlayId(kind: OverlayKind): string {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID().replace(/-/g, "").slice(0, 16)
      : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
  return `overlay_${kind.replace(/-/g, "_")}_${random}`
}

function cloneOverlay(value: OverlayDescriptor): OverlayDescriptor {
  return {
    ...value,
    payload: { ...value.payload },
  }
}

export class OverlayRuntime {
  readonly #stack: OverlayDescriptor[] = []
  readonly #listeners = new Set<() => void>()
  #snapshot: OverlaySnapshot = Object.freeze({ stack: Object.freeze([]), revision: 0 })
  #revision = 0
  #closed = false

  getSnapshot = (): OverlaySnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  open(input: OpenOverlayInput): OverlayDescriptor {
    this.#assertOpen()
    if (!input.title.trim()) throw new TypeError("Overlay title must not be empty.")
    if (input.replaceKind) {
      const index = this.#stack.findIndex((entry) => entry.kind === input.kind)
      if (index >= 0) {
        const previous = this.#stack[index]!
        const next: OverlayDescriptor = {
          ...previous,
          title: input.title.trim(),
          modal: input.modal ?? previous.modal,
          dismissible: input.dismissible ?? previous.dismissible,
          focusCheckpoint: input.focusCheckpoint ?? previous.focusCheckpoint,
          payload: { ...input.payload },
          openedAt: Date.now(),
        }
        this.#stack.splice(index, 1)
        this.#stack.push(next)
        this.#publish()
        return cloneOverlay(next)
      }
    }
    const descriptor: OverlayDescriptor = {
      id: input.id ?? overlayId(input.kind),
      kind: input.kind,
      title: input.title.trim(),
      modal: input.modal ?? true,
      dismissible: input.dismissible ?? true,
      openedAt: Date.now(),
      focusCheckpoint: input.focusCheckpoint,
      payload: { ...input.payload },
    }
    if (this.#stack.some((entry) => entry.id === descriptor.id)) {
      throw new TypeError(`Overlay identity is already active: ${descriptor.id}`)
    }
    this.#stack.push(descriptor)
    this.#publish()
    return cloneOverlay(descriptor)
  }

  update(id: string, patch: Partial<Omit<OverlayDescriptor, "id" | "kind" | "openedAt">>): OverlayDescriptor | undefined {
    this.#assertOpen()
    const index = this.#stack.findIndex((entry) => entry.id === id)
    if (index < 0) return undefined
    const current = this.#stack[index]!
    const next: OverlayDescriptor = {
      ...current,
      title: patch.title?.trim() || current.title,
      modal: patch.modal ?? current.modal,
      dismissible: patch.dismissible ?? current.dismissible,
      focusCheckpoint: patch.focusCheckpoint ?? current.focusCheckpoint,
      payload: patch.payload ? { ...patch.payload } : current.payload,
    }
    this.#stack[index] = next
    this.#publish()
    return cloneOverlay(next)
  }

  close(id: string, options: { force?: boolean } = {}): OverlayDescriptor | undefined {
    this.#assertOpen()
    const index = this.#stack.findIndex((entry) => entry.id === id)
    if (index < 0) return undefined
    const current = this.#stack[index]!
    if (!current.dismissible && !options.force) return undefined
    this.#stack.splice(index, 1)
    this.#publish()
    return cloneOverlay(current)
  }

  closeActive(options: { force?: boolean } = {}): OverlayDescriptor | undefined {
    const active = this.#stack.at(-1)
    return active ? this.close(active.id, options) : undefined
  }

  closeKind(kind: OverlayKind, options: { force?: boolean; all?: boolean } = {}): OverlayDescriptor[] {
    this.#assertOpen()
    const removed: OverlayDescriptor[] = []
    for (let index = this.#stack.length - 1; index >= 0; index -= 1) {
      const current = this.#stack[index]!
      if (current.kind !== kind) continue
      if (!current.dismissible && !options.force) continue
      this.#stack.splice(index, 1)
      removed.unshift(cloneOverlay(current))
      if (!options.all) break
    }
    if (removed.length) this.#publish()
    return removed
  }

  closeAll(options: { force?: boolean } = {}): OverlayDescriptor[] {
    this.#assertOpen()
    const removed: OverlayDescriptor[] = []
    for (let index = this.#stack.length - 1; index >= 0; index -= 1) {
      const current = this.#stack[index]!
      if (!current.dismissible && !options.force) continue
      this.#stack.splice(index, 1)
      removed.unshift(cloneOverlay(current))
    }
    if (removed.length) this.#publish()
    return removed
  }

  active(): OverlayDescriptor | undefined {
    const value = this.#stack.at(-1)
    return value ? cloneOverlay(value) : undefined
  }

  find(id: string): OverlayDescriptor | undefined {
    const value = this.#stack.find((entry) => entry.id === id)
    return value ? cloneOverlay(value) : undefined
  }

  containsKind(kind: OverlayKind): boolean {
    return this.#stack.some((entry) => entry.kind === kind)
  }

  handleEscape(): OverlayDescriptor | undefined {
    const active = this.#stack.at(-1)
    if (!active || !active.dismissible) return undefined
    return this.close(active.id)
  }

  closeRuntime(): void {
    if (this.#closed) return
    this.#closed = true
    this.#stack.length = 0
    this.#revision += 1
    this.#snapshot = Object.freeze({
      stack: Object.freeze([]),
      revision: this.#revision,
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // Overlay observers cannot retain ownership.
      }
    }
    this.#listeners.clear()
  }

  #publish(): void {
    this.#revision += 1
    const stack = Object.freeze(this.#stack.map(cloneOverlay))
    this.#snapshot = Object.freeze({
      stack,
      active: stack.at(-1),
      revision: this.#revision,
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // UI observers cannot change overlay state.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Overlay runtime is closed.")
  }
}
