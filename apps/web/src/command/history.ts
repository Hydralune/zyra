export interface HistoryStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export interface CommandHistoryEntry {
  id: string
  value: string
  mode: "prompt" | "command"
  createdAt: number
  taskId?: string
}

export interface HistoryNavigation {
  handled: boolean
  value?: string
  cursor?: "start" | "end"
  index: number
}

const MAX_HISTORY = 100

function historyId(value: string, createdAt: number): string {
  let hash = 0x811c9dc5
  for (const character of `${createdAt}:${value}`) {
    hash ^= character.charCodeAt(0)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return `history_${createdAt.toString(36)}_${hash.toString(16).padStart(8, "0")}`
}

function validEntry(value: unknown): value is CommandHistoryEntry {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false
  const entry = value as Record<string, unknown>
  return (
    typeof entry.id === "string" &&
    typeof entry.value === "string" &&
    (entry.mode === "prompt" || entry.mode === "command") &&
    typeof entry.createdAt === "number" &&
    Number.isFinite(entry.createdAt) &&
    (entry.taskId === undefined || typeof entry.taskId === "string")
  )
}

export class CommandHistory {
  readonly #storage?: HistoryStorage
  readonly #storageKey: string
  readonly #entries: CommandHistoryEntry[] = []
  #index = -1
  #savedDraft = ""

  constructor(options: { storage?: HistoryStorage; storageKey?: string } = {}) {
    this.#storage = options.storage
    this.#storageKey = options.storageKey ?? "zyra.workbench.command-history.v1"
    this.#load()
  }

  add(value: string, options: { taskId?: string; createdAt?: number } = {}): CommandHistoryEntry | undefined {
    const normalized = value.trim()
    if (!normalized) return undefined
    const previous = this.#entries[0]
    if (previous?.value === normalized && previous.taskId === options.taskId) {
      this.resetNavigation()
      return { ...previous }
    }
    const createdAt = options.createdAt ?? Date.now()
    const entry: CommandHistoryEntry = {
      id: historyId(normalized, createdAt),
      value: normalized,
      mode: normalized.startsWith("/") ? "command" : "prompt",
      createdAt,
      taskId: options.taskId,
    }
    this.#entries.unshift(entry)
    if (this.#entries.length > MAX_HISTORY) this.#entries.length = MAX_HISTORY
    this.resetNavigation()
    this.#persist()
    return { ...entry }
  }

  list(options: { taskId?: string; query?: string; limit?: number } = {}): CommandHistoryEntry[] {
    const query = options.query?.trim().toLowerCase()
    return this.#entries
      .filter((entry) => !options.taskId || entry.taskId === options.taskId)
      .filter((entry) => !query || entry.value.toLowerCase().includes(query))
      .slice(0, Math.max(1, Math.min(MAX_HISTORY, Math.floor(options.limit ?? MAX_HISTORY))))
      .map((entry) => ({ ...entry }))
  }

  navigate(
    direction: "up" | "down",
    currentValue: string,
    cursor: number,
    options: { taskId?: string } = {},
  ): HistoryNavigation {
    const values = this.list({ taskId: options.taskId })
    if (!values.length) return { handled: false, index: this.#index }
    if (!canNavigateHistory(direction, currentValue, cursor, this.#index >= 0)) {
      return { handled: false, index: this.#index }
    }
    if (direction === "up") {
      if (this.#index < 0) {
        this.#savedDraft = currentValue
        this.#index = 0
      } else if (this.#index < values.length - 1) {
        this.#index += 1
      } else {
        return { handled: false, index: this.#index }
      }
      return {
        handled: true,
        value: values[this.#index]?.value ?? currentValue,
        cursor: "start",
        index: this.#index,
      }
    }
    if (this.#index > 0) {
      this.#index -= 1
      return {
        handled: true,
        value: values[this.#index]?.value ?? currentValue,
        cursor: "end",
        index: this.#index,
      }
    }
    if (this.#index === 0) {
      this.#index = -1
      const saved = this.#savedDraft
      this.#savedDraft = ""
      return { handled: true, value: saved, cursor: "end", index: -1 }
    }
    return { handled: false, index: this.#index }
  }

  resetNavigation(): void {
    this.#index = -1
    this.#savedDraft = ""
  }

  remove(id: string): boolean {
    const index = this.#entries.findIndex((entry) => entry.id === id)
    if (index < 0) return false
    this.#entries.splice(index, 1)
    this.resetNavigation()
    this.#persist()
    return true
  }

  clear(): void {
    this.#entries.length = 0
    this.resetNavigation()
    this.#storage?.removeItem(this.#storageKey)
  }

  #load(): void {
    const raw = this.#storage?.getItem(this.#storageKey)
    if (!raw) return
    try {
      const value = JSON.parse(raw)
      if (!Array.isArray(value)) throw new TypeError("History value must be an array.")
      for (const entry of value) {
        if (validEntry(entry) && entry.value.trim()) {
          this.#entries.push({ ...entry, value: entry.value.trim() })
        }
        if (this.#entries.length >= MAX_HISTORY) break
      }
    } catch {
      this.#storage?.removeItem(this.#storageKey)
    }
  }

  #persist(): void {
    if (!this.#storage) return
    try {
      this.#storage.setItem(this.#storageKey, JSON.stringify(this.#entries))
    } catch {
      // Storage quota or privacy mode cannot break command submission.
    }
  }
}

export function canNavigateHistory(
  direction: "up" | "down",
  text: string,
  cursor: number,
  inHistory = false,
): boolean {
  const position = Math.max(0, Math.min(cursor, text.length))
  if (inHistory) return position === 0 || position === text.length
  if (direction === "up") {
    return position === 0 && !text.includes("\n")
  }
  return position === text.length && !text.includes("\n")
}

export function browserCommandHistory(): CommandHistory {
  return new CommandHistory({
    storage: typeof localStorage === "undefined" ? undefined : localStorage,
  })
}
